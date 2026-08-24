"""The runner: which APIs get called, when, and what gets remembered."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from conftest import NOON, ZURICH, discharging, importing, surplus
from pool_heater.config import Config, Credentials
from pool_heater.models import Action, HeaterState, Mode
from pool_heater.runner import Runner
from pool_heater.solar_manager import SolarManagerError
from pool_heater.state import State, StateStore
from pool_heater.zodiac import ZodiacError, ZodiacRateLimited

CREDENTIALS = Credentials(
    solar_api_key="k", solar_sm_id="sm1",
    zodiac_email="a@b.ch", zodiac_password="pw", zodiac_serial="S1",
    telegram_token="t", telegram_chat_id="c",
)


class FakeSolar:
    def __init__(self, reading=None, error=None):
        self.reading, self.error, self.reads = reading, error, 0

    def read(self, now):
        self.reads += 1
        if self.error:
            raise SolarManagerError(self.error)
        return replace(self.reading, taken_at=now)


class FakeZodiac:
    def __init__(self, state=None, error=None):
        self.state = state or HeaterState(on=False)
        self.error, self.calls, self.reads = error, [], 0

    def read_state(self):
        self.reads += 1
        if self.error:
            raise ZodiacError(self.error)
        return self.state

    def turn_on(self, mode, setpoint=None):
        self.calls.append(("on", mode, setpoint))
        self.state = HeaterState(on=True, mode=mode)

    def turn_off(self):
        self.calls.append(("off",))
        self.state = HeaterState(on=False)

    def set_mode(self, mode):
        self.calls.append(("mode", mode))
        self.state = HeaterState(on=self.state.on, mode=mode)


class FakeNotifier:
    def __init__(self):
        self.messages = []
        self.configured = True

    def send(self, text):
        self.messages.append(text)
        return True


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state.json")


def build(store, config=None, solar=None, zodiac=None, now=NOON, seeded=None):
    config = config or Config(dry_run=False)
    if seeded is not None:
        store.save(seeded)
    runner = Runner(
        config,
        CREDENTIALS,
        store,
        solar=solar or FakeSolar(surplus(5000)),
        zodiac=zodiac or FakeZodiac(),
        notifier=FakeNotifier(),
        now=now,
    )
    return runner


def primed(now=NOON, *, on=False, ons_today=0) -> State:
    """State that has already seen enough surplus to satisfy the on-delay."""
    state = State()
    state.season_active = True
    state.budget_day = now.date().isoformat()
    state.switch_ons_today = ons_today
    state.surplus_since = now - timedelta(minutes=10)
    state.surplus_samples = 2
    state.last_reading_at = now - timedelta(minutes=5)
    state.device_on = on
    state.commanded_on = on
    if on:
        state.last_on_at = now - timedelta(minutes=90)
        state.device_mode = "boost"
    return state


# --- dry run vs live --------------------------------------------------------------


def test_dry_run_decides_but_never_commands(store):
    zodiac = FakeZodiac()
    runner = build(store, config=Config(dry_run=True), zodiac=zodiac, seeded=primed())
    result = runner.run_once()

    assert result.decision.action is Action.TURN_ON
    assert result.applied is False
    assert zodiac.calls == []
    assert any("dry run" in message for message in runner.notifier.messages)


def test_dry_run_does_not_spend_the_switching_budget(store):
    runner = build(store, config=Config(dry_run=True), seeded=primed())
    runner.run_once()
    assert store.load().switch_ons_today == 0


def test_a_live_start_commands_the_heater_and_records_it(store):
    zodiac = FakeZodiac()
    runner = build(store, zodiac=zodiac, seeded=primed())
    result = runner.run_once()

    assert result.applied is True
    assert zodiac.calls == [("on", Mode.BOOST, None)]

    saved = store.load()
    assert saved.commanded_on is True
    assert saved.switch_ons_today == 1
    assert saved.last_on_at == NOON
    assert any("switch ON" in message for message in runner.notifier.messages)


def test_the_budget_rolls_before_a_start_is_counted(store):
    """Regression: rolling the day after recording would zero the count."""
    yesterday = primed(NOON - timedelta(days=1), ons_today=3)
    yesterday.surplus_since = NOON - timedelta(minutes=10)
    yesterday.last_reading_at = NOON - timedelta(minutes=5)
    runner = build(store, seeded=yesterday)
    runner.run_once()

    saved = store.load()
    assert saved.budget_day == NOON.date().isoformat()
    assert saved.switch_ons_today == 1


def test_a_live_stop_commands_off(store):
    state = primed(on=True)
    state.offcond_since = NOON - timedelta(minutes=10)
    state.offcond_samples = 2
    runner = build(
        store, solar=FakeSolar(importing(1500)), zodiac=FakeZodiac(HeaterState(on=True)),
        seeded=state,
    )
    result = runner.run_once()

    assert result.decision.action is Action.TURN_OFF
    assert runner.zodiac.calls == [("off",)]
    assert store.load().commanded_on is False


# --- how often the heat pump is polled ----------------------------------------------


def test_the_shadow_is_not_read_on_a_quiet_cycle(store):
    """iAquaLink rate-limits, so a no-change cycle must not touch it."""
    state = primed()
    state.surplus_samples = 0
    state.surplus_since = None
    state.last_shadow_at = NOON - timedelta(minutes=5)
    zodiac = FakeZodiac()
    runner = build(store, solar=FakeSolar(surplus(500)), zodiac=zodiac, seeded=state)
    runner.run_once()

    assert zodiac.reads == 0
    assert runner.solar.reads == 1


def test_the_shadow_is_read_before_any_command(store):
    state = primed()
    state.last_shadow_at = NOON - timedelta(minutes=5)
    zodiac = FakeZodiac()
    runner = build(store, zodiac=zodiac, seeded=state)
    runner.run_once()

    assert zodiac.reads == 1
    assert zodiac.calls == [("on", Mode.BOOST, None)]


def test_the_shadow_is_re_read_on_the_reconcile_interval(store):
    state = primed()
    state.surplus_samples = 0
    state.surplus_since = None
    state.last_shadow_at = NOON - timedelta(minutes=45)
    zodiac = FakeZodiac()
    runner = build(store, solar=FakeSolar(surplus(500)), zodiac=zodiac, seeded=state)
    runner.run_once()

    assert zodiac.reads == 1
    assert store.load().last_shadow_at == NOON


def test_a_heater_someone_started_by_hand_is_noticed_on_reconcile(store):
    """The device says on, we thought off: the fresh read wins, and the
    off-delay then runs its normal course rather than switching on the spot."""
    state = primed()
    state.surplus_samples = 0
    state.surplus_since = None
    state.last_shadow_at = NOON - timedelta(minutes=45)
    state.last_on_at = NOON - timedelta(minutes=90)
    zodiac = FakeZodiac(HeaterState(on=True, mode=Mode.BOOST))

    first = build(store, solar=FakeSolar(importing(2000)), zodiac=zodiac, seeded=state)
    first_result = first.run_once()

    assert store.load().device_on is True, "the drift should be recorded, not ignored"
    assert first_result.decision.action is Action.NONE
    assert zodiac.calls == []

    later = NOON + timedelta(minutes=5)
    second = build(store, solar=FakeSolar(importing(2000)), zodiac=zodiac, now=later)
    second_result = second.run_once()

    assert second_result.decision.action is Action.TURN_OFF
    assert zodiac.calls == [("off",)]
    assert store.load().device_on is False


# --- out of hours and out of season -------------------------------------------------


def test_after_the_evening_ceiling_a_running_heater_is_stopped_at_once(store):
    evening = datetime(2026, 7, 15, 20, 5, tzinfo=ZURICH)
    state = primed(evening, on=True)
    zodiac = FakeZodiac(HeaterState(on=True))
    runner = build(store, zodiac=zodiac, now=evening, seeded=state)
    result = runner.run_once()

    assert result.decision.action is Action.TURN_OFF
    assert zodiac.calls == [("off",)]


def test_overnight_the_apis_are_left_alone_once_the_heater_is_known_off(store):
    night = datetime(2026, 7, 16, 3, 0, tzinfo=ZURICH)
    state = primed(night)
    state.last_off_season_check_at = night - timedelta(minutes=10)
    zodiac, solar = FakeZodiac(), FakeSolar(surplus(0))
    runner = build(store, solar=solar, zodiac=zodiac, now=night, seeded=state)
    result = runner.run_once()

    assert result.skipped is True
    assert zodiac.reads == 0 and solar.reads == 0


def test_overnight_the_heater_is_still_verified_hourly(store):
    night = datetime(2026, 7, 16, 3, 0, tzinfo=ZURICH)
    state = primed(night)
    state.last_off_season_check_at = night - timedelta(minutes=75)
    zodiac = FakeZodiac(HeaterState(on=True))
    runner = build(store, zodiac=zodiac, now=night, seeded=state)
    result = runner.run_once()

    assert zodiac.reads == 1
    assert result.decision.action is Action.TURN_OFF
    assert zodiac.calls == [("off",)]


def test_out_of_season_a_heater_found_running_is_switched_off_and_alerted(store):
    december = datetime(2026, 12, 10, 13, 0, tzinfo=ZURICH)
    state = primed(december)
    state.season_active = False
    zodiac = FakeZodiac(HeaterState(on=True))
    runner = build(store, zodiac=zodiac, now=december, seeded=state)
    result = runner.run_once()

    assert result.decision.action is Action.TURN_OFF
    assert zodiac.calls == [("off",)]
    assert any("out of season" in m or "season" in m for m in runner.notifier.messages)


def test_out_of_season_no_solar_reading_is_taken_at_all(store):
    december = datetime(2026, 12, 10, 13, 0, tzinfo=ZURICH)
    state = primed(december)
    state.season_active = False
    solar = FakeSolar(surplus(9000))
    runner = build(store, solar=solar, now=december, seeded=state)
    runner.run_once()
    assert solar.reads == 0


def test_dormant_mode_makes_no_api_calls_whatsoever(store):
    december = datetime(2026, 12, 10, 13, 0, tzinfo=ZURICH)
    state = primed(december)
    state.season_active = False
    zodiac, solar = FakeZodiac(HeaterState(on=True)), FakeSolar(surplus(9000))
    runner = build(
        store,
        config=Config(dry_run=False, off_season_mode="dormant"),
        solar=solar, zodiac=zodiac, now=december, seeded=state,
    )
    result = runner.run_once()

    assert result.skipped is True
    assert zodiac.reads == 0 and solar.reads == 0 and zodiac.calls == []


def test_the_start_of_the_season_is_announced(store):
    state = primed()
    state.season_active = False
    runner = build(store, config=Config(dry_run=True), seeded=state)
    runner.run_once()
    assert any("resuming" in message for message in runner.notifier.messages)


def test_the_end_of_the_season_is_announced_once(store):
    december = datetime(2026, 12, 10, 13, 0, tzinfo=ZURICH)
    state = primed(december)
    state.season_active = True
    state.last_off_season_check_at = december - timedelta(minutes=5)
    runner = build(store, now=december, seeded=state)
    runner.run_once()

    assert any("season has ended" in message for message in runner.notifier.messages)
    assert store.load().season_active is False


# --- failing safe ---------------------------------------------------------------------


def test_a_solar_outage_switches_a_running_heater_off_and_alerts(store):
    state = primed(on=True)
    zodiac = FakeZodiac(HeaterState(on=True))
    runner = build(
        store, solar=FakeSolar(error="gateway offline"), zodiac=zodiac, seeded=state
    )
    result = runner.run_once()

    assert result.ok is False
    assert zodiac.calls == [("off",)]
    assert any("problem" in message for message in runner.notifier.messages)


def test_a_solar_outage_with_the_heater_off_alerts_but_commands_nothing(store):
    zodiac = FakeZodiac()
    runner = build(
        store, solar=FakeSolar(error="gateway offline"), zodiac=zodiac, seeded=primed()
    )
    runner.run_once()
    assert zodiac.calls == []
    assert runner.notifier.messages


def test_an_outage_does_not_repeat_the_alert_every_cycle(store):
    state = primed()
    state.failsafe_off_sent = True
    runner = build(
        store, solar=FakeSolar(error="gateway offline"), seeded=state
    )
    runner.run_once()
    assert runner.notifier.messages == []


def test_the_failure_count_climbs_across_cycles(store):
    state = primed()
    state.consecutive_failures = 4
    runner = build(store, solar=FakeSolar(error="still offline"), seeded=state)
    runner.run_once()
    assert store.load().consecutive_failures == 5


def test_state_is_saved_even_when_the_cycle_fails(store):
    runner = build(store, solar=FakeSolar(error="offline"), seeded=primed())
    runner.run_once()
    assert store.load().consecutive_failures == 1


def test_a_hand_started_heater_still_gets_its_compressor_minimum(store):
    """We do not know when someone started it, so the clock starts when we see it."""
    state = primed()
    state.surplus_samples = 0
    state.surplus_since = None
    state.last_on_at = None
    state.last_shadow_at = NOON - timedelta(minutes=45)
    zodiac = FakeZodiac(HeaterState(on=True, mode=Mode.BOOST))

    build(store, solar=FakeSolar(importing(2000)), zodiac=zodiac, seeded=state).run_once()
    assert store.load().last_on_at == NOON

    # Ten minutes later the off-delay is satisfied, but the compressor is not.
    later = NOON + timedelta(minutes=10)
    result = build(
        store, solar=FakeSolar(importing(2000)), zodiac=zodiac, now=later
    ).run_once()
    assert result.decision.action is Action.NONE
    assert "compressor minimum" in result.decision.reason
    assert zodiac.calls == []


# --- did the start actually take? ---------------------------------------------------


def running(status=2):
    return HeaterState(on=True, mode=Mode.BOOST, status=status)


def commanded_on(now=NOON, minutes_ago=12, ons_today=1):
    state = primed(now, on=True, ons_today=ons_today)
    state.last_on_at = now - timedelta(minutes=minutes_ago)
    state.start_verified = False
    state.last_shadow_at = now - timedelta(minutes=minutes_ago)
    return state


def test_a_start_that_took_is_confirmed_and_not_rechecked(store):
    zodiac = FakeZodiac(running())
    runner = build(store, solar=FakeSolar(surplus(6000)), zodiac=zodiac, seeded=commanded_on())
    runner.run_once()

    saved = store.load()
    assert saved.start_verified is True
    assert saved.commanded_on is True
    assert zodiac.calls == []


def test_a_heater_switched_on_but_not_running_is_switched_off_and_alerted(store):
    """state 1 with status 0: told to run, heating nothing. Usually no water flow."""
    zodiac = FakeZodiac(HeaterState(on=True, mode=Mode.BOOST, status=0))
    runner = build(store, solar=FakeSolar(surplus(6000)), zodiac=zodiac, seeded=commanded_on())
    result = runner.run_once()

    assert result.decision.action is Action.TURN_OFF
    assert zodiac.calls == [("off",)]
    assert "filter pump" in result.decision.reason
    assert any("failed to start" in m or "not running" in m for m in runner.notifier.messages)


def test_a_failed_start_refunds_the_switching_cycle(store):
    """A pump that starts later should not find the budget already spent."""
    zodiac = FakeZodiac(HeaterState(on=True, status=0))
    runner = build(store, solar=FakeSolar(surplus(6000)), zodiac=zodiac,
                   seeded=commanded_on(ons_today=1))
    runner.run_once()

    saved = store.load()
    assert saved.switch_ons_today == 0
    assert saved.failed_starts_today == 1


def test_repeated_failed_starts_stop_being_refunded(store):
    """A real fault must not retry all afternoon."""
    state = commanded_on(ons_today=2)
    state.failed_starts_today = 2
    zodiac = FakeZodiac(HeaterState(on=True, status=0))
    runner = build(store, solar=FakeSolar(surplus(6000)), zodiac=zodiac, seeded=state)
    runner.run_once()

    saved = store.load()
    assert saved.failed_starts_today == 3
    assert saved.switch_ons_today == 2, "the third failure keeps its cycle"


def test_the_check_waits_for_the_grace_period(store):
    """A heat pump stages up over minutes; checking at once would always fail."""
    zodiac = FakeZodiac(HeaterState(on=True, status=0))
    state = commanded_on(minutes_ago=3)
    state.last_shadow_at = NOON
    runner = build(store, solar=FakeSolar(surplus(6000)), zodiac=zodiac, seeded=state)
    runner.run_once()

    assert zodiac.calls == [], "too early to judge"
    assert store.load().start_verified is False


def test_a_verified_start_is_not_re_verified_every_cycle(store):
    state = commanded_on()
    state.start_verified = True
    state.last_shadow_at = NOON
    zodiac = FakeZodiac(running())
    runner = build(store, solar=FakeSolar(surplus(6000)), zodiac=zodiac, seeded=state)
    runner.run_once()
    assert zodiac.reads == 0


def test_a_fresh_start_clears_the_previous_verification(store):
    state = primed()
    state.start_verified = True
    runner = build(store, seeded=state)
    runner.run_once()
    saved = store.load()
    assert saved.commanded_on is True and saved.start_verified is False


# --- rate limiting is not an outage --------------------------------------------------


class RateLimitedZodiac(FakeZodiac):
    def read_state(self):
        self.reads += 1
        raise ZodiacRateLimited("iAquaLink rate-limited the shadow read")


def test_rate_limiting_skips_the_cycle_rather_than_failing_safe(store):
    """A 429 says the cloud is fine and busy, not that the heater is unreachable."""
    state = commanded_on()
    zodiac = RateLimitedZodiac(running())
    runner = build(store, solar=FakeSolar(surplus(6000)), zodiac=zodiac, seeded=state)
    result = runner.run_once()

    assert result.skipped is True
    assert result.ok is True, "a throttled read must not fail the run"
    assert zodiac.calls == [], "switching off would be another call to the same API"
    assert runner.notifier.messages == []


def test_rate_limiting_leaves_the_commanded_state_untouched(store):
    state = commanded_on()
    runner = build(
        store, solar=FakeSolar(surplus(6000)), zodiac=RateLimitedZodiac(), seeded=state
    )
    runner.run_once()

    saved = store.load()
    assert saved.commanded_on is True
    assert saved.start_verified is False
    assert saved.consecutive_failures == 0


def test_a_genuine_outage_still_fails_safe(store):
    """The distinction matters: unreachable is not the same as throttled."""
    state = primed(on=True)
    zodiac = FakeZodiac(HeaterState(on=True), error="connection reset")
    runner = build(store, solar=FakeSolar(surplus(6000)), zodiac=zodiac, seeded=state)
    result = runner.run_once()
    assert result.ok is False


# --- one message about a situation, not ninety --------------------------------------


def test_the_same_notification_is_not_repeated_every_cycle(store):
    """Dry run cannot record its switch, so the decision repeats; the mail must not."""
    evening = datetime(2026, 7, 15, 20, 5, tzinfo=ZURICH)
    state = primed(evening, on=True)
    runner = build(store, config=Config(dry_run=True),
                   zodiac=FakeZodiac(HeaterState(on=True)), now=evening, seeded=state)
    runner.run_once()
    first_count = len(runner.notifier.messages)
    assert first_count >= 1

    later = evening + timedelta(minutes=5)
    second = build(store, config=Config(dry_run=True),
                   zodiac=FakeZodiac(HeaterState(on=True)), now=later)
    second.run_once()
    assert second.notifier.messages == [], "the identical message must be suppressed"


def test_a_different_message_still_gets_through_after_a_repeat(store):
    state = primed()
    state.notes["last_notified"] = "[dry run] would switch OFF: outside the 10:00-20:00 run window"
    runner = build(store, config=Config(dry_run=True), seeded=state)
    runner.run_once()
    assert any("would switch ON" in m for m in runner.notifier.messages), (
        "a message with different content is not a repeat"
    )
