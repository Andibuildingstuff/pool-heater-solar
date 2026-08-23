"""Configuration: defaults from the spec, every one overridable by environment.

Tunables live in the workflow file as plain `env:` entries so they can be edited
without touching code. Credentials come from the platform secret store and are
kept in a separate object so the config can be logged safely.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from zoneinfo import ZoneInfo

from .models import Mode

TRUE_WORDS = {"1", "true", "yes", "on", "y"}
FALSE_WORDS = {"0", "false", "no", "off", "n"}


class ConfigError(ValueError):
    """Raised when an environment variable cannot be understood."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a number") from exc


def _int(name: str, default: int) -> int:
    value = _float(name, default)
    if value != int(value):
        raise ConfigError(f"{name}={value!r} must be a whole number")
    return int(value)


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    lowered = raw.lower()
    if lowered in TRUE_WORDS:
        return True
    if lowered in FALSE_WORDS:
        return False
    raise ConfigError(f"{name}={raw!r} is not a yes/no value")


def parse_clock(raw: str, name: str) -> time:
    """Parse 'HH:MM' into a time. Accepts '9:05' as well as '09:05'."""
    parts = raw.split(":")
    if len(parts) != 2:
        raise ConfigError(f"{name}={raw!r} must look like HH:MM")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} must look like HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ConfigError(f"{name}={raw!r} is not a valid time of day")
    return time(hour, minute)


def parse_day(raw: str, name: str) -> tuple[int, int]:
    """Parse a season boundary. Accepts '01 May', '1 May', '05-01' and '05/01'."""
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    cleaned = raw.strip().replace("/", "-").replace(".", "-")
    words = cleaned.split()
    if len(words) == 2 and words[1][:3].lower() in months:
        try:
            day = int(words[0])
        except ValueError as exc:
            raise ConfigError(f"{name}={raw!r}: {words[0]!r} is not a day") from exc
        month = months[words[1][:3].lower()]
    elif "-" in cleaned:
        first, _, second = cleaned.partition("-")
        try:
            month, day = int(first), int(second)
        except ValueError as exc:
            raise ConfigError(f"{name}={raw!r} must look like '01 May' or '05-01'") from exc
    else:
        raise ConfigError(f"{name}={raw!r} must look like '01 May' or '05-01'")

    if not 1 <= month <= 12:
        raise ConfigError(f"{name}={raw!r}: month {month} is out of range")
    # 29 February is allowed; the comparison is on (month, day) ordinals only.
    days_in_month = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    if not 1 <= day <= days_in_month:
        raise ConfigError(f"{name}={raw!r}: day {day} is out of range for month {month}")
    return month, day


def _mode_map() -> dict[Mode, int]:
    """Map named modes onto the heat pump's `st` field.

    0 (Boost) and 1 (Silent/EcoSilence) are confirmed for this API family. The
    Smart value is a best guess -- run `probe-zodiac` with the app set to Smart
    and correct ZODIAC_MODE_SMART if it reports something else.
    """
    return {
        Mode.BOOST: _int("ZODIAC_MODE_BOOST", 0),
        Mode.ECOSILENCE: _int("ZODIAC_MODE_ECOSILENCE", 1),
        Mode.SMART: _int("ZODIAC_MODE_SMART", 2),
    }


