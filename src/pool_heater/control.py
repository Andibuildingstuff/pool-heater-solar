"""The decision function and its safety rails.

Split in two on purpose:

* `update_streaks` folds this cycle's reading into the debounce counters. It is
  the only function here that mutates state.
* `decide` reads state and returns what should happen. It touches no clock, no
  network and no globals, so every scenario in the spec can be unit-tested by
  handing it a datetime and a Reading.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .config import Config
from .models import Action, Decision, HeaterState, Mode, Reading
from .state import State


# --- calendar and clock helpers ----------------------------------------------


def is_in_season(day: date, config: Config) -> bool:
    """True if `day` falls inside the configured pool season.

    Handles a season that wraps the new year (start after end), which is what a
    southern-hemisphere or year-round-with-winter-break setup would need.
    """
    start, end, today = config.season_start, config.season_end, (day.month, day.day)
    if start <= end:
        return start <= today <= end
    return today >= start or today <= end


def within_run_window(moment: time, config: Config) -> bool:
    """True if the heater is allowed to run at this local time of day."""
    opens, closes = config.hard_off_end, config.hard_off_start
    if opens < closes:
        return opens <= moment < closes
    # Window wraps midnight, e.g. opens 22:00 and closes 02:00.
    return moment >= opens or moment < closes


def next_hard_off(now: datetime, config: Config) -> datetime:
    """The next moment the hard-off ceiling closes the window, at or after `now`."""
    ceiling = now.replace(
        hour=config.hard_off_start.hour,
        minute=config.hard_off_start.minute,
        second=0,
        microsecond=0,
    )
    if ceiling <= now:
        ceiling += timedelta(days=1)
    return ceiling


# --- reading interpretation ---------------------------------------------------


def on_threshold_for(reading: Reading, config: Config) -> float:
    """The surplus the heater must see before it may start.

    Raised while the car is charging hard. The raw surplus figure already nets
    the car out -- power going into the Easee is neither exported nor stored --
    so this margin is not double-counting. It is headroom, so the heater does
    not claim the last watt of surplus and leave the charger to ramp into the
    grid a minute later.
    """
    if config.car_priority and reading.car_w >= config.car_active_w:
        return config.on_threshold_w + config.car_priority_margin_w
    return config.on_threshold_w


def surplus_holding(reading: Reading, config: Config) -> bool:
    return reading.surplus_w >= on_threshold_for(reading, config)


def off_condition(reading: Reading, config: Config) -> tuple[bool, str]:
    """Is the house paying for the heater? Returns (yes/no, why)."""
    if reading.grid_import_w > config.import_threshold_w:
        return True, f"importing {reading.grid_import_w:.0f} W from the grid"
    draining = reading.battery_discharge_w > config.discharge_threshold_w
    below_floor = reading.soc_pct is None or reading.soc_pct < config.soc_floor_pct
    if draining and below_floor:
        soc = "unknown" if reading.soc_pct is None else f"{reading.soc_pct:.0f}%"
        return True, f"battery discharging {reading.battery_discharge_w:.0f} W at {soc} SoC"
    return False, ""


# --- debounce -----------------------------------------------------------------


def effective_delay_min(delay_min: float, config: Config) -> float:
    """Convert a wall-clock delay into the elapsed time a streak must span.

    The spec asks for "N consecutive cycles", which on a punctual 5-minute
    schedule means the second sample of a 10-minute delay arrives 5 minutes
    after the first. Requiring a full 10 minutes of elapsed time would silently
    turn a 2-cycle delay into 3. So the bar is: at least `min_samples` agreeing
    readings, spanning at least one cycle less than the nominal delay. On a
    punctual schedule that is exactly the spec's cycle count; when GitHub's cron
    fires early or bunches runs together, the elapsed-time floor stops a burst
    of samples from counting as ten minutes of evidence.
    """
    return max(0.0, delay_min - config.cycle_interval_min)


def streak_held(
    since: datetime | None, samples: int, now: datetime, delay_min: float, config: Config
) -> bool:
    if since is None or samples < config.min_samples:
        return False
    elapsed_min = (now - since).total_seconds() / 60.0
    return elapsed_min >= effective_delay_min(delay_min, config)


def update_streaks(state: State, now: datetime, reading: Reading, config: Config) -> None:
    """Fold this cycle's reading into the debounce counters.

    A long gap between readings -- a missed cron, an API outage -- breaks the
    chain. We cannot claim a condition held continuously across time we did not
    observe, so the streaks start again.
    """
    if state.last_reading_at is not None:
        gap_min = (now - state.last_reading_at).total_seconds() / 60.0
        if gap_min > config.max_sample_gap_min or gap_min < 0:
            state.clear_streaks()

    state.note_surplus(now, surplus_holding(reading, config))
    state.note_offcond(now, off_condition(reading, config)[0])
    state.last_reading_at = now


# --- the decision --------------------------------------------------------------


def decide(
    now: datetime,
    reading: Reading,
    heater: HeaterState,
    state: State,
    config: Config,
) -> Decision:
    """Decide what to do with the heater, given a fresh reading and the streaks.

    Rails are checked outermost-first: season, then the hard-off window, then
    the running-heater cases, then starting up. Anything that forces the heater
    off wins over anything that would keep it on.
    """
    # -- Rail 1: the pool season. No ON command exists outside it. -------------
    off_season_reason = None
    if config.force_off_season:
        off_season_reason = "FORCE_OFF_SEASON is set"
    elif not is_in_season(now.date(), config):
        off_season_reason = (
            f"outside the {config.season_start[1]:02d}/{config.season_start[0]:02d}"
            f"-{config.season_end[1]:02d}/{config.season_end[0]:02d} season"
        )
    if off_season_reason:
        if heater.on:
            return Decision(
                Action.TURN_OFF,
                f"{off_season_reason}, but the heater is running",
                notify=True,
                warnings=("heater was running out of season -- switched off",),
            )
        return Decision(Action.NONE, f"{off_season_reason}; heater is off")

    # -- Rail 2: the hard-off window. -----------------------------------------
    if not within_run_window(now.time(), config):
        window = f"{config.hard_off_end:%H:%M}-{config.hard_off_start:%H:%M}"
        if heater.on:
            return Decision(
                Action.TURN_OFF,
                f"outside the {window} run window",
                notify=True,
            )
        return Decision(Action.NONE, f"outside the {window} run window; heater is off")

    # -- The heater is running: should it stop, or change gear? ---------------
    if heater.on:
        should_stop, why = off_condition(reading, config)
        if should_stop and streak_held(
            state.offcond_since, state.offcond_samples, now, config.off_delay_min, config
        ):
            held_back = _min_run_remaining(now, state, config)
            if held_back is not None:
                return Decision(
                    Action.NONE,
                    f"{why}, but holding {held_back:.0f} more min for the "
                    f"{config.min_run_min:.0f} min compressor minimum",
                )
            return Decision(Action.TURN_OFF, why, notify=True)

        if should_stop:
            return Decision(
                Action.NONE,
                f"{why}; waiting for the {config.off_delay_min:.0f} min off-delay "
                f"({state.offcond_samples} reading(s) so far)",
            )

        if config.ecosilence_enabled:
            gear = _modulation(reading, heater, config)
            if gear is not None:
                return gear

        return Decision(
            Action.NONE,
            f"running on {reading.surplus_w:.0f} W surplus",
        )

    # -- The heater is off: may it start? -------------------------------------
    if not surplus_holding(reading, config):
        threshold = on_threshold_for(reading, config)
        return Decision(
            Action.NONE,
            f"surplus {reading.surplus_w:.0f} W is below the {threshold:.0f} W start threshold",
        )

    if not streak_held(
        state.surplus_since, state.surplus_samples, now, config.on_delay_min, config
    ):
        return Decision(
            Action.NONE,
            f"surplus {reading.surplus_w:.0f} W, waiting for the "
            f"{config.on_delay_min:.0f} min on-delay ({state.surplus_samples} reading(s) so far)",
        )

    # Anti-short-cycling. The compressor minimum has a mirror image: restarting
    # a heat pump minutes after stopping it is as hard on the unit as cutting a
    # run short, and on a flickering day it is also the fastest way to spend the
    # switching budget before the afternoon has begun.
    if state.last_off_at is not None and config.min_off_min > 0:
        rest_min = (now - state.last_off_at).total_seconds() / 60.0
        if 0 <= rest_min < config.min_off_min:
            return Decision(
                Action.NONE,
                f"surplus is there, but the compressor has only rested "
                f"{rest_min:.0f} of {config.min_off_min:.0f} min",
            )

    if state.switch_ons_today >= config.max_switches_per_day:
        return Decision(
            Action.NONE,
            f"surplus is there, but today's {config.max_switches_per_day}-cycle "
            f"switching budget is spent",
            warnings=(
                f"switching budget exhausted ({state.switch_ons_today} starts today); "
                "not starting again until tomorrow",
            ),
        )

    # Never start a run the hard-off ceiling would have to cut short. This is
    # what keeps the ceiling and the compressor minimum from contradicting each
    # other at the end of the day.
    minutes_left = (next_hard_off(now, config) - now).total_seconds() / 60.0
    if minutes_left < config.min_run_min:
        return Decision(
            Action.NONE,
            f"surplus is there, but only {minutes_left:.0f} min remain before the "
            f"{config.hard_off_start:%H:%M} ceiling -- too short for a "
            f"{config.min_run_min:.0f} min run",
        )

    mode = config.on_mode
    if config.ecosilence_enabled and reading.surplus_w < config.on_threshold_w:
        mode = Mode.ECOSILENCE
    return Decision(
        Action.TURN_ON,
        f"surplus {reading.surplus_w:.0f} W held for "
        f"{_streak_minutes(state.surplus_since, now):.0f} min",
        mode=mode,
        notify=True,
    )


def _min_run_remaining(now: datetime, state: State, config: Config) -> float | None:
    """Minutes still owed to the compressor minimum, or None if it is satisfied."""
    if state.last_on_at is None:
        return None
    run_min = (now - state.last_on_at).total_seconds() / 60.0
    if run_min >= config.min_run_min:
        return None
    return config.min_run_min - run_min


def _streak_minutes(since: datetime | None, now: datetime) -> float:
    if since is None:
        return 0.0
    return (now - since).total_seconds() / 60.0


def _modulation(reading: Reading, heater: HeaterState, config: Config) -> Decision | None:
    """Optional refinement: ride out a thin surplus in EcoSilence instead of stopping.

    Only reachable with ECOSILENCE_ENABLED. A mode change is not a switching
    cycle, so it does not spend the daily budget.
    """
    thin = 0 < reading.surplus_w < config.on_threshold_w
    if thin and heater.mode is not Mode.ECOSILENCE:
        return Decision(
            Action.SET_MODE,
            f"surplus thinned to {reading.surplus_w:.0f} W -- easing off to EcoSilence",
            mode=Mode.ECOSILENCE,
        )
    if reading.surplus_w >= config.on_threshold_w and heater.mode is not Mode.BOOST:
        return Decision(
            Action.SET_MODE,
            f"surplus back to {reading.surplus_w:.0f} W -- back to Boost",
            mode=Mode.BOOST,
        )
    return None


def start_check_due(now: datetime, state: State, config: Config) -> bool:
    """Is it time to confirm that a start we commanded actually took?"""
    if not state.commanded_on or state.start_verified or state.last_on_at is None:
        return False
    elapsed_min = (now - state.last_on_at).total_seconds() / 60.0
    return elapsed_min >= config.start_grace_min


def failed_to_start(state: State, config: Config) -> Decision:
    """The heater was told to run and, given time, reports that it is not.

    The usual cause is no water flow: a pool heat pump will not start unless the
    filter pump is circulating, and that pump is outside this project's control.
    Switching off rather than leaving it commanded-on keeps the record honest --
    and refunding the cycle means a pump that starts later in the day still gets
    used, up to a limit, so a persistent fault cannot retry all afternoon.
    """
    return Decision(
        Action.TURN_OFF,
        f"commanded on {config.start_grace_min:.0f} min ago but the unit reports "
        "it is not running -- check the filter pump is circulating",
        notify=True,
        warnings=("heater failed to start; switching off",),
    )


def failsafe_decision(state: State, error: str) -> Decision:
    """What to do when a reading or the heater could not be reached.

    Fail safe means fail off, but only once: re-sending OFF every five minutes
    through a multi-hour outage would bury the alert that matters.
    """
    if state.commanded_on or state.device_on:
        return Decision(
            Action.TURN_OFF,
            f"failing safe after an API error: {error}",
            notify=True,
            warnings=(f"API unreachable: {error}",),
        )
    return Decision(
        Action.NONE,
        f"API error, heater already off: {error}",
        notify=not state.failsafe_off_sent,
        warnings=(f"API unreachable: {error}",),
    )
