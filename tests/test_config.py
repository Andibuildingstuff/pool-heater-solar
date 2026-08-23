"""Environment parsing. Defaults must match the spec exactly."""

from __future__ import annotations

from datetime import time

import pytest

from pool_heater.config import Config, ConfigError, Credentials, parse_clock, parse_day
from pool_heater.models import Mode


def test_the_defaults_are_the_ones_the_spec_asks_for(monkeypatch):
    for name in list(__import__("os").environ):
        if name.isupper() and name in {
            "ON_THRESHOLD", "IMPORT_THRESHOLD", "DISCHARGE_THRESHOLD", "SOC_FLOOR",
            "ON_DELAY", "OFF_DELAY", "MIN_RUN", "MAX_SWITCHES_PER_DAY", "DRY_RUN",
        }:
            monkeypatch.delenv(name, raising=False)
    config = Config.from_env()
    assert config.on_threshold_w == 3000
    assert config.import_threshold_w == 300
    assert config.discharge_threshold_w == 500
    assert config.soc_floor_pct == 90
    assert (config.on_delay_min, config.off_delay_min) == (10, 10)
    assert config.min_run_min == 30
    assert config.max_switches_per_day == 3
    assert (config.hard_off_start, config.hard_off_end) == (time(20, 0), time(10, 0))
    assert (config.season_start, config.season_end) == ((5, 1), (9, 30))
    assert config.off_season_mode == "monitor"
    assert config.timezone == "Europe/Zurich"


def test_control_defaults_to_dry_run(monkeypatch):
    """Nothing switches real hardware until DRY_RUN is explicitly turned off."""
    monkeypatch.delenv("DRY_RUN", raising=False)
    assert Config.from_env().dry_run is True


def test_environment_overrides_are_applied(monkeypatch):
    monkeypatch.setenv("ON_THRESHOLD", "2500")
    monkeypatch.setenv("HARD_OFF_START", "21:30")
    monkeypatch.setenv("SEASON_END", "15 Oct")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ECOSILENCE_ENABLED", "yes")
    config = Config.from_env()
    assert config.on_threshold_w == 2500
    assert config.hard_off_start == time(21, 30)
    assert config.season_end == (10, 15)
    assert config.dry_run is False
    assert config.ecosilence_enabled is True


@pytest.mark.parametrize("raw", ["01 May", "1 May", "05-01", "05/01"])
def test_season_dates_accept_the_obvious_spellings(raw):
    assert parse_day(raw, "SEASON_START") == (5, 1)


@pytest.mark.parametrize("raw", ["", "May", "32 May", "13-01", "nonsense"])
def test_a_bad_season_date_is_rejected_loudly(raw):
    with pytest.raises(ConfigError):
        parse_day(raw, "SEASON_START")


@pytest.mark.parametrize("raw", ["24:00", "10", "ten:00", "10:99"])
def test_a_bad_clock_time_is_rejected_loudly(raw):
    with pytest.raises(ConfigError):
        parse_clock(raw, "HARD_OFF_START")


def test_a_nonsense_number_is_rejected_rather_than_silently_defaulted(monkeypatch):
    monkeypatch.setenv("ON_THRESHOLD", "quite a lot")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_an_unknown_off_season_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("OFF_SEASON_MODE", "hibernate")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_an_unknown_timezone_is_rejected(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "Mars/Olympus_Mons")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_the_mode_map_can_be_corrected_from_the_environment(monkeypatch):
    monkeypatch.setenv("ZODIAC_MODE_SMART", "3")
    assert Config.from_env().mode_map[Mode.SMART] == 3


def test_missing_credentials_are_named_precisely(monkeypatch):
    for name in (
        "SOLAR_MANAGER_API_KEY", "SOLAR_MANAGER_EMAIL", "SOLAR_MANAGER_PASSWORD",
        "SOLAR_MANAGER_SM_ID", "ZODIAC_EMAIL", "ZODIAC_PASSWORD", "ZODIAC_SERIAL",
    ):
        monkeypatch.delenv(name, raising=False)
    missing = Credentials.from_env().missing_for_control()
    assert any("SOLAR_MANAGER_API_KEY" in item for item in missing)
    assert "ZODIAC_SERIAL" in missing


def test_an_api_key_alone_satisfies_the_solar_credentials():
    credentials = Credentials(
        solar_api_key="k", solar_sm_id="sm", zodiac_email="e",
        zodiac_password="p", zodiac_serial="s",
    )
    assert credentials.missing_for_control() == []


def test_email_and_password_also_satisfy_them():
    credentials = Credentials(
        solar_email="e", solar_password="p", solar_sm_id="sm",
        zodiac_email="e", zodiac_password="p", zodiac_serial="s",
    )
    assert credentials.missing_for_control() == []


def test_an_untouched_repository_reads_as_not_configured():
    """The schedule runs from the moment the workflow lands, before setup."""
    assert Credentials().any_configured() is False


def test_a_half_filled_setup_reads_as_configured():
    """Some secrets present and others missing is a fault, not a fresh start."""
    assert Credentials(solar_api_key="k").any_configured() is True


def test_a_start_threshold_below_the_heater_draw_is_rejected(monkeypatch):
    """The invariant behind "it never takes power off the grid"."""
    monkeypatch.setenv("ON_THRESHOLD", "1200")
    monkeypatch.setenv("HEATER_DRAW_W", "1600")
    with pytest.raises(ConfigError) as raised:
        Config.from_env()
    assert "import" in str(raised.value)


def test_a_start_threshold_at_the_draw_is_allowed(monkeypatch):
    monkeypatch.setenv("ON_THRESHOLD", "1600")
    monkeypatch.setenv("HEATER_DRAW_W", "1600")
    assert Config.from_env().on_threshold_w == 1600


def test_the_default_threshold_leaves_room_above_the_default_draw():
    assert Config().on_threshold_w >= Config().heater_draw_w