@dataclass(frozen=True)
class Credentials:
    """Secrets. Never logged, never written to the state file."""

    solar_api_key: str | None = None
    solar_email: str | None = None
    solar_password: str | None = None
    solar_sm_id: str | None = None
    zodiac_email: str | None = None
    zodiac_password: str | None = None
    zodiac_serial: str | None = None
    telegram_token: str | None = None
    telegram_chat_id: str | None = None

    @classmethod
    def from_env(cls) -> "Credentials":
        return cls(
            solar_api_key=_env("SOLAR_MANAGER_API_KEY"),
            solar_email=_env("SOLAR_MANAGER_EMAIL"),
            solar_password=_env("SOLAR_MANAGER_PASSWORD"),
            solar_sm_id=_env("SOLAR_MANAGER_SM_ID"),
            zodiac_email=_env("ZODIAC_EMAIL"),
            zodiac_password=_env("ZODIAC_PASSWORD"),
            zodiac_serial=_env("ZODIAC_SERIAL"),
            telegram_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        )

    def any_configured(self) -> bool:
        """True if someone has started setting this up.

        Distinguishes "not configured yet" from "configured wrongly". The first
        is the normal state of a freshly created repository and is not worth
        alarming about; the second is a real fault worth failing on.
        """
        return any((
            self.solar_api_key, self.solar_email, self.solar_password,
            self.solar_sm_id, self.zodiac_email, self.zodiac_password,
            self.zodiac_serial,
        ))

    def missing_for_control(self) -> list[str]:
        """Names of the secrets a live control run cannot proceed without."""
        missing = []
        if not (self.solar_api_key or (self.solar_email and self.solar_password)):
            missing.append("SOLAR_MANAGER_API_KEY (or SOLAR_MANAGER_EMAIL+PASSWORD)")
        if not self.solar_sm_id:
            missing.append("SOLAR_MANAGER_SM_ID")
        if not self.zodiac_email:
            missing.append("ZODIAC_EMAIL")
        if not self.zodiac_password:
            missing.append("ZODIAC_PASSWORD")
        if not self.zodiac_serial:
            missing.append("ZODIAC_SERIAL")
        return missing


