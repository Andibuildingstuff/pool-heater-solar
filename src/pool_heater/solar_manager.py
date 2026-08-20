"""Solar Manager cloud client.

Endpoints and field names follow the official external API (Swagger at
https://external-web.solar-manager.ch/swagger) as used by the community Home
Assistant integration:

    POST /v3/auth/refresh    API key exchanged for a 24h access token
    POST /v1/oauth/login     legacy email/password, deprecated 30.06.2027
    GET  /v3/users/{smId}/data/stream   live figures, plus a devices[] array

Live stream fields, all watts unless noted:
    pW   PV production          cW   house consumption
    iW   grid import            eW   grid export
    bcW  battery charging       bdW  battery discharging
    soc  battery state of charge (%)
"""

from __future__ import annotations

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
AUTH_EXCHANGE = "API key exchanged for a token at /v3/auth/refresh"
AUTH_RAW_BEARER = "API key sent directly as a bearer token"
AUTH_LEGACY_LOGIN = "email and password at /v1/oauth/login"

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
        self._token: str | None = None
        self._token_type = "Bearer"
        self._device_meta: dict[str, dict[str, Any]] | None = None
        self._strategy_index = 0
        self.auth_method: str | None = None

    # -- auth ------------------------------------------------------------------

    def _strategies(self) -> list[tuple[str, Any]]:
        """Ways to authenticate, most likely first.

        Solar Manager's own documentation is behind a support portal we could not
        read at build time, and the two plausible readings of "Cloud API key"
        differ: the key may be exchanged for a short-lived access token, or it may
        be the bearer token itself. Rather than guess, the client tries both and
        remembers which one your account accepted -- `probe-solar` prints it.
        """
        if self._credentials.solar_api_key:
            return [
                (AUTH_EXCHANGE, self._auth_exchange),
                (AUTH_RAW_BEARER, self._auth_raw_bearer),
            ]
        return [(AUTH_LEGACY_LOGIN, self._auth_legacy)]

    def authenticate(self) -> None:
        """Obtain a usable token, trying each strategy until one sticks."""
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

    def _auth_exchange(self) -> None:
        data = self._post_json(
            "/v3/auth/refresh",
            {"grant_type": "refresh_token", "refresh_token": self._credentials.solar_api_key},
        )
        token = data.get("access_token") or data.get("accessToken")
        if not token:
            raise SolarManagerAuthError("no access_token in the /v3/auth/refresh response")
        self._token = token
        self._token_type = data.get("token_type") or data.get("tokenType") or "Bearer"

    def _auth_raw_bearer(self) -> None:
        """Use the API key as the bearer token itself. Costs no extra request."""
        self._token = self._credentials.solar_api_key
        self._token_type = "Bearer"

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
        self._token = token
        self._token_type = data.get("tokenType", "Bearer")

    def _advance_strategy(self) -> bool:
        """Give up on the current strategy and move to the next, if there is one."""
        if self._strategy_index + 1 >= len(self._strategies()):
            return False
        self._strategy_index += 1
        self._token = None
        self.auth_method = None
        return True

    def _headers(self) -> dict[str, str]:
        if not self._token:
            self.authenticate()
        return {
            "Authorization": f"{self._token_type} {self._token}",
            "Accept": "application/json",
        }

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
                self._token = None
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
        return Reading(
            taken_at=now,
            pv_w=_watts(data, "pW"),
            consumption_w=_watts(data, "cW"),
            grid_import_w=_watts(data, "iW"),
            grid_export_w=_watts(data, "eW"),
            battery_charge_w=charge_w,
            battery_discharge_w=discharge_w,
            soc_pct=_optional_number(data.get("soc")),
            car_w=self.car_power(data),
            raw=data,
        )

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


def _looks_like_car_charger(device: dict[str, Any], meta: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(source.get(key, ""))
        for source in (device, meta, meta.get("tag", {}) if isinstance(meta.get("tag"), dict) else {})
        for key in ("type", "device_type", "deviceType", "device_group", "name", "model")
    ).lower()
    return any(hint in haystack for hint in CAR_CHARGER_HINTS)
