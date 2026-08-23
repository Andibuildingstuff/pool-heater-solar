"""The internal loop that gives five-minute control from an hourly trigger."""

from __future__ import annotations

import pytest

from pool_heater.models import Action, Decision
from pool_heater.runner import CycleResult, run_loop


class FakeClock:
    """A clock that only moves when something sleeps."""

    def __init__(self):
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def work(self, seconds: float) -> None:
        self.now += seconds


class FakeRunner:
    def __init__(self, clock: FakeClock, results=None, work_s: float = 0.0):
        self.clock, self.work_s = clock, work_s
        self.results = list(results or [])
        self.calls = 0

    def run_once(self) -> CycleResult:
        self.calls += 1
        self.clock.work(self.work_s)
        if self.results:
            outcome = self.results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return CycleResult(Decision(Action.NONE, "quiet"))


def test_an_hour_of_looping_is_twelve_five_minute_cycles():
    clock = FakeClock()
    runner = FakeRunner(clock)
    cycles, ok = run_loop(runner, 55, 5, sleeper=clock.sleep, clock=clock)

    assert cycles == 12
    assert ok == 12
    assert clock.slept == [300.0] * 11


def test_a_failing_cycle_does_not_end_the_loop():
    """An hour of missed control is worse than one bad reading."""
    clock = FakeClock()
    runner = FakeRunner(clock, results=[
        CycleResult(Decision(Action.NONE, "fine")),
        RuntimeError("solar manager fell over"),
        CycleResult(Decision(Action.NONE, "fine")),
    ])
    cycles, ok = run_loop(runner, 10, 5, sleeper=clock.sleep, clock=clock)

    assert cycles == 3
    assert ok == 2


def test_a_cycle_reporting_an_error_is_counted_but_not_fatal():
    clock = FakeClock()
    runner = FakeRunner(clock, results=[CycleResult(Decision(Action.NONE, "x"), error="boom")])
    cycles, ok = run_loop(runner, 0, 5, sleeper=clock.sleep, clock=clock)
    assert (cycles, ok) == (1, 0)


def test_a_slow_cycle_does_not_push_the_later_ones_out_of_step():
    """Sleep to the next tick, not for a fixed period, or the loop drifts."""
    clock = FakeClock()
    runner = FakeRunner(clock, work_s=60.0)
    run_loop(runner, 20, 5, sleeper=clock.sleep, clock=clock)

    assert all(pytest.approx(240.0) == slept for slept in clock.slept[:-1])


def test_a_cycle_slower_than_the_interval_never_sleeps_negative():
    clock = FakeClock()
    runner = FakeRunner(clock, work_s=400.0)
    run_loop(runner, 20, 5, sleeper=clock.sleep, clock=clock)
    assert all(slept >= 0 for slept in clock.slept)


def test_the_last_sleep_never_overruns_the_budget():
    clock = FakeClock()
    runner = FakeRunner(clock)
    run_loop(runner, 12, 5, sleeper=clock.sleep, clock=clock)
    assert clock.now <= 12 * 60


def test_a_zero_minute_budget_still_runs_one_cycle():
    """The scheduler may fire late; one reading beats none."""
    clock = FakeClock()
    runner = FakeRunner(clock)
    cycles, ok = run_loop(runner, 0, 5, sleeper=clock.sleep, clock=clock)
    assert (cycles, ok) == (1, 1)
    assert clock.slept == []
