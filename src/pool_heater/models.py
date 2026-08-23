"""Value types shared by the readers, the decision function and the runner.

Everything here is plain data. The decision function consumes these and nothing
else, which is what makes it testable without touching either cloud API.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class Action(enum.Enum):
    """What the runner should do to the heater this cycle."""

    NONE = "none"
    TURN_ON = "turn_on"
    TURN_OFF = "turn_off"
    SET_MODE = "set_mode"


class Mode(enum.Enum):
    """Heat pump operating modes, named as the iAquaLink app names them."""

    BOOST = "boost"
    SMART = "smart"
    ECOSILENCE = "ecosilence"


@dataclass(frozen=True)
class Reading:
    """One sample of house-level power, as read from Solar Manager.

    All powers are positive watts. Solar Manager reports import and export as
    separate non-negative figures, so we keep that shape rather than inventing a
    signed convention that would have to be un-picked again downstream.
    """

    taken_at: datetime
    pv_w: float = 0.0
    consumption_w: float = 0.0
    grid_import_w: float = 0.0
    grid_export_w: float = 0.0
    battery_charge_w: float = 0.0
    battery_discharge_w: float = 0.0
    soc_pct: float | None = None
    car_w: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def surplus_w(self) -> float:
        """Solar the house is not using: hard surplus plus soft surplus.

        Grid export is hard surplus. Power going into the battery is soft
        surplus -- it is available to the heater, at the cost of charging the
        battery more slowly.
        """
        return self.grid_export_w + self.battery_charge_w


@dataclass(frozen=True)
class HeaterState:
    """What the heat pump says about itself."""

    on: bool
    mode: Mode | None = None
    status: int | None = None
    water_temp_c: float | None = None
    air_temp_c: float | None = None
    setpoint_c: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class Decision:
    """The outcome of one pass through the control logic."""

    action: Action
    reason: str
    mode: Mode | None = None
    notify: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def is_change(self) -> bool:
        return self.action is not Action.NONE
