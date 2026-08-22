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
    AUTH_BASIC_KEY,
    AUTH_EXCHANGE,
    AUTH_HEADER_KEY,
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


def test_the_api_key_is_sent_as_a_header_without_any_token_exchange():
    """Preferred: /v3/auth/refresh is capped at 50 calls an hour and needs caching."""
    session = FakeSession({
        "/data/stream": FakeResponse(payload=STREAM),
        "/v1/info/sensors": FakeResponse(payload=SENSORS),
    })
    client = SolarManagerClient(
        Credentials(solar_api_key="my-key", solar_sm_id="sm1"), Config(), session=session
    )
    reading = client.read(NOON)

    assert client.auth_method == AUTH_HEADER_KEY
    assert not any("/v3/auth/refresh" in call[1] for call in session.calls)
    stream = next(call for call in session.calls if "/data/stream" in call[1])
    assert reading.surplus_w == 4800  # 3100 exported + 1700 into the battery


def test_header_auth_sends_the_documented_header_name():
    captured = {}

    def stream(**kwargs):
        captured.update(kwargs["headers"])
        return FakeResponse(payload=STREAM)

    client = solar_client({"/data/stream": stream})
    client.stream()
    assert captured["X-API-KEY"] == "key"


def test_the_exchange_is_used_when_header_auth_is_refused():
    session = FakeSession({
        "/data/stream": lambda **kw: (
            FakeResponse(status_code=401)
            if "X-API-KEY" in kw["headers"]
            else FakeResponse(payload=STREAM)
        ),
        "/v3/auth/refresh": FakeResponse(payload={"access_token": "tok"}),
        "/v1/info/sensors": FakeResponse(payload=SENSORS),
    })
    client = SolarManagerClient(
        Credentials(solar_api_key="my-key", solar_sm_id="sm1"), Config(), session=session
    )
    reading = client.read(NOON)

    exchange = next(call for call in session.calls if "/v3/auth/refresh" in call[1])
    assert exchange[2] == {"grant_type": "refresh_token", "refresh_token": "my-key"}
    assert client.auth_method == AUTH_EXCHANGE
    assert reading.surplus_w == 4800


def test_basic_auth_is_offered_only_when_a_username_is_known():
    with_email = SolarManagerClient(
        Credentials(solar_api_key="k", solar_email="a@b.ch", solar_sm_id="sm"),
        Config(), session=FakeSession({}),
    )
    without = SolarManagerClient(
        Credentials(solar_api_key="k", solar_sm_id="sm"), Config(), session=FakeSession({})
    )
    assert AUTH_BASIC_KEY in [name for name, _ in with_email._strategies()]
    assert AUTH_BASIC_KEY not in [name for name, _ in without._strategies()]


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


def test_when_no_method_is_accepted_the_error_says_which_were_tried():
    client = solar_client({
        "/v3/auth/refresh": FakeResponse(status_code=401),
        "/data/stream": FakeResponse(status_code=401),
        "/v1/info/sensors": FakeResponse(status_code=401),
    })
    with pytest.raises(SolarManagerAuthError) as raised:
        client.stream()
    assert "rejected" in str(raised.value)


def test_a_single_rejection_is_retried_before_the_method_is_abandoned():
    calls = {"stream": 0}

    def stream(**kwargs):
        calls["stream"] += 1
        return FakeResponse(status_code=401) if calls["stream"] == 1 else FakeResponse(payload=STREAM)

    client = solar_client({"/data/stream": stream})
    assert client.stream()["eW"] == 3100
    assert client.auth_method == AUTH_HEADER_KEY, "one rejection is not a reason to switch"


def test_a_rejected_legacy_login_is_reported_as_an_auth_error():
    """With no API key there is no fallback, so the failure surfaces immediately."""
    credentials = Credentials(solar_email="a@b.ch", solar_password="pw", solar_sm_id="sm1")
    session = FakeSession({"/v1/oauth/login": FakeResponse(status_code=401)})
    client = SolarManagerClient(credentials, Config(), session=session)
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


# --- field mapping the documented schema forced ------------------------------------


def test_grid_powers_are_derived_when_the_watt_pair_is_absent():
    """/v2/point documents only iWh/eWh, so iW/eW cannot be assumed present."""
    client = solar_client({
        "/data/stream": FakeResponse(payload={
            "pW": 7000, "cW": 2000, "bcW": 1000, "bdW": 0, "soc": 70,
        }),
        "/v1/info/sensors": FakeResponse(payload=[]),
    })
    reading = client.read(NOON)
    # 7000 produced - 2000 used - 1000 stored = 4000 exported
    assert reading.grid_export_w == 4000
    assert reading.grid_import_w == 0
    assert reading.surplus_w == 5000  # 4000 exported + 1000 into the battery


