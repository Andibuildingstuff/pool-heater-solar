"""Solar Manager cloud client.

Behaviour here follows Solar Manager's own API article (Wissensdatenbank ->
"Solar Manager API"), not guesswork:

    Base URL      https://cloud.solar-manager.ch/   (HTTPS only)
    Rate limit    500 requests per hour per endpoint
                  ...except /v3/auth/refresh, capped at 50 per hour

Four authentication methods are offered. We prefer the header one, because this
runs as a stateless five-minute job with nowhere safe to cache a token: the
repository is public, so writing an access token into the state file is not an
option. Sending the key as a header sidesteps the token lifecycle completely and
never touches the rate-limited refresh endpoint.

    1. X-API-KEY header          <- preferred here: no exchange, no caching
    2. POST /v3/auth/refresh     {"grant_type": "refresh_token", ...}
    3. Basic auth, username + API key as the password
    4. Email and password        deprecated, ends 30 June 2027

Do NOT enable "Erneuerung erlauben" (allow renewal) on the API key. That turns it
into a rotating refresh token: every exchange issues a new key and invalidates
the old one, which a job that cannot persist secrets would break on its second
run. The static key is what this wants.

Live figures come from the data stream. Powers are watts:
    pW  production        cW  consumption
    bcW battery charging  bdW battery discharging
    iW  grid import       eW  grid export        soc  state of charge (%)

Grid import and export are derived from the energy balance when the stream does
not carry them: the local /v2/point endpoint documents only the watt-hour forms,
so the watt pair cannot be assumed present.
"""

from __future__ import annotations

import base64
import logging
import os
from datetime import datetime
from typing import Any

import requests

from .config import Config, Credentials
from .models import Reading

LOGGER = logging.getLogger(__name__)

BASE_URL = "https://cloud.solar-manager.ch"
TIMEOUT = 30

# Names for the authentication strategies, reported by `probe-solar` so the one
# that actually works can be written down rather than rediscovered.
AUTH_HEADER_KEY = "API key in the X-API-KEY header"
AUTH_EXCHANGE = "API key exchanged for a token at /v3/auth/refresh"
AUTH_BASIC_KEY = "HTTP basic auth, API key as the password"
AUTH_LEGACY_LOGIN = "email and password at /v1/oauth/login"

# Words that identify the house battery in the device list, used only when the
# stream carries no top-level state of charge. Cars report `soc` too, so a car
# must never be mistaken for the battery here.
BATTERY_HINTS = ("battery", "batterie", "speicher", "akku", "pylontech")

# Endpoints worth asking when the SM ID is unknown. Solar Manager documents the
# endpoints that consume the ID but not one that lists it, so these are informed
# guesses -- used only by the probe, which reports what answers rather than
# depending on any of them.
SM_ID_DISCOVERY_PATHS = (
    "/v1/info/users",
    "/v1/users",
    "/v1/info/user",
    "/v3/users",
    "/v1/gateways",
    "/v1/info/gateways",
)

# Words that identify a car charger in the device list when no explicit device
# id is configured. Solar Manager labels devices by tag name and device type.
CAR_CHARGER_HINTS = ("easee", "charger", "ladestation", "wallbox", "car")


class SolarManagerError(RuntimeError):
    """Any failure to get a usable reading out of Solar Manager."""


class SolarManagerAuthError(SolarManagerError):
    """Credentials were rejected."""


