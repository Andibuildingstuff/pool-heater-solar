"""A whole simulated day, cycle by cycle, checked against the acceptance criteria.

This is the closest thing to a rehearsal that can run without the real APIs: a
west-facing PV curve, a house load, a battery, and the actual Runner driving an
actual state file across 288 five-minute cycles.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from pool_heater.config import Config, Credentials
from pool_heater.models import HeaterState, Mode, Reading
from pool_heater.runner import Runner
from pool_heater.state import StateStore

ZURICH = ZoneInfo("Europe/Zurich")
CREDENTIALS = Credentials(
    solar_api_key="k", solar_sm_id="sm", zodiac_email="e",
    zodiac_password="p", zodiac_serial="s",
)

HEATER_DRAW_W = 3000.0
BASE_LOAD_W = 800.0
BATTERY_MAX_CHARGE_W = 3000.0
BATTERY_CAPACITY_WH = 10_000.0


class House:
    """A crude but honest energy balance: PV, a base load, a battery, the grid."""

    def __init__(self, peak_w: float = 9000.0, peak_hour: float = 15.5, width: float = 3.4):
        self.peak_w, self.peak_hour, self.width = peak_w, peak_hour, width
        self.soc_wh = 4000.0
        self.cloud = 1.0

    def pv_w(self, moment: datetime) -> float:
        hour = moment.hour + moment.minute / 60.0
        # West-facing: the curve leans late, and there is nothing before dawn.
        value = self.peak_w * math.exp(-((hour - self.peak_hour) ** 2) / (2 * self.width**2))
        return 0.0 if hour < 7.0 or hour > 21.0 else value * self.cloud

    def step(self, moment: datetime, heater_on: bool) -> Reading:
        pv = self.pv_w(moment)
        load = BASE_LOAD_W + (HEATER_DRAW_W if heater_on else 0.0)
        net = pv - load

        charge = discharge = export = importing = 0.0
        if net > 0:
            headroom = BATTERY_CAPACITY_WH - self.soc_wh
            charge = min(net, BATTERY_MAX_CHARGE_W, headroom * 12)
            export = net - charge
            self.soc_wh = min(BATTERY_CAPACITY_WH, self.soc_wh + charge / 12)
        else:
            want = -net
            available = max(0.0, self.soc_wh - 0.2 * BATTERY_CAPACITY_WH)
            discharge = min(want, available * 12)
            importing = want - discharge
            self.soc_wh = max(0.0, self.soc_wh - discharge / 12)

        return Reading(
            taken_at=moment,
            pv_w=pv,
            consumption_w=load,
            grid_import_w=importing,
            grid_export_w=export,
            battery_charge_w=charge,
            battery_discharge_w=discharge,
            soc_pct=100.0 * self.soc_wh / BATTERY_CAPACITY_WH,
        )


class SimZodiac:
    """Stands in for the heat pump, and writes down every transition."""

    def __init__(self):
        self.state = HeaterState(on=False)
        self.transitions: list[tuple[datetime, str]] = []
        self.now: datetime | None = None
        self.reads = 0

    def read_state(self):
        self.reads += 1
        return self.state

    def turn_on(self, mode, setpoint=None):
        self.state = HeaterState(on=True, mode=mode)
        self.transitions.append((self.now, "on"))

    def turn_off(self):
        self.state = HeaterState(on=False)
        self.transitions.append((self.now, "off"))

    def set_mode(self, mode):
        self.state = HeaterState(on=self.state.on, mode=mode)


class SimSolar:
    def __init__(self, house: House, zodiac: SimZodiac):
        self.house, self.zodiac, self.reads = house, zodiac, 0

    def read(self, now):
        self.reads += 1
        return self.house.step(now, self.zodiac.state.on)


class SilentNotifier:
    configured = True

    def __init__(self):
        self.messages = []

    def send(self, text):
        self.messages.append(text)
        return True


def run_day(tmp_path, house: House, config: Config, day=datetime(2026, 7, 15, tzinfo=ZURICH)):
    store = StateStore(tmp_path / "state.json")
    zodiac, notifier = SimZodiac(), SilentNotifier()
    solar = SimSolar(house, zodiac)

    for step in range(288):  # 24h at five-minute cycles
        moment = day + timedelta(minutes=5 * step)
        zodiac.now = moment
        if step == 150:
            house.cloud = house.cloud  # placeholder for weather changes in variants
        Runner(
            config, CREDENTIALS, store,
            solar=solar, zodiac=zodiac, notifier=notifier, now=moment,
        ).run_once()

    return zodiac, store.load(), notifier


def runs_from(transitions, end_of_day):
    """Pair the transitions up into (start, end) heater runs."""
    runs, start = [], None
    for moment, kind in transitions:
        if kind == "on" and start is None:
            start = moment
        elif kind == "off" and start is not None:
            runs.append((start, moment))
            start = None
    if start is not None:
        runs.append((start, end_of_day))
    return runs


@pytest.fixture
def live_config():
    return Config(dry_run=False)


# --- a clear day ------------------------------------------------------------------


def test_a_clear_day_runs_the_heater_in_the_export_window(tmp_path, live_config):
    zodiac, state, _ = run_day(tmp_path, House(), live_config)
    day_end = datetime(2026, 7, 16, tzinfo=ZURICH)
    runs = runs_from(zodiac.transitions, day_end)

    assert runs, "a clear July day should have produced at least one run"
    for start, end in runs:
        assert start.hour >= 10, f"run started at {start:%H:%M}, before the window opens"
        assert end <= start.replace(hour=20, minute=0), f"run ended at {end:%H:%M}, past the ceiling"


def test_no_run_is_shorter_than_the_compressor_minimum(tmp_path, live_config):
    zodiac, _, _ = run_day(tmp_path, House(), live_config)
    day_end = datetime(2026, 7, 16, tzinfo=ZURICH)
    for start, end in runs_from(zodiac.transitions, day_end):
        minutes = (end - start).total_seconds() / 60
        assert minutes >= live_config.min_run_min, f"{minutes:.0f} min run starting {start:%H:%M}"


def test_the_daily_switching_budget_is_respected(tmp_path, live_config):
    zodiac, state, _ = run_day(tmp_path, House(), live_config)
    starts = [moment for moment, kind in zodiac.transitions if kind == "on"]
    assert len(starts) <= live_config.max_switches_per_day
    assert state.switch_ons_today == len(starts)


def test_the_heater_is_off_by_the_end_of_the_day(tmp_path, live_config):
    zodiac, _, _ = run_day(tmp_path, House(), live_config)
    assert zodiac.state.on is False
    assert zodiac.transitions[-1][1] == "off"


def test_the_heat_pump_is_not_polled_once_per_cycle(tmp_path, live_config):
    """288 cycles must not mean 288 shadow reads -- iAquaLink rate-limits."""
    zodiac, _, _ = run_day(tmp_path, House(), live_config)
    assert zodiac.reads < 60, f"{zodiac.reads} shadow reads in a day is too many"


# --- a day that never gets going -----------------------------------------------------


def test_a_dull_day_never_starts_the_heater(tmp_path, live_config):
    dull = House(peak_w=3200)  # never clears the 3 kW start threshold net of load
    zodiac, state, _ = run_day(tmp_path, dull, live_config)
    assert zodiac.transitions == []
    assert state.switch_ons_today == 0


# --- a broken cloudy day --------------------------------------------------------------


class SteepEveningHouse(House):
    """A realistic west-facing day: strong afternoon, production gone by 18:00."""

    def pv_w(self, moment: datetime) -> float:
        hour = moment.hour + moment.minute / 60.0
        if hour < 8.0 or hour > 18.0:
            return 0.0
        return super().pv_w(moment.replace(hour=int(hour), minute=moment.minute)) * (
            1.0 if hour < 16.5 else max(0.0, (18.0 - hour) / 1.5) ** 2
        )


def test_the_heater_stops_when_the_surplus_ends_not_at_the_ceiling(tmp_path, live_config):
    """Acceptance: off within ~15 min of the surplus ending, well before 20:00."""
    zodiac, _, _ = run_day(tmp_path, SteepEveningHouse(peak_w=9000), live_config)
    day_end = datetime(2026, 7, 16, tzinfo=ZURICH)
    runs = runs_from(zodiac.transitions, day_end)

    assert runs, "the steep-evening day should still have produced a run"
    last_end = runs[-1][1]
    assert last_end.hour < 19, (
        f"the heater ran until {last_end:%H:%M}; the surplus-based stop should have "
        "fired long before the 20:00 ceiling"
    )


class FlickeringHouse(House):
    """Sun and cloud alternating every ten minutes: the flapping scenario."""

    def pv_w(self, moment: datetime) -> float:
        clear = super().pv_w(moment)
        return clear if (moment.hour * 60 + moment.minute) % 20 < 10 else clear * 0.15


def test_a_flickering_day_does_not_burn_through_the_budget(tmp_path, live_config):
    zodiac, state, _ = run_day(tmp_path, FlickeringHouse(), live_config)
    starts = [moment for moment, kind in zodiac.transitions if kind == "on"]
    assert len(starts) <= live_config.max_switches_per_day
    day_end = datetime(2026, 7, 16, tzinfo=ZURICH)
    for start, end in runs_from(zodiac.transitions, day_end):
        assert (end - start).total_seconds() / 60 >= live_config.min_run_min


def test_the_compressor_always_gets_its_rest_between_runs(tmp_path, live_config):
    zodiac, _, _ = run_day(tmp_path, FlickeringHouse(), live_config)
    day_end = datetime(2026, 7, 16, tzinfo=ZURICH)
    runs = runs_from(zodiac.transitions, day_end)
    for (_, previous_end), (next_start, _) in zip(runs, runs[1:]):
        rest = (next_start - previous_end).total_seconds() / 60
        assert rest >= live_config.min_off_min, f"only {rest:.0f} min of rest"


# --- out of season ----------------------------------------------------------------------


def test_a_bright_winter_day_never_starts_the_heater(tmp_path, live_config):
    zodiac, _, notifier = run_day(
        tmp_path, House(peak_w=9000), live_config, day=datetime(2026, 2, 10, tzinfo=ZURICH)
    )
    assert [kind for _, kind in zodiac.transitions if kind == "on"] == []


def test_out_of_season_a_heater_left_on_is_caught_within_the_hour(tmp_path, live_config):
    """The exact mistake the automation exists to prevent."""
    store = StateStore(tmp_path / "state.json")
    zodiac, notifier = SimZodiac(), SilentNotifier()
    house = House()
    solar = SimSolar(house, zodiac)
    winter = datetime(2026, 2, 10, 14, 0, tzinfo=ZURICH)

    # Someone switches it on from the app.
    zodiac.state = HeaterState(on=True, mode=Mode.BOOST)

    caught_at = None
    for step in range(13):  # just over an hour of cycles
        moment = winter + timedelta(minutes=5 * step)
        zodiac.now = moment
        Runner(
            live_config, CREDENTIALS, store,
            solar=solar, zodiac=zodiac, notifier=notifier, now=moment,
        ).run_once()
        if not zodiac.state.on:
            caught_at = moment
            break

    assert caught_at is not None, "the heater was never switched off"
    assert (caught_at - winter).total_seconds() / 60 <= live_config.off_season_poll_min
    assert any("season" in message.lower() for message in notifier.messages)
