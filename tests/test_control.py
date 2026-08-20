"""The decision function, exercised against the scenarios in the spec."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from conftest import NOON, ZURICH, at, discharging, feed, importing, surplus
from pool_heater import control
from pool_heater.config import Config
from pool_heater.models import Action, HeaterState, Mode, Reading
from pool_heater.state import State

OFF = HeaterState(on=False)
ON = HeaterState(on=True, mode=Mode.BOOST)


def decide(now, reading, heater, state, config):
    return control.decide(now, reading, heater, state, config)


# --- starting up ---------------------------------------------------------------


def test_strong_surplus_starts_the_heater_after_the_on_delay(config, state):
    reading, now = feed(state, config, [surplus(4000)] * 2)
    decision = decide(now, reading, OFF, state, config)
    assert decision.action is Action.TURN_ON
    assert decision.mode is Mode.BOOST
    assert decision.notify


def test_a_single_good_reading_is_not_enough(config, state):
    reading, now = feed(state, config, [surplus(4000)])
    assert decide(now, reading, OFF, state, config).action is Action.NONE


def test_surplus_below_threshold_does_nothing(config, state):
    reading, now = feed(state, config, [surplus(2000)] * 4)
    decision = decide(now, reading, OFF, state, config)
    assert decision.action is Action.NONE
    assert "below" in decision.reason


def test_battery_charging_counts_as_surplus(config, state):
    """Soft surplus: power going into the battery is available to the heater."""
    charging = Reading(taken_at=NOON, pv_w=6000, battery_charge_w=3200, soc_pct=60)
    reading, now = feed(state, config, [charging] * 2)
    assert decide(now, reading, OFF, state, config).action is Action.TURN_ON


def test_a_flapping_cloudy_day_never_accumulates_a_streak(config, state):
    """Alternating sun and cloud must not spend a switching cycle."""
    pattern = [surplus(4000), surplus(500)] * 6
    reading, now = feed(state, config, pattern)
    assert decide(now, reading, OFF, state, config).action is Action.NONE
    assert state.surplus_samples == 0


def test_a_long_gap_between_readings_resets_the_streak(config, state):
    """A missed cron cannot be counted as evidence the surplus held."""
    feed(state, config, [surplus(4000)])
    control.update_streaks(state, at(45), surplus(4000, now=at(45)), config)
    assert state.surplus_samples == 1
    assert decide(at(45), surplus(4000, now=at(45)), OFF, state, config).action is Action.NONE


# --- stopping ------------------------------------------------------------------


def test_grid_import_stops_the_heater_once_the_minimum_run_is_served(config, state):
    state.last_on_at = NOON - timedelta(minutes=60)
    reading, now = feed(state, config, [importing(1200)] * 2)
    decision = decide(now, reading, ON, state, config)
    assert decision.action is Action.TURN_OFF
    assert "importing" in decision.reason


def test_the_compressor_minimum_holds_an_early_stop_back(config, state):
    state.last_on_at = NOON - timedelta(minutes=5)
    reading, now = feed(state, config, [importing(1200)] * 2)
    decision = decide(now, reading, ON, state, config)
    assert decision.action is Action.NONE
    assert "compressor minimum" in decision.reason


def test_battery_assist_near_full_charge_is_tolerated(config, state):
    """SOC_FLOOR exists so a brief top-up draw does not stop the heater."""
    reading, now = feed(state, config, [discharging(900, soc=95)] * 3)
    state.last_on_at = NOON - timedelta(minutes=60)
    assert decide(now, reading, ON, state, config).action is Action.NONE


def test_battery_draining_below_the_floor_stops_the_heater(config, state):
    state.last_on_at = NOON - timedelta(minutes=60)
    reading, now = feed(state, config, [discharging(900, soc=80)] * 2)
    decision = decide(now, reading, ON, state, config)
    assert decision.action is Action.TURN_OFF
    assert "battery discharging" in decision.reason


def test_a_small_discharge_below_the_threshold_is_ignored(config, state):
    state.last_on_at = NOON - timedelta(minutes=60)
    reading, now = feed(state, config, [discharging(200, soc=40)] * 3)
    assert decide(now, reading, ON, state, config).action is Action.NONE


def test_one_bad_reading_does_not_stop_the_heater(config, state):
    state.last_on_at = NOON - timedelta(minutes=60)
    reading, now = feed(state, config, [importing(1200)])
    decision = decide(now, reading, ON, state, config)
    assert decision.action is Action.NONE
    assert "off-delay" in decision.reason


# --- the switching budget -------------------------------------------------------


def test_the_daily_switch_budget_blocks_a_fourth_start(config, state):
    state.switch_ons_today = 3
    reading, now = feed(state, config, [surplus(5000)] * 3)
    decision = decide(now, reading, OFF, state, config)
    assert decision.action is Action.NONE
    assert "budget is spent" in decision.reason
    assert decision.warnings


def test_the_closing_off_is_always_allowed_even_with_the_budget_spent(config, state):
    """Spending the budget must never strand the heater in the ON state."""
    state.switch_ons_today = 3
    state.last_on_at = NOON - timedelta(minutes=60)
    reading, now = feed(state, config, [importing(1500)] * 2)
    assert decide(now, reading, ON, state, config).action is Action.TURN_OFF


# --- the hard-off window --------------------------------------------------------


def test_the_heater_is_switched_off_after_the_evening_ceiling(config, state):
    evening = datetime(2026, 7, 15, 20, 5, tzinfo=ZURICH)
    decision = decide(evening, surplus(4000, now=evening), ON, state, config)
    assert decision.action is Action.TURN_OFF
    assert "run window" in decision.reason


def test_no_start_before_the_morning_opening(config, state):
    morning = datetime(2026, 7, 15, 9, 30, tzinfo=ZURICH)
    reading, now = feed(state, config, [surplus(6000)] * 4, start=morning)
    assert decide(now, reading, OFF, state, config).action is Action.NONE


def test_the_ceiling_never_has_to_cut_a_run_short(config, state):
    """No start inside MIN_RUN of the ceiling, so the two rails cannot collide."""
    late = datetime(2026, 7, 15, 19, 45, tzinfo=ZURICH)
    reading, now = feed(state, config, [surplus(6000)] * 3, start=late - timedelta(minutes=10))
    decision = decide(now, reading, OFF, state, config)
    assert decision.action is Action.NONE
    assert "ceiling" in decision.reason


def test_a_start_is_still_allowed_with_room_for_a_full_run(config, state):
    early_evening = datetime(2026, 7, 15, 19, 0, tzinfo=ZURICH)
    reading, now = feed(
        state, config, [surplus(6000)] * 2, start=early_evening - timedelta(minutes=5)
    )
    assert decide(now, reading, OFF, state, config).action is Action.TURN_ON


# --- the season -----------------------------------------------------------------


def test_out_of_season_a_running_heater_is_switched_off_and_alerted(config, state):
    december = datetime(2026, 12, 10, 13, 0, tzinfo=ZURICH)
    decision = decide(december, surplus(8000, now=december), ON, state, config)
    assert decision.action is Action.TURN_OFF
    assert decision.notify
    assert decision.warnings


def test_out_of_season_no_amount_of_surplus_starts_the_heater(config, state):
    december = datetime(2026, 12, 10, 13, 0, tzinfo=ZURICH)
    reading, now = feed(state, config, [surplus(9000)] * 6, start=december)
    assert decide(now, reading, OFF, state, config).action is Action.NONE


def test_force_off_season_overrides_the_calendar(config, state):
    forced = replace(config, force_off_season=True)
    reading, now = feed(state, forced, [surplus(9000)] * 6)
    assert decide(now, reading, OFF, state, forced).action is Action.NONE


def test_a_season_that_wraps_the_new_year_is_understood():
    southern = Config(season_start=(11, 1), season_end=(3, 31))
    assert control.is_in_season(datetime(2026, 12, 25).date(), southern)
    assert control.is_in_season(datetime(2026, 2, 2).date(), southern)
    assert not control.is_in_season(datetime(2026, 6, 1).date(), southern)


def test_season_boundaries_are_inclusive(config):
    assert control.is_in_season(datetime(2026, 5, 1).date(), config)
    assert control.is_in_season(datetime(2026, 9, 30).date(), config)
    assert not control.is_in_season(datetime(2026, 4, 30).date(), config)
    assert not control.is_in_season(datetime(2026, 10, 1).date(), config)


# --- car priority ----------------------------------------------------------------


def test_a_charging_car_raises_the_bar_for_starting(config, state):
    """4 kW surplus normally starts the heater; not while the Easee is pulling 5 kW."""
    reading, now = feed(state, config, [surplus(3500, car_w=5000)] * 3)
    decision = decide(now, reading, OFF, state, config)
    assert decision.action is Action.NONE
    assert "4000 W start threshold" in decision.reason


def test_enough_surplus_starts_the_heater_even_with_the_car_charging(config, state):
    reading, now = feed(state, config, [surplus(4500, car_w=5000)] * 2)
    assert decide(now, reading, OFF, state, config).action is Action.TURN_ON


def test_car_priority_can_be_switched_off(config, state):
    relaxed = replace(config, car_priority=False)
    reading, now = feed(state, relaxed, [surplus(3500, car_w=5000)] * 2)
    assert decide(now, reading, OFF, state, relaxed).action is Action.TURN_ON


def test_a_trickle_charging_car_does_not_raise_the_bar(config, state):
    reading, now = feed(state, config, [surplus(3200, car_w=1400)] * 2)
    assert decide(now, reading, OFF, state, config).action is Action.TURN_ON


# --- the EcoSilence refinement ----------------------------------------------------


def test_modulation_is_off_by_default(config, state):
    state.last_on_at = NOON - timedelta(minutes=60)
    reading, now = feed(state, config, [surplus(1500)] * 3)
    assert decide(now, reading, ON, state, config).action is Action.NONE


def test_with_the_flag_on_a_thin_surplus_eases_off_instead_of_stopping(config, state):
    eco = replace(config, ecosilence_enabled=True)
    state.last_on_at = NOON - timedelta(minutes=60)
    reading, now = feed(state, eco, [surplus(1500)] * 3)
    decision = decide(now, reading, ON, state, eco)
    assert decision.action is Action.SET_MODE
    assert decision.mode is Mode.ECOSILENCE


def test_with_the_flag_on_a_recovered_surplus_goes_back_to_boost(config, state):
    eco = replace(config, ecosilence_enabled=True)
    state.last_on_at = NOON - timedelta(minutes=60)
    reading, now = feed(state, eco, [surplus(5000)] * 3)
    running_quiet = HeaterState(on=True, mode=Mode.ECOSILENCE)
    decision = decide(now, reading, running_quiet, state, eco)
    assert decision.action is Action.SET_MODE
    assert decision.mode is Mode.BOOST


def test_modulation_never_overrides_a_real_reason_to_stop(config, state):
    eco = replace(config, ecosilence_enabled=True)
    state.last_on_at = NOON - timedelta(minutes=60)
    reading, now = feed(state, eco, [importing(1500)] * 2)
    assert decide(now, reading, ON, state, eco).action is Action.TURN_OFF


# --- failing safe -----------------------------------------------------------------


def test_an_api_failure_switches_a_running_heater_off(state):
    state.commanded_on = True
    decision = control.failsafe_decision(state, "connection timed out")
    assert decision.action is Action.TURN_OFF
    assert decision.notify


def test_an_api_failure_with_the_heater_off_alerts_once(state):
    first = control.failsafe_decision(state, "connection timed out")
    assert first.action is Action.NONE
    assert first.notify
    state.failsafe_off_sent = True
    assert not control.failsafe_decision(state, "connection timed out").notify


# --- debounce arithmetic ------------------------------------------------------------


def test_a_ten_minute_delay_on_a_five_minute_schedule_means_two_readings(config):
    assert control.effective_delay_min(10.0, config) == 5.0


def test_bunched_up_runs_do_not_satisfy_the_delay(config, state):
    """Cron firing three times in ninety seconds is not ten minutes of evidence."""
    for offset in (0, 0.5, 1.0):
        control.update_streaks(state, at(offset), surplus(5000, now=at(offset)), config)
    assert state.surplus_samples == 3
    assert not control.streak_held(
        state.surplus_since, state.surplus_samples, at(1.0), config.on_delay_min, config
    )


def test_an_unknown_state_of_charge_is_treated_as_below_the_floor(config, state):
    """If we cannot tell how full the battery is, assume the cautious answer."""
    state.last_on_at = NOON - timedelta(minutes=60)
    blind = Reading(taken_at=NOON, battery_discharge_w=900, soc_pct=None)
    reading, now = feed(state, config, [blind] * 2)
    decision = decide(now, reading, ON, state, config)
    assert decision.action is Action.TURN_OFF
    assert "unknown" in decision.reason
