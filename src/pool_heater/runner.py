"""One pass of the control loop: read, decide, act, remember, tell.

Designed around two cost asymmetries:

* Solar Manager is our own gateway and cheap to poll, so it is read every cycle.
* The iAquaLink cloud rate-limits, so the heater's shadow is read only when
  there is a reason to: before commanding it, on a slow reconcile interval, and
  on the relaxed out-of-hours check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from . import control
from .config import Config, Credentials
from .models import Action, Decision, HeaterState, Mode, Reading
from .notify import Notifier
from .solar_manager import SolarManagerClient, SolarManagerError
from .state import State, StateStore
from .zodiac import ZodiacClient, ZodiacError

LOGGER = logging.getLogger(__name__)


@dataclass
class CycleResult:
    decision: Decision
    reading: Reading | None = None
    heater: HeaterState | None = None
    applied: bool = False
    error: str | None = None
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


def should_reconcile(state: State, now: datetime, config: Config) -> bool:
    """Is it time to ask the heater what it actually thinks its state is?"""
    if state.last_shadow_at is None:
        return True
    age_min = (now - state.last_shadow_at).total_seconds() / 60.0
    return age_min >= config.reconcile_interval_min or age_min < 0


def relaxed_check_due(state: State, now: datetime, config: Config) -> bool:
    """Out of season or out of hours, verify the heater at most hourly."""
    if state.last_off_season_check_at is None:
        return True
    age_min = (now - state.last_off_season_check_at).total_seconds() / 60.0
    return age_min >= config.off_season_poll_min or age_min < 0


def tracked_heater_state(state: State, config: Config) -> HeaterState:
    """What we believe the heater is doing, without asking it."""
    mode = None
    if state.device_mode:
        try:
            mode = Mode(state.device_mode)
        except ValueError:
            mode = None
    on = state.device_on if state.device_on is not None else state.commanded_on
    return HeaterState(on=bool(on), mode=mode)


class Runner:
    def __init__(
        self,
        config: Config,
        credentials: Credentials,
        store: StateStore,
        solar: SolarManagerClient | None = None,
        zodiac: ZodiacClient | None = None,
        notifier: Notifier | None = None,
        now: datetime | None = None,
    ):
        self.config = config
        self.credentials = credentials
        self.store = store
        self.solar = solar or SolarManagerClient(credentials, config)
        self.zodiac = zodiac or ZodiacClient(credentials, config)
        self.notifier = notifier or Notifier(credentials)
        self._now = now

    def now(self) -> datetime:
        return self._now or datetime.now(self.config.tz)

    # -- the cycle -------------------------------------------------------------

    def run_once(self) -> CycleResult:
        now = self.now()
        state = self.store.load()
        state.roll_budget(now.date())

        try:
            result = self._cycle(now, state)
        finally:
            self.store.save(state)
        return result

    def _cycle(self, now: datetime, state: State) -> CycleResult:
        in_season = not self.config.force_off_season and control.is_in_season(
            now.date(), self.config
        )
        self._announce_season(now, state, in_season)

        if not in_season and self.config.off_season_mode == "dormant":
            LOGGER.info("out of season and OFF_SEASON_MODE=dormant; doing nothing")
            return CycleResult(
                Decision(Action.NONE, "dormant out of season"), skipped=True
            )

        in_window = control.within_run_window(now.time(), self.config)
        believed = tracked_heater_state(state, self.config)

        # Out of season, or out of hours: no reading is needed to know the answer.
        # If we believe the heater is on, act on that immediately; otherwise just
        # verify occasionally, so the iAquaLink cloud is not polled all night.
        if not in_season or not in_window:
            return self._guard_cycle(now, state, believed, in_season)

        return self._control_cycle(now, state, believed)

    # -- out of season / out of hours -----------------------------------------

    def _guard_cycle(
        self, now: datetime, state: State, believed: HeaterState, in_season: bool
    ) -> CycleResult:
        heater = believed
        if not believed.on and not relaxed_check_due(state, now, self.config):
            reason = "out of season" if not in_season else "outside the run window"
            LOGGER.info("%s, heater believed off; next verification not due yet", reason)
            return CycleResult(Decision(Action.NONE, f"{reason}; nothing to do"), skipped=True)

        if not believed.on:
            # Due a verification read: this is the check that catches a heater
            # someone started from the app.
            try:
                heater = self.zodiac.read_state()
                state.last_shadow_at = now
                state.last_off_season_check_at = now
                state.device_on = heater.on
                state.device_mode = heater.mode.value if heater.mode else None
            except ZodiacError as exc:
                return self._handle_error(now, state, f"iAquaLink: {exc}")

        decision = control.decide(now, _no_surplus(now), heater, state, self.config)
        return self._apply(now, state, decision, None, heater)

    # -- in season, in hours ---------------------------------------------------

    def _control_cycle(self, now: datetime, state: State, believed: HeaterState) -> CycleResult:
        try:
            reading = self.solar.read(now)
        except SolarManagerError as exc:
            return self._handle_error(now, state, f"Solar Manager: {exc}")

        LOGGER.info(
            "PV %.0f W | consumption %.0f W | grid +%.0f/-%.0f W | battery +%.0f/-%.0f W "
            "| SoC %s | car %.0f W | surplus %.0f W",
            reading.pv_w, reading.consumption_w, reading.grid_export_w, reading.grid_import_w,
            reading.battery_charge_w, reading.battery_discharge_w,
            "n/a" if reading.soc_pct is None else f"{reading.soc_pct:.0f}%",
            reading.car_w, reading.surplus_w,
        )

        control.update_streaks(state, now, reading, self.config)

        heater = believed
        fresh = False
        if should_reconcile(state, now, self.config):
            try:
                heater = self.zodiac.read_state()
                fresh = True
                self._note_shadow(state, now, heater, believed)
            except ZodiacError as exc:
                return self._handle_error(now, state, f"iAquaLink: {exc}")

        decision = control.decide(now, reading, heater, state, self.config)

        # About to command the heater on tracked state alone? Confirm with the
        # device first -- a command built on a stale picture is how you get a
        # switching cycle spent on a no-op.
        if decision.is_change and not fresh:
            try:
                heater = self.zodiac.read_state()
                self._note_shadow(state, now, heater, believed)
            except ZodiacError as exc:
                return self._handle_error(now, state, f"iAquaLink: {exc}")
            decision = control.decide(now, reading, heater, state, self.config)

        return self._apply(now, state, decision, reading, heater)

    def _note_shadow(
        self, state: State, now: datetime, heater: HeaterState, believed: HeaterState
    ) -> None:
        state.last_shadow_at = now
        if heater.on != believed.on:
            LOGGER.warning(
                "heater state drifted: we believed %s, the device reports %s",
                "on" if believed.on else "off",
                "on" if heater.on else "off",
            )
            if heater.on and state.last_on_at is None:
                # Someone started it from the app and we have no idea when. Treat
                # now as the start so the compressor still gets its minimum run
                # rather than being stopped moments after it spun up.
                state.last_on_at = now
        state.device_on = heater.on
        state.device_mode = heater.mode.value if heater.mode else None
        if heater.water_temp_c is not None:
            LOGGER.info(
                "heater %s | water %.1f C | setpoint %s",
                "on" if heater.on else "off",
                heater.water_temp_c,
                "n/a" if heater.setpoint_c is None else f"{heater.setpoint_c:.1f} C",
            )

    # -- acting ----------------------------------------------------------------

    def _apply(
        self,
        now: datetime,
        state: State,
        decision: Decision,
        reading: Reading | None,
        heater: HeaterState,
    ) -> CycleResult:
        for warning in decision.warnings:
            LOGGER.warning("%s", warning)

        if not decision.is_change:
            LOGGER.info("no change: %s", decision.reason)
            state.consecutive_failures = 0
            state.failsafe_off_sent = False
            if decision.notify:
                self._notify(decision.reason)
            return CycleResult(decision, reading, heater)

        verb = {
            Action.TURN_ON: "switch ON",
            Action.TURN_OFF: "switch OFF",
            Action.SET_MODE: f"set mode {decision.mode.value if decision.mode else '?'}",
        }[decision.action]

        if self.config.dry_run:
            LOGGER.info("DRY RUN would %s -- %s", verb, decision.reason)
            state.consecutive_failures = 0
            self._notify(f"[dry run] would {verb}: {decision.reason}")
            return CycleResult(decision, reading, heater, applied=False)

        try:
            self._command(decision)
        except ZodiacError as exc:
            return self._handle_error(now, state, f"iAquaLink command failed: {exc}")

        LOGGER.info("%s -- %s", verb, decision.reason)
        if decision.action is Action.TURN_ON:
            state.record_on(now, (decision.mode or self.config.on_mode).value)
        elif decision.action is Action.TURN_OFF:
            state.record_off(now)
        elif decision.action is Action.SET_MODE and decision.mode:
            state.device_mode = decision.mode.value
            state.commanded_mode = decision.mode.value

        state.consecutive_failures = 0
        state.failsafe_off_sent = False
        self._notify(f"{verb}: {decision.reason}{self._context(reading, state)}")
        return CycleResult(decision, reading, heater, applied=True)

    def _command(self, decision: Decision) -> None:
        if decision.action is Action.TURN_ON:
            self.zodiac.turn_on(decision.mode or self.config.on_mode, self.config.setpoint_c)
        elif decision.action is Action.TURN_OFF:
            self.zodiac.turn_off()
        elif decision.action is Action.SET_MODE and decision.mode:
            self.zodiac.set_mode(decision.mode)

    def _context(self, reading: Reading | None, state: State) -> str:
        if reading is None:
            return ""
        return (
            f"\nsurplus {reading.surplus_w:.0f} W (export {reading.grid_export_w:.0f}"
            f" + battery {reading.battery_charge_w:.0f}), PV {reading.pv_w:.0f} W"
            f"\nstarts today: {state.switch_ons_today}/{self.config.max_switches_per_day}"
        )

    # -- failure ---------------------------------------------------------------

    def _handle_error(self, now: datetime, state: State, error: str) -> CycleResult:
        LOGGER.error("%s", error)
        state.consecutive_failures += 1
        decision = control.failsafe_decision(state, error)

        applied = False
        if decision.action is Action.TURN_OFF and not self.config.dry_run:
            try:
                self.zodiac.turn_off()
                state.record_off(now)
                state.failsafe_off_sent = True
                applied = True
            except ZodiacError as exc:
                LOGGER.error("fail-safe OFF could not be delivered: %s", exc)
        elif decision.action is Action.TURN_OFF:
            LOGGER.info("DRY RUN would send fail-safe OFF")
            state.failsafe_off_sent = True

        if decision.notify:
            self._notify(f"Pool heater automation problem\n{error}\n{decision.reason}")
            state.failsafe_off_sent = True
        return CycleResult(decision, error=error, applied=applied)

    # -- season announcements --------------------------------------------------

    def _announce_season(self, now: datetime, state: State, in_season: bool) -> None:
        if state.season_active == in_season:
            return
        first_ever = state.season_active is None
        state.season_active = in_season
        if in_season:
            message = (
                "Pool heater automation is armed for the season"
                f" ({'dry run' if self.config.dry_run else 'live control'})."
            )
            if not first_ever:
                message = "Pool season has started -- automation resuming. " + message
        else:
            message = (
                "Pool season has ended. The automation will not switch the heater on;"
                f" it will keep checking {'hourly' if self.config.off_season_mode == 'monitor' else 'nothing'}"
                " and switch it off if it finds it running."
            )
        LOGGER.info("%s", message)
        self._notify(message)

    def _notify(self, text: str) -> None:
        self.notifier.send(text)


def _no_surplus(now: datetime) -> Reading:
    """A zeroed reading, for the paths where the decision cannot depend on power."""
    return Reading(taken_at=now)
