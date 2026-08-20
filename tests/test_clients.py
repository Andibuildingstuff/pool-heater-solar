"""The two API clients, against recorded response shapes."""

from __future__ import annotations

import json
from datetime import datetime

import pytest
import requests

from conftest import NOON
from pool_heater.config import Config, Credentials
from pool_heater.models import Mode
from pool_heater.solar_manager import (
    SolarManagerAuthError,
    SolarManagerClient,
    SolarManagerError,
)
from pool_heater.zodiac import ZodiacAuthError, ZodiacClient, ZodiacRateLimited, parse_shadow


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload if payload is not None else {})

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Answers requests from a routing table and records what was asked."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _answer(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json"), kwargs.get("params")))
        for fragment, response in self.routes.items():
            if fragment in url:
                return response(**kwargs) if callable(response) else response
        raise AssertionError(f"unexpected {method} {url}")

    def get(self, url, **kwargs):
        return self._answer("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._answer("POST", url, **kwargs)


STREAM = {
    "pW": 7200, "cW": 2400, "iW": 0, "eW": 3100,
    "bcW": 1700, "bdW": 0, "soc": 82,
    "devices": [
        {"_id": "aaa", "power": 4100},
        {"_id": "bbb", "power": 350},
    ],
}

SENSORS = [
    {"_id": "aaa", "type": "car-charger", "name": "Easee Home"},
    {"_id": "bbb", "type": "heat-pump", "tag": {"name": "Boiler"}},
]


def solar_client(routes, **cred_kwargs):
    credentials = Credentials(solar_api_key="key", solar_sm_id="sm1", **cred_kwargs)
    return SolarManagerClient(credentials, Config(), session=FakeSession(routes))


# --- Solar Manager ---------------------------------------------------------------


def test_the_api_key_is_exchanged_for_an_access_token():
    session = FakeSession({
        "/v3/auth/refresh": FakeResponse(payload={"access_token": "tok", "expires_in": 86400}),
        "/data/stream": FakeResponse(payload=STREAM),
        "/v1/info/sensors": FakeResponse(payload=SENSORS),
    })
    client = SolarManagerClient(
        Credentials(solar_api_key="my-key", solar_sm_id="sm1"), Config(), session=session
    )
    reading = client.read(NOON)

    method, url, body, _ = session.calls[0]
    assert body == {"grant_type": "refresh_token", "refresh_token": "my-key"}
    assert "/v3/auth/refresh" in url
    assert reading.surplus_w == 4800  # 3100 exported + 1700 into the battery


def test_the_legacy_email_login_is_used_when_no_api_key_is_set():
    session = FakeSession({
        "/v1/oauth/login": FakeResponse(payload={"accessToken": "tok", "tokenType": "Bearer"}),
        "/data/stream": FakeResponse(payload=STREAM),
        "/v1/info/sensors": FakeResponse(payload=SENSORS),
    })
    client = SolarManagerClient(
        Credentials(solar_email="a@b.ch", solar_password="pw", solar_sm_id="sm1"),
        Config(),
        session=session,
    )
    client.authenticate()
    assert session.calls[0][2] == {"email": "a@b.ch", "password": "pw"}


def test_rejected_credentials_are_reported_as_an_auth_error():
    client = solar_client({"/v3/auth/refresh": FakeResponse(status_code=401)})
    with pytest.raises(SolarManagerAuthError):
        client.authenticate()


def test_the_reading_maps_every_field_the_control_logic_needs():
    client = solar_client({
        "/v3/auth/refresh": FakeResponse(payload={"access_token": "t"}),
        "/data/stream": FakeResponse(payload=STREAM),
        "/v1/info/sensors": FakeResponse(payload=SENSORS),
    })
    reading = client.read(NOON)
    assert (reading.pv_w, reading.consumption_w) == (7200, 2400)
    assert (reading.grid_import_w, reading.grid_export_w) == (0, 3100)
    assert (reading.battery_charge_w, reading.battery_discharge_w) == (1700, 0)
    assert reading.soc_pct == 82


def test_the_car_charger_is_found_by_name_when_no_id_is_configured():
    client = solar_client({
        "/v3/auth/refresh": FakeResponse(payload={"access_token": "t"}),
        "/data/stream": FakeResponse(payload=STREAM),
        "/v1/info/sensors": FakeResponse(payload=SENSORS),
    })
    assert client.read(NOON).car_w == 4100


def test_an_explicit_car_device_id_wins(monkeypatch):
    monkeypatch.setenv("SOLAR_MANAGER_CAR_DEVICE_ID", "bbb")
    client = solar_client({
        "/v3/auth/refresh": FakeResponse(payload={"access_token": "t"}),
        "/data/stream": FakeResponse(payload=STREAM),
    })
    assert client.read(NOON).car_w == 350


def test_a_missing_device_list_does_not_fail_the_cycle():
    """Car priority is a refinement; losing it must not cost us the reading."""
    client = solar_client({
        "/v3/auth/refresh": FakeResponse(payload={"access_token": "t"}),
        "/data/stream": FakeResponse(payload=STREAM),
        "/v1/info/sensors": FakeResponse(status_code=500, text="boom"),
    })
    reading = client.read(NOON)
    assert reading.car_w == 0.0
    assert reading.surplus_w == 4800


def test_a_signed_battery_figure_is_split_when_the_pair_is_absent():
    client = solar_client({
        "/v3/auth/refresh": FakeResponse(payload={"access_token": "t"}),
        "/data/stream": FakeResponse(payload={"eW": 500, "batW": -2200, "soc": 40}),
        "/v1/info/sensors": FakeResponse(payload=[]),
    })
    reading = client.read(NOON)
    assert reading.battery_discharge_w == 2200
    assert reading.battery_charge_w == 0


def test_a_server_error_on_the_stream_is_raised():
    client = solar_client({
        "/v3/auth/refresh": FakeResponse(payload={"access_token": "t"}),
        "/data/stream": FakeResponse(status_code=502, text="bad gateway"),
    })
    with pytest.raises(SolarManagerError):
        client.stream()


def test_a_network_failure_is_wrapped_not_leaked():
    class Exploding(FakeSession):
        def get(self, url, **kwargs):
            raise requests.ConnectionError("no route to host")

    credentials = Credentials(solar_api_key="k", solar_sm_id="sm1")
    session = Exploding({"/v3/auth/refresh": FakeResponse(payload={"access_token": "t"})})
    client = SolarManagerClient(credentials, Config(), session=session)
    with pytest.raises(SolarManagerError):
        client.stream()


# --- Zodiac -----------------------------------------------------------------------

LOGIN_OK = FakeResponse(payload={
    "id": 4242,
    "authentication_token": "auth-tok",
    "userPoolOAuth": {"IdToken": "id-tok", "RefreshToken": "r", "ExpiresIn": 3600},
})

SHADOW = {
    "deviceId": "SERIAL1",
    "state": {
        "reported": {
            "equipment": {"hp_0": {"state": 1, "st": 0, "tsp": 28, "status": 2, "wt": 26}}
        }
    },
}


def zodiac_client(routes):
    credentials = Credentials(
        zodiac_email="a@b.ch", zodiac_password="pw", zodiac_serial="SERIAL1"
    )
    return ZodiacClient(credentials, Config(), session=FakeSession(routes))


def test_login_sends_the_shared_api_key_and_keeps_the_id_token():
    session = FakeSession({"/users/v1/login": LOGIN_OK, "/shadow": FakeResponse(payload=SHADOW)})
    client = ZodiacClient(
        Credentials(zodiac_email="a@b.ch", zodiac_password="pw", zodiac_serial="S1"),
        Config(),
        session=session,
    )
    client.read_state()
    _, url, body, _ = session.calls[0]
    assert "prod.zodiac-io.com/users/v1/login" in url
    assert body["email"] == "a@b.ch" and body["apiKey"]

    _, shadow_url, _, _ = session.calls[1]
    assert "/devices/v2/S1/shadow" in shadow_url


def test_the_authorization_header_is_the_raw_id_token():
    captured = {}

    def shadow(**kwargs):
        captured.update(kwargs["headers"])
        return FakeResponse(payload=SHADOW)

    client = zodiac_client({"/users/v1/login": LOGIN_OK, "/shadow": shadow})
    client.read_state()
    assert captured["Authorization"] == "id-tok"


def test_bad_credentials_raise_an_auth_error():
    client = zodiac_client({"/users/v1/login": FakeResponse(status_code=401)})
    with pytest.raises(ZodiacAuthError):
        client.login()


def test_rate_limiting_is_reported_distinctly():
    client = zodiac_client({"/users/v1/login": LOGIN_OK, "/shadow": FakeResponse(status_code=429)})
    with pytest.raises(ZodiacRateLimited):
        client.get_shadow()


def test_turning_on_writes_state_mode_and_setpoint():
    session = FakeSession({"/users/v1/login": LOGIN_OK, "/shadow": FakeResponse(payload={})})
    config = Config(setpoint_c=28)
    client = ZodiacClient(
        Credentials(zodiac_email="a@b.ch", zodiac_password="pw", zodiac_serial="S1"),
        config,
        session=session,
    )
    client.turn_on(Mode.BOOST, config.setpoint_c)
    _, url, body, _ = session.calls[-1]
    assert "/devices/v1/S1/shadow" in url
    assert body == {"state": {"desired": {"equipment": {"hp_0": {"state": 1, "st": 0, "tsp": 28}}}}}


def test_turning_off_writes_only_the_power_field():
    session = FakeSession({"/users/v1/login": LOGIN_OK, "/shadow": FakeResponse(payload={})})
    client = ZodiacClient(
        Credentials(zodiac_email="a@b.ch", zodiac_password="pw", zodiac_serial="S1"),
        Config(),
        session=session,
    )
    client.turn_off()
    assert session.calls[-1][2] == {"state": {"desired": {"equipment": {"hp_0": {"state": 0}}}}}


def test_an_expired_token_triggers_one_re_login_then_succeeds():
    attempts = {"count": 0}

    def shadow(**kwargs):
        attempts["count"] += 1
        return FakeResponse(status_code=401) if attempts["count"] == 1 else FakeResponse(payload=SHADOW)

    client = zodiac_client({"/users/v1/login": LOGIN_OK, "/shadow": shadow})
    assert client.read_state().on is True
    assert attempts["count"] == 2


def test_the_shadow_is_parsed_into_a_heater_state():
    state = parse_shadow(SHADOW, Config())
    assert state.on is True
    assert state.mode is Mode.BOOST
    assert (state.status, state.setpoint_c, state.water_temp_c) == (2, 28.0, 26.0)


def test_an_unfamiliar_equipment_key_is_still_found():
    shadow = {"state": {"reported": {"equipment": {"hp_1": {"state": 0, "st": 1}}}}}
    assert parse_shadow(shadow, Config()).mode is Mode.ECOSILENCE


def test_an_empty_shadow_reads_as_off_rather_than_exploding():
    assert parse_shadow({}, Config()).on is False