class SolarManagerClient:
    def __init__(
        self,
        credentials: Credentials,
        config: Config,
        base_url: str = BASE_URL,
        session: requests.Session | None = None,
    ):
        self._credentials = credentials
        self._config = config
        self._base = base_url.rstrip("/")
        self._session = session or requests.Session()
        self._auth_headers: dict[str, str] | None = None
        self._device_meta: dict[str, dict[str, Any]] | None = None
        self._strategy_index = 0
        self.auth_method: str | None = None

    # -- auth ------------------------------------------------------------------

    def _strategies(self) -> list[tuple[str, Any]]:
        """Ways to authenticate, best-for-this-architecture first.

        The header method is first on purpose. It costs no extra request, never
        touches the 50-per-hour refresh endpoint, and needs nothing cached
        between runs. The exchange is Solar Manager's general recommendation and
        is kept as the fallback for accounts where header auth is not enabled.
        """
        if self._credentials.solar_api_key:
            options = [
                (AUTH_HEADER_KEY, self._auth_header_key),
                (AUTH_EXCHANGE, self._auth_exchange),
            ]
            if self._credentials.solar_email:
                options.append((AUTH_BASIC_KEY, self._auth_basic_key))
            return options
        return [(AUTH_LEGACY_LOGIN, self._auth_legacy)]

    def authenticate(self) -> None:
        """Obtain usable credentials, trying each method until one sticks."""
        strategies = self._strategies()
        failures: list[str] = []
        while self._strategy_index < len(strategies):
            name, attempt = strategies[self._strategy_index]
            try:
                attempt()
            except SolarManagerError as exc:
                failures.append(f"{name} -> {exc}")
                self._strategy_index += 1
                continue
            if self.auth_method != name:
                LOGGER.info("Solar Manager authenticated: %s", name)
            self.auth_method = name
            return
        raise SolarManagerAuthError(
            "no Solar Manager authentication method was accepted: " + "; ".join(failures)
        )

    def _auth_header_key(self) -> None:
        """Send the key as a header. No request, no token, nothing to expire."""
        self._auth_headers = {"X-API-KEY": self._credentials.solar_api_key or ""}

    def _auth_exchange(self) -> None:
        data = self._post_json(
            "/v3/auth/refresh",
            {"grant_type": "refresh_token", "refresh_token": self._credentials.solar_api_key},
        )
        token = data.get("access_token") or data.get("accessToken")
        if not token:
            raise SolarManagerAuthError("no access_token in the /v3/auth/refresh response")
        token_type = data.get("token_type") or data.get("tokenType") or "Bearer"
        self._auth_headers = {"Authorization": f"{token_type} {token}"}

    def _auth_basic_key(self) -> None:
        pair = f"{self._credentials.solar_email}:{self._credentials.solar_api_key}"
        encoded = base64.b64encode(pair.encode("utf-8")).decode("ascii")
        self._auth_headers = {"Authorization": f"Basic {encoded}"}

    def _auth_legacy(self) -> None:
        if not (self._credentials.solar_email and self._credentials.solar_password):
            raise SolarManagerAuthError(
                "set SOLAR_MANAGER_API_KEY, or both SOLAR_MANAGER_EMAIL and "
                "SOLAR_MANAGER_PASSWORD"
            )
        data = self._post_json(
            "/v1/oauth/login",
            {
                "email": self._credentials.solar_email,
                "password": self._credentials.solar_password,
            },
        )
        token = data.get("accessToken") or data.get("access_token")
        if not token:
            raise SolarManagerAuthError("no accessToken in the /v1/oauth/login response")
        self._auth_headers = {"Authorization": f"{data.get('tokenType', 'Bearer')} {token}"}

    def _advance_strategy(self) -> bool:
        """Give up on the current method and move to the next, if there is one."""
        if self._strategy_index + 1 >= len(self._strategies()):
            return False
        self._strategy_index += 1
        self._auth_headers = None
        self.auth_method = None
        return True

    def _headers(self) -> dict[str, str]:
        if not self._auth_headers:
            self.authenticate()
        return {**(self._auth_headers or {}), "Accept": "application/json"}

    # -- transport -------------------------------------------------------------

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._session.post(
                f"{self._base}{path}",
                json=payload,
                headers={"Accept": "application/json"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            raise SolarManagerError(f"POST {path} failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise SolarManagerAuthError(
                f"POST {path} rejected the credentials ({response.status_code})"
            )
        if not response.ok:
            raise SolarManagerError(
                f"POST {path} returned {response.status_code}: {response.text[:200]}"
            )
        return _as_dict(response, path)

    def _get_json(self, path: str, attempt: int = 0) -> Any:
        try:
            response = self._session.get(
                f"{self._base}{path}", headers=self._headers(), timeout=TIMEOUT
            )
        except requests.RequestException as exc:
            raise SolarManagerError(f"GET {path} failed: {exc}") from exc

        if response.status_code in (401, 403):
            # First rejection: assume the token simply expired and mint another.
            # Second: the strategy itself is wrong for this account, so move on
            # to the next one. Third: we are out of ideas, and say so plainly.
            if attempt == 0:
                self._auth_headers = None
                self.authenticate()
                return self._get_json(path, attempt=1)
            if attempt == 1 and self._advance_strategy():
                return self._get_json(path, attempt=2)
            raise SolarManagerAuthError(
                f"GET {path} rejected ({response.status_code}) using "
                f"{self.auth_method or 'no accepted method'}"
            )
        if not response.ok:
            raise SolarManagerError(
                f"GET {path} returned {response.status_code}: {response.text[:200]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise SolarManagerError(f"GET {path} did not return JSON") from exc

    # -- reads -----------------------------------------------------------------

    def stream(self) -> dict[str, Any]:
        sm_id = self._credentials.solar_sm_id
        if not sm_id:
            raise SolarManagerError("SOLAR_MANAGER_SM_ID is not set")
        data = self._get_json(f"/v3/users/{sm_id}/data/stream")
        if isinstance(data, list) and data:
            # Some deployments wrap the current sample in a single-item list.
            data = data[0]
        if not isinstance(data, dict):
            raise SolarManagerError("stream response was not an object")
        return data

    def discover_sm_id(self) -> list[tuple[str, Any]]:
        """Hunt for the SM ID so it does not have to be found by hand.

        Solar Manager's article documents the endpoints that *use* the ID but
        not one that lists it, so this tries the plausible candidates and reports
        whatever answers. It is diagnostic only -- `probe-solar` calls it, the
        control loop never does.
        """
        found: list[tuple[str, Any]] = []

        # An exchanged access token is a JWT, and its claims usually name the
        # user or installation it was minted for.
        if self._credentials.solar_api_key:
            try:
                self._auth_exchange()
                header = (self._auth_headers or {}).get("Authorization", "")
                claims = _jwt_claims(header.split(" ")[-1])
                if claims:
                    found.append(("access token claims", claims))
            except SolarManagerError as exc:
                LOGGER.debug("token exchange unavailable for discovery: %s", exc)
            finally:
                self._auth_headers = None
                self._strategy_index = 0

        for path in SM_ID_DISCOVERY_PATHS:
            try:
                found.append((path, self._get_json(path)))
            except SolarManagerError as exc:
                LOGGER.debug("%s did not answer: %s", path, exc)
        return found

    def device_metadata(self) -> dict[str, dict[str, Any]]:
        """Device list keyed by `_id`, used to find the car charger."""
        if self._device_meta is not None:
            return self._device_meta
        sm_id = self._credentials.solar_sm_id
        try:
            devices = self._get_json(f"/v1/info/sensors/{sm_id}")
        except SolarManagerError as exc:
            # Car priority is a refinement, not a safety rail. If the device
            # list is unavailable we carry on with car_w = 0 rather than
            # failing the whole cycle.
            LOGGER.warning("could not read the device list: %s", exc)
            self._device_meta = {}
            return self._device_meta
        meta: dict[str, dict[str, Any]] = {}
        if isinstance(devices, list):
            for device in devices:
                if isinstance(device, dict) and device.get("_id"):
                    meta[str(device["_id"])] = device
        self._device_meta = meta
        return meta

    def read(self, now: datetime) -> Reading:
        """One complete house reading, ready for the control logic."""
        data = self.stream()
        charge_w, discharge_w = _battery_powers(data)
        import_w, export_w = _grid_powers(data, charge_w, discharge_w)
        return Reading(
            taken_at=now,
            pv_w=_watts(data, "pW"),
            consumption_w=_watts(data, "cW"),
            grid_import_w=import_w,
            grid_export_w=export_w,
            battery_charge_w=charge_w,
            battery_discharge_w=discharge_w,
            soc_pct=self.battery_soc(data),
            car_w=self.car_power(data),
            raw=data,
        )

    def battery_soc(self, stream_data: dict[str, Any]) -> float | None:
        """House battery state of charge, in percent.

        Prefers the top-level figure. Failing that, looks for a battery in the
        device list -- carefully, because cars report `soc` as well and taking a
        car's charge level for the house battery would corrupt the SOC_FLOOR
        rule in the one direction that matters.
        """
        top_level = _optional_number(stream_data.get("soc"))
        if top_level is not None:
            return top_level

        devices = stream_data.get("devices")
        if not isinstance(devices, list):
            return None
        meta = self.device_metadata()
        for device in devices:
            if not isinstance(device, dict):
                continue
            soc = _optional_number(device.get("soc"))
            if soc is None:
                continue
            info = meta.get(str(device.get("_id", "")), {})
            if _looks_like_car_charger(device, info):
                continue
            if _looks_like_battery(device, info):
                return soc
        return None

    def car_power(self, stream_data: dict[str, Any]) -> float:
        """Current draw of the car charger, or 0 if it cannot be identified."""
        devices = stream_data.get("devices")
        if not isinstance(devices, list):
            return 0.0

        wanted = self._credentials_car_device_id()
        meta = self.device_metadata() if not wanted else {}

        for device in devices:
            if not isinstance(device, dict):
                continue
            device_id = str(device.get("_id", ""))
            if wanted:
                if device_id != wanted:
                    continue
            elif not _looks_like_car_charger(device, meta.get(device_id, {})):
                continue
            power = _optional_number(device.get("power"))
            if power is not None:
                return abs(power)
        return 0.0

    def _credentials_car_device_id(self) -> str | None:
        value = os.environ.get("SOLAR_MANAGER_CAR_DEVICE_ID", "").strip()
        return value or None


# --- response parsing helpers --------------------------------------------------


def _as_dict(response: requests.Response, path: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise SolarManagerError(f"{path} did not return JSON") from exc
    if not isinstance(data, dict):
        raise SolarManagerError(f"{path} returned {type(data).__name__}, expected an object")
    return data


def _optional_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def _watts(data: dict[str, Any], key: str) -> float:
    value = _optional_number(data.get(key))
    return 0.0 if value is None else value


def _battery_powers(data: dict[str, Any]) -> tuple[float, float]:
    """Charge and discharge in watts, both non-negative.

    Prefers the split `bcW`/`bdW` fields. Falls back to the signed net figure,
    where a positive value means the battery is charging.
    """
    if "bcW" in data or "bdW" in data:
        return abs(_watts(data, "bcW")), abs(_watts(data, "bdW"))
    net = _optional_number(data.get("batW"))
    if net is None:
        return 0.0, 0.0
    return (net, 0.0) if net >= 0 else (0.0, -net)


def _grid_powers(
    data: dict[str, Any], charge_w: float, discharge_w: float
) -> tuple[float, float]:
    """Grid import and export in watts, both non-negative.

    The cloud stream carries `iW`/`eW` directly. The documented `/v2/point`
    schema does not -- it lists only the watt-hour forms, accumulated over an
    interval whose length is not reported. Rather than guess at that interval,
    fall back to the energy balance, which needs only the watt figures that are
    always present:

        exported = production + battery discharge - consumption - battery charge

    A positive result is export, a negative one is import.
    """
    if "iW" in data or "eW" in data:
        return abs(_watts(data, "iW")), abs(_watts(data, "eW"))
    net_export = _watts(data, "pW") + discharge_w - _watts(data, "cW") - charge_w
    return (0.0, net_export) if net_export >= 0 else (-net_export, 0.0)


def _looks_like_battery(device: dict[str, Any], meta: dict[str, Any]) -> bool:
    return any(hint in _describe(device, meta) for hint in BATTERY_HINTS)


def _looks_like_car_charger(device: dict[str, Any], meta: dict[str, Any]) -> bool:
    return any(hint in _describe(device, meta) for hint in CAR_CHARGER_HINTS)


def _describe(device: dict[str, Any], meta: dict[str, Any]) -> str:
    """Everything the API says about a device, lowercased, for hint matching."""
    tag = meta.get("tag") if isinstance(meta.get("tag"), dict) else {}
    return " ".join(
        str(source.get(key, ""))
        for source in (device, meta, tag)
        for key in ("type", "device_type", "deviceType", "device_group", "name", "model")
    ).lower()


def _jwt_claims(token: str) -> dict[str, Any] | None:
    """Decode a JWT payload without verifying it.

    We are reading it for a hint, not trusting it for a decision, so no signature
    check is needed or wanted -- and the key to check it with is the server's.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        import json

        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return None
