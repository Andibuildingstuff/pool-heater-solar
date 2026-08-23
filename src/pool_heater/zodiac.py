"""Zodiac iAquaLink client for the Z550iQ heat pump.

There is no official public API. This follows the community-documented cloud
API that the Home Assistant integrations for the same device family use:

    POST https://prod.zodiac-io.com/users/v1/login       {apiKey, email, password}
    GET  https://prod.zodiac-io.com/devices/v2/{serial}/shadow
    POST https://prod.zodiac-io.com/devices/v1/{serial}/shadow

Authorisation is the raw `IdToken` from `userPoolOAuth` -- no "Bearer" prefix.

The heat pump lives under `state.reported.equipment.hp_0`:
    state   0 off, 1 on          (writable)
    st      operating mode        (writable; 0 Boost, 1 Silent/EcoSilence)
    tsp     target setpoint in C  (writable; 8-32)
    status  0 off, 1 buffering, 2 heating   (read-only)

This endpoint rate-limits. The runner is built to read the shadow only when it
has a reason to, not once per five-minute cycle -- see `runner.should_reconcile`.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from .config import Config, Credentials
from .models import HeaterState, Mode

LOGGER = logging.getLogger(__name__)

BASE_URL = "https://prod.zodiac-io.com"
DEVICES_URL = "https://r-api.iaqualink.net/devices.json"
# Shared by every iAquaLink client; not a secret and not per-user.
DEFAULT_API_KEY = "EOOEMOW4YR6QNB07"
# The iAquaLink app's own client string. The cloud rejects the shadow endpoints
# for some device families otherwise, and this is your account talking to your
# own heat pump either way.
USER_AGENT = "okhttp/3.12.0"
TIMEOUT = 30
EQUIPMENT_KEY = "hp_0"

# Shadow reads are tried in this order. v1 is first because it is what the
# community integrations for this device family actually use; v2 is documented
# as equivalent but answered "missing signature" against a real zs500, which
# suggests it now expects AWS request signing that v1 does not.
SHADOW_READ_TEMPLATES = (
    "/devices/v1/{serial}/shadow",
    "/devices/v2/{serial}/shadow",
)
SHADOW_WRITE_TEMPLATE = "/devices/v1/{serial}/shadow"

# The shadow does not document a water-temperature field consistently across
# firmware versions, so we try the keys seen in the wild and fall back to none.
WATER_TEMP_KEYS = ("wt", "water_temp", "temp", "current_temp", "ta")


class ZodiacError(RuntimeError):
    """Any failure talking to the iAquaLink cloud."""


class ZodiacAuthError(ZodiacError):
    """Login was rejected."""


class ZodiacRateLimited(ZodiacError):
    """The cloud asked us to back off."""


class ZodiacClient:
    def __init__(
        self,
        credentials: Credentials,
        config: Config,
        base_url: str = BASE_URL,
        api_key: str | None = None,
        session: requests.Session | None = None,
    ):
        self._credentials = credentials
        self._config = config
        self._base = base_url.rstrip("/")
        self._api_key = api_key or DEFAULT_API_KEY
        self._session = session or requests.Session()
        self._id_token: str | None = None
        self._auth_token: str | None = None
        self._user_id: str | None = None
        self.shadow_path: str | None = None

    # -- auth ------------------------------------------------------------------

    def login(self) -> None:
        email, password = self._credentials.zodiac_email, self._credentials.zodiac_password
        if not (email and password):
            raise ZodiacAuthError("ZODIAC_EMAIL and ZODIAC_PASSWORD must both be set")
        payload = {"apiKey": self._api_key, "email": email, "password": password}
        try:
            response = self._session.post(
                f"{self._base}/users/v1/login",
                json=payload,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ZodiacError(f"login request failed: {exc}") from exc

        if response.status_code in (401, 403):
            raise ZodiacAuthError("iAquaLink rejected the email or password")
        if response.status_code == 429:
            raise ZodiacRateLimited("iAquaLink rate-limited the login")
        if not response.ok:
            raise ZodiacError(f"login returned {response.status_code}: {response.text[:200]}")

        try:
            data = response.json()
        except ValueError as exc:
            raise ZodiacError("login did not return JSON") from exc

        oauth = data.get("userPoolOAuth") or {}
        self._id_token = oauth.get("IdToken")
        self._auth_token = data.get("authentication_token")
        user_id = data.get("id") or data.get("userId")
        self._user_id = str(user_id) if user_id is not None else None
        if not self._id_token:
            raise ZodiacAuthError("no IdToken in the login response")

    def _headers(self, json_body: bool = False) -> dict[str, str]:
        if not self._id_token:
            self.login()
        headers = {
            "Authorization": self._id_token or "",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if json_body:
            headers["Content-Type"] = "application/json; charset=utf-8"
        return headers

    # -- device discovery ------------------------------------------------------

    def list_devices(self) -> list[dict[str, Any]]:
        """Devices on the account. Used by the probe tool to find the serial."""
        if not self._id_token:
            self.login()
        params = {
            "api_key": self._api_key,
            "authentication_token": self._auth_token or "",
            "user_id": self._user_id or "",
        }
        try:
            response = self._session.get(
                DEVICES_URL,
                params=params,
                headers={"Accept": "application/json", "User-Agent": USER_AGENT},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise ZodiacError(f"device list failed: {exc}") from exc
        if not response.ok:
            raise ZodiacError(
                f"device list returned {response.status_code}: {response.text[:200]}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ZodiacError("device list did not return JSON") from exc
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    # -- shadow ----------------------------------------------------------------

    def _serial(self) -> str:
        serial = self._credentials.zodiac_serial
        if not serial:
            raise ZodiacError("ZODIAC_SERIAL is not set")
        return serial

    def get_shadow(self, retry_auth: bool = True) -> dict[str, Any]:
        """Read the device shadow, trying each known endpoint shape in turn.

        The variants are not interchangeable in practice, and which one an
        account accepts is not documented anywhere trustworthy, so the working
        one is discovered and then remembered for the rest of the run.
        """
        serial = self._serial()
        templates = (
            (self.shadow_path,) if self.shadow_path else SHADOW_READ_TEMPLATES
        )
        failures: list[str] = []

        for template in templates:
            path = f"{self._base}{template.format(serial=serial)}"
            try:
                response = self._session.get(path, headers=self._headers(), timeout=TIMEOUT)
            except requests.RequestException as exc:
                raise ZodiacError(f"shadow read failed: {exc}") from exc

            if response.status_code in (401, 403) and retry_auth:
                self._id_token = None
                self.login()
                return self.get_shadow(retry_auth=False)
            if response.status_code == 429:
                raise ZodiacRateLimited("iAquaLink rate-limited the shadow read")

            if response.ok:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise ZodiacError("shadow read did not return JSON") from exc
                if not isinstance(data, dict):
                    raise ZodiacError("shadow read did not return an object")
                if self.shadow_path != template:
                    LOGGER.info("iAquaLink shadow endpoint: %s", template)
                self.shadow_path = template
                return data

            failures.append(f"{template} -> {response.status_code} {response.text[:120]}")

        raise ZodiacError("no shadow endpoint answered: " + "; ".join(failures))

    def set_desired(self, fields: dict[str, Any], retry_auth: bool = True) -> dict[str, Any]:
        """Write fields under equipment.hp_0 in the AWS-IoT desired-state shape."""
        payload = {"state": {"desired": {"equipment": {EQUIPMENT_KEY: fields}}}}
        path = f"{self._base}{SHADOW_WRITE_TEMPLATE.format(serial=self._serial())}"
        try:
            response = self._session.post(
                path, json=payload, headers=self._headers(json_body=True), timeout=TIMEOUT
            )
        except requests.RequestException as exc:
            raise ZodiacError(f"shadow write failed: {exc}") from exc
        if response.status_code in (401, 403) and retry_auth:
            self._id_token = None
            self.login()
            return self.set_desired(fields, retry_auth=False)
        if response.status_code == 429:
            raise ZodiacRateLimited("iAquaLink rate-limited the shadow write")
        if not response.ok:
            raise ZodiacError(f"shadow write returned {response.status_code}: {response.text[:200]}")
        try:
            return response.json()
        except ValueError:
            return {}

    # -- high level ------------------------------------------------------------

    def read_state(self) -> HeaterState:
        return parse_shadow(self.get_shadow(), self._config)

    def turn_on(self, mode: Mode, setpoint_c: float | None = None) -> None:
        fields: dict[str, Any] = {"state": 1, "st": self._config.mode_map[mode]}
        if setpoint_c is not None:
            fields["tsp"] = int(round(setpoint_c))
        self.set_desired(fields)

    def turn_off(self) -> None:
        self.set_desired({"state": 0})

    def set_mode(self, mode: Mode) -> None:
        self.set_desired({"st": self._config.mode_map[mode]})


# --- shadow parsing ------------------------------------------------------------


def equipment_from_shadow(shadow: dict[str, Any]) -> dict[str, Any]:
    """Pull `state.reported.equipment.hp_0` out, tolerating shape differences."""
    state = shadow.get("state")
    if not isinstance(state, dict):
        state = shadow
    reported = state.get("reported")
    if not isinstance(reported, dict):
        reported = state
    equipment = reported.get("equipment")
    if not isinstance(equipment, dict):
        return {}
    unit = equipment.get(EQUIPMENT_KEY)
    if isinstance(unit, dict):
        return unit
    # Some firmwares name the unit differently; take the first heat-pump-ish key.
    for key, value in equipment.items():
        if key.startswith("hp") and isinstance(value, dict):
            return value
    return {}


def parse_shadow(shadow: dict[str, Any], config: Config) -> HeaterState:
    unit = equipment_from_shadow(shadow)
    raw_state = unit.get("state")
    on = _truthy(raw_state)
    status = _int_or_none(unit.get("status"))
    # `status` is the authoritative running indicator when present: a unit that
    # reports status 0 is not heating even if `state` still reads 1.
    if raw_state is None and status is not None:
        on = status > 0
    return HeaterState(
        on=on,
        mode=_mode_from_code(_int_or_none(unit.get("st")), config),
        status=status,
        water_temp_c=_first_number(unit, WATER_TEMP_KEYS),
        setpoint_c=_number_or_none(unit.get("tsp")),
        raw=shadow,
    )


def _mode_from_code(code: int | None, config: Config) -> Mode | None:
    if code is None:
        return None
    for mode, value in config.mode_map.items():
        if value == code:
            return mode
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "on", "yes"}
    return False


def _int_or_none(value: Any) -> int | None:
    number = _number_or_none(value)
    return None if number is None else int(number)


def _number_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def _first_number(unit: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _number_or_none(unit.get(key))
        if value is not None:
            return value
    return None
