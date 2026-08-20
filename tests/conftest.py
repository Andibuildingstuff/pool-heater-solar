"""Shared fixtures: a fixed clock, a default config, and readings by intent."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from pool_heater.config import Config
from pool_heater.models import Reading
from pool_heater.state import State

ZURICH = ZoneInfo("Europe/Zurich")

# A clear July afternoon, comfortably inside both the season and the run window.
NOON = datetime(2026, 7, 15, 12, 0, tzinfo=ZURICH)


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def state() -> State:
    return State()


def at(minutes: float, base: datetime = NOON) -> datetime:
    return base + timedelta(minutes=minutes)


def surplus(watts: float, *, now: datetime = NOON, car_w: float = 0.0, soc: float = 95.0) -> Reading:
    """A reading with `watts` of genuine surplus, exported rather than stored."""
    return Reading(
        taken_at=now,
        pv_w=watts + 2000,
        consumption_w=2000,
        grid_export_w=watts,
        soc_pct=soc,
        car_w=car_w,
    )


def importing(watts: float, *, now: datetime = NOON, soc: float = 95.0) -> Reading:
    return Reading(
        taken_at=now, pv_w=0, consumption_w=watts, grid_import_w=watts, soc_pct=soc
    )


def discharging(watts: float, *, now: datetime = NOON, soc: float = 50.0) -> Reading:
    return Reading(
        taken_at=now, pv_w=0, consumption_w=watts, battery_discharge_w=watts, soc_pct=soc
    )


def feed(state: State, config: Config, readings, start: datetime = NOON, step: float = 5.0):
    """Push a sequence of readings through the streak tracker, 5 minutes apart.

    Returns the (reading, now) pair of the last sample, ready to hand to decide().
    """
    from pool_heater import control

    now = start
    last = None
    for index, reading in enumerate(readings):
        now = start + timedelta(minutes=step * index)
        reading = Reading(
            taken_at=now,
            pv_w=reading.pv_w,
            consumption_w=reading.consumption_w,
            grid_import_w=reading.grid_import_w,
            grid_export_w=reading.grid_export_w,
            battery_charge_w=reading.battery_charge_w,
            battery_discharge_w=reading.battery_discharge_w,
            soc_pct=reading.soc_pct,
            car_w=reading.car_w,
        )
        control.update_streaks(state, now, reading, config)
        last = (reading, now)
    return last