@dataclass(frozen=True)
class Config:
    """Every tunable in the spec, with the spec's defaults."""

    # --- switching thresholds -------------------------------------------------
    on_threshold_w: float = 3000.0
    heater_draw_w: float = 2000.0
    import_threshold_w: float = 300.0
    discharge_threshold_w: float = 500.0
    soc_floor_pct: float = 90.0

    # --- debounce and compressor protection -----------------------------------
    on_delay_min: float = 10.0
    off_delay_min: float = 10.0
    min_run_min: float = 30.0
    min_off_min: float = 30.0
    min_samples: int = 2

    # --- safety rails ---------------------------------------------------------
    max_switches_per_day: int = 3
    start_grace_min: float = 10.0
    max_failed_starts_per_day: int = 2
    hard_off_start: time = time(20, 0)
    hard_off_end: time = time(10, 0)

    # --- season ---------------------------------------------------------------
    season_start: tuple[int, int] = (5, 1)
    season_end: tuple[int, int] = (9, 30)
    off_season_mode: str = "monitor"
    force_off_season: bool = False

    # --- refinements ----------------------------------------------------------
    ecosilence_enabled: bool = False
    car_priority: bool = True
    car_active_w: float = 3000.0
    car_priority_margin_w: float = 1000.0
    setpoint_c: float | None = None

    # --- cadence --------------------------------------------------------------
    cycle_interval_min: float = 5.0
    max_sample_gap_min: float = 15.0
    reconcile_interval_min: float = 30.0
    off_season_poll_min: float = 60.0

    # --- operations -----------------------------------------------------------
    timezone: str = "Europe/Zurich"
    dry_run: bool = True
    mode_map: dict[Mode, int] = field(default_factory=lambda: {Mode.BOOST: 0, Mode.ECOSILENCE: 1, Mode.SMART: 2})

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def on_mode(self) -> Mode:
        """The mode the heater is commanded into when surplus is strong."""
        return Mode.BOOST

    def validate(self) -> None:
        if self.on_threshold_w <= 0:
            raise ConfigError("ON_THRESHOLD must be positive")
        if self.heater_draw_w <= 0:
            raise ConfigError("HEATER_DRAW_W must be positive")
        if self.on_threshold_w < self.heater_draw_w:
            # Starting the heater on less surplus than it consumes pulls the
            # difference from the grid. Taking power that was going into the
            # battery never does that -- the battery just charges more slowly --
            # so this one comparison is what keeps "never import" true.
            raise ConfigError(
                f"ON_THRESHOLD ({self.on_threshold_w:.0f} W) is below "
                f"HEATER_DRAW_W ({self.heater_draw_w:.0f} W): starting on that "
                "little surplus would import the difference from the grid"
            )
        if self.min_samples < 1:
            raise ConfigError("MIN_SAMPLES must be at least 1")
        if self.min_off_min < 0:
            raise ConfigError("MIN_OFF cannot be negative")
        if self.max_switches_per_day < 0:
            raise ConfigError("MAX_SWITCHES_PER_DAY cannot be negative")
        if self.off_season_mode not in {"monitor", "dormant"}:
            raise ConfigError("OFF_SEASON_MODE must be 'monitor' or 'dormant'")
        if not 0 <= self.soc_floor_pct <= 100:
            raise ConfigError("SOC_FLOOR must be a percentage between 0 and 100")
        if self.hard_off_start == self.hard_off_end:
            raise ConfigError("HARD_OFF_START and HARD_OFF_END must differ")
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:  # pragma: no cover - depends on tzdata
            raise ConfigError(f"TIMEZONE={self.timezone!r} is not a known zone") from exc

    @classmethod
    def from_env(cls) -> "Config":
        config = cls(
            on_threshold_w=_float("ON_THRESHOLD", 3000.0),
            heater_draw_w=_float("HEATER_DRAW_W", 2000.0),
            import_threshold_w=_float("IMPORT_THRESHOLD", 300.0),
            discharge_threshold_w=_float("DISCHARGE_THRESHOLD", 500.0),
            soc_floor_pct=_float("SOC_FLOOR", 90.0),
            on_delay_min=_float("ON_DELAY", 10.0),
            off_delay_min=_float("OFF_DELAY", 10.0),
            min_run_min=_float("MIN_RUN", 30.0),
            min_off_min=_float("MIN_OFF", 30.0),
            min_samples=_int("MIN_SAMPLES", 2),
            max_switches_per_day=_int("MAX_SWITCHES_PER_DAY", 3),
            start_grace_min=_float("START_GRACE", 10.0),
            max_failed_starts_per_day=_int("MAX_FAILED_STARTS_PER_DAY", 2),
            hard_off_start=parse_clock(_env("HARD_OFF_START", "20:00"), "HARD_OFF_START"),
            hard_off_end=parse_clock(_env("HARD_OFF_END", "10:00"), "HARD_OFF_END"),
            season_start=parse_day(_env("SEASON_START", "01 May"), "SEASON_START"),
            season_end=parse_day(_env("SEASON_END", "30 Sep"), "SEASON_END"),
            off_season_mode=(_env("OFF_SEASON_MODE", "monitor") or "monitor").lower(),
            force_off_season=_bool("FORCE_OFF_SEASON", False),
            ecosilence_enabled=_bool("ECOSILENCE_ENABLED", False),
            car_priority=_bool("CAR_PRIORITY", True),
            car_active_w=_float("CAR_ACTIVE_W", 3000.0),
            car_priority_margin_w=_float("CAR_PRIORITY_MARGIN_W", 1000.0),
            setpoint_c=(_float("SETPOINT_C", -1.0) if _env("SETPOINT_C") else None),
            cycle_interval_min=_float("CYCLE_INTERVAL_MIN", 5.0),
            max_sample_gap_min=_float("MAX_SAMPLE_GAP_MIN", 15.0),
            reconcile_interval_min=_float("RECONCILE_INTERVAL_MIN", 30.0),
            off_season_poll_min=_float("OFF_SEASON_POLL_MIN", 60.0),
            timezone=_env("TIMEZONE", "Europe/Zurich") or "Europe/Zurich",
            dry_run=_bool("DRY_RUN", True),
            mode_map=_mode_map(),
        )
        config.validate()
        return config
