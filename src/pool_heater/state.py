"""Persisted state, and the JSON file it lives in.

The file is written to a dedicated `pool-heater-state` branch so the project's
own history does not fill up with one commit every five minutes. Nothing secret
is ever stored here -- the repository is public, and auth tokens are re-minted
each run rather than cached to disk.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from pathlib import Path
from typing import Any

STATE_VERSION = 1


def to_iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def from_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


@dataclass
class State:
    """Everything the control loop needs to remember between runs."""

    version: int = STATE_VERSION

    # What we last told the heater to do, and when.
    commanded_on: bool = False
    commanded_mode: str | None = None
    last_on_at: datetime | None = None
    last_off_at: datetime | None = None

    # Debounce tracking. `*_since` is when the condition first became true in an
    # unbroken run; `*_samples` counts the readings that agreed since then.
    surplus_since: datetime | None = None
    surplus_samples: int = 0
    offcond_since: datetime | None = None
    offcond_samples: int = 0
    last_reading_at: datetime | None = None

    # Daily switch budget, reset on the local calendar day.
    budget_day: str | None = None
    switch_ons_today: int = 0

    # What the heater itself last reported, and when we last asked.
    device_on: bool | None = None
    device_mode: str | None = None
    last_shadow_at: datetime | None = None

    # Season handling and alerting.
    season_active: bool | None = None
    last_off_season_check_at: datetime | None = None
    consecutive_failures: int = 0
    failsafe_off_sent: bool = False
    last_alert_at: datetime | None = None

    notes: dict[str, Any] = field(default_factory=dict)

    # -- (de)serialisation -----------------------------------------------------

    _DATETIME_FIELDS = (
        "last_on_at", "last_off_at", "surplus_since", "offcond_since",
        "last_reading_at", "last_shadow_at", "last_off_season_check_at",
        "last_alert_at",
    )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for name in self._DATETIME_FIELDS:
            data[name] = to_iso(getattr(self, name))
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "State":
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                continue
            kwargs[key] = from_iso(value) if key in cls._DATETIME_FIELDS else value
        state = cls(**kwargs)
        state.version = STATE_VERSION
        return state

    # -- debounce helpers ------------------------------------------------------

    def note_surplus(self, now: datetime, holding: bool) -> None:
        """Advance or reset the 'surplus has held' streak."""
        if holding:
            if self.surplus_since is None:
                self.surplus_since = now
                self.surplus_samples = 0
            self.surplus_samples += 1
        else:
            self.surplus_since = None
            self.surplus_samples = 0

    def note_offcond(self, now: datetime, holding: bool) -> None:
        """Advance or reset the 'reason to switch off has held' streak."""
        if holding:
            if self.offcond_since is None:
                self.offcond_since = now
                self.offcond_samples = 0
            self.offcond_samples += 1
        else:
            self.offcond_since = None
            self.offcond_samples = 0

    def clear_streaks(self) -> None:
        self.surplus_since = None
        self.surplus_samples = 0
        self.offcond_since = None
        self.offcond_samples = 0

    def roll_budget(self, today: date) -> None:
        """Reset the daily switch budget when the local date changes."""
        key = today.isoformat()
        if self.budget_day != key:
            self.budget_day = key
            self.switch_ons_today = 0

    def record_on(self, now: datetime, mode: str | None) -> None:
        self.commanded_on = True
        self.commanded_mode = mode
        self.last_on_at = now
        self.device_on = True
        self.device_mode = mode
        self.switch_ons_today += 1
        self.clear_streaks()

    def record_off(self, now: datetime) -> None:
        self.commanded_on = False
        self.commanded_mode = None
        self.last_off_at = now
        self.device_on = False
        self.clear_streaks()


class StateStore:
    """Reads and writes the state JSON, atomically."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def load(self) -> State:
        if not self.path.exists():
            return State()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt state file must not wedge the loop. Starting from a
            # clean slate is safe: the heater is only ever switched on after a
            # fresh debounce period, and the reconcile read re-syncs the rest.
            return State()
        if not isinstance(data, dict):
            return State()
        return State.from_dict(data)

    def save(self, state: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_dict(), indent=2, sort_keys=True) + "\n"
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, delete=False
        ) as handle:
            handle.write(payload)
            temp_name = handle.name
        os.replace(temp_name, self.path)