def test_the_derived_balance_reports_import_when_the_house_is_short():
    client = solar_client({
        "/data/stream": FakeResponse(payload={"pW": 500, "cW": 3000, "bdW": 800, "soc": 40}),
        "/v1/info/sensors": FakeResponse(payload=[]),
    })
    reading = client.read(NOON)
    assert reading.grid_import_w == 1700  # 3000 needed - 500 made - 800 from the battery
    assert reading.grid_export_w == 0


def test_the_reported_watt_pair_wins_over_the_derivation():
    client = solar_client({
        "/data/stream": FakeResponse(payload=STREAM),
        "/v1/info/sensors": FakeResponse(payload=SENSORS),
    })
    reading = client.read(NOON)
    assert (reading.grid_import_w, reading.grid_export_w) == (0, 3100)


def test_battery_soc_falls_back_to_the_battery_device():
    client = solar_client({
        "/data/stream": FakeResponse(payload={
            "pW": 5000, "cW": 1000,
            "devices": [{"_id": "bat", "soc": 64}],
        }),
        "/v1/info/sensors": FakeResponse(payload=[
            {"_id": "bat", "type": "battery", "name": "Pylontech"},
        ]),
    })
    assert client.read(NOON).soc_pct == 64


def test_a_cars_charge_level_is_never_mistaken_for_the_house_battery():
    """Cars report `soc` too. Reading one as the battery would corrupt SOC_FLOOR."""
    client = solar_client({
        "/data/stream": FakeResponse(payload={
            "pW": 5000, "cW": 1000,
            "devices": [{"_id": "aaa", "soc": 88, "power": 4100}],
        }),
        "/v1/info/sensors": FakeResponse(payload=SENSORS),
    })
    assert client.read(NOON).soc_pct is None


def test_an_unknown_soc_is_left_unknown_rather_than_guessed():
    client = solar_client({
        "/data/stream": FakeResponse(payload={"pW": 5000, "cW": 1000}),
        "/v1/info/sensors": FakeResponse(payload=[]),
    })
    assert client.read(NOON).soc_pct is None


# --- finding the SM ID -------------------------------------------------------------


def test_discovery_reports_whichever_endpoint_answers():
    client = solar_client({
        "/v1/info/users": FakeResponse(payload=[{"smId": "SM-42", "name": "Home"}]),
        "/v1/users": FakeResponse(status_code=404),
        "/v1/info/user": FakeResponse(status_code=404),
        "/v3/users": FakeResponse(status_code=404),
        "/v1/gateways": FakeResponse(status_code=404),
        "/v1/info/gateways": FakeResponse(status_code=404),
        "/v3/auth/refresh": FakeResponse(status_code=404),
    })
    findings = dict(client.discover_sm_id())
    assert findings["/v1/info/users"] == [{"smId": "SM-42", "name": "Home"}]


def test_discovery_reads_the_claims_out_of_an_exchanged_token():
    """A JWT usually names the installation it was minted for."""
    import base64 as b64
    import json as js

    def encode(obj):
        return b64.urlsafe_b64encode(js.dumps(obj).encode()).decode().rstrip("=")

    token = f"{encode({'alg': 'HS256'})}.{encode({'sub': 'user-1', 'smId': 'SM-42'})}.sig"
    client = solar_client({
        "/v3/auth/refresh": FakeResponse(payload={"access_token": token}),
        "/v1/": FakeResponse(status_code=404),
        "/v3/users": FakeResponse(status_code=404),
    })
    findings = dict(client.discover_sm_id())
    assert findings["access token claims"]["smId"] == "SM-42"


def test_discovery_returns_nothing_rather_than_failing_when_all_doors_are_shut():
    client = solar_client({
        "/v3/auth/refresh": FakeResponse(status_code=404),
        "/v1/": FakeResponse(status_code=404),
        "/v3/users": FakeResponse(status_code=404),
    })
    assert client.discover_sm_id() == []


def test_discovery_leaves_the_client_usable_afterwards():
    """It fiddles with auth state to try the exchange; it must put it back."""
    # Routes are matched by substring in order, so the stream must come first:
    # "/v3/users" is a prefix of "/v3/users/{smId}/data/stream".
    client = solar_client({
        "/data/stream": FakeResponse(payload=STREAM),
        "/v1/info/sensors": FakeResponse(payload=SENSORS),
        "/v3/auth/refresh": FakeResponse(status_code=404),
        "/v1/": FakeResponse(status_code=404),
        "/v3/users": FakeResponse(status_code=404),
    })
    client.discover_sm_id()
    assert client.read(NOON).surplus_w == 4800
