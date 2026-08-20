"""State persistence and the daily budget."""

from __future__ import annotations

import json
from datetime import timedelta

from conftest import NOON
from pool_heater.state import State, StateStore


def test_state_survives_a_round_trip(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = State()
    state.roll_budget(NOON.date())
    state.record_on(NOON, "boost")
    state.last_shadow_at = NOON
    store.save(state)

    loaded = store.load()
    assert loaded.commanded_on is True
    assert loaded.commanded_mode == "boost"
    assert loaded.last_on_at == NOON
    assert loaded.switch_ons_today == 1
    assert loaded.budget_day == NOON.date().isoformat()


def test_a_missing_state_file_starts_from_a_clean_slate(tmp_path):
    assert StateStore(tmp_path / "nothing.json").load() == State()


def test_a_corrupt_state_file_does_not_wedge_the_loop(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert StateStore(path).load().commanded_on is False


def test_unknown_fields_from_a_future_version_are_ignored(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"commanded_on": True, "invented_later": 42}), encoding="utf-8")
    assert StateStore(path).load().commanded_on is True


def test_the_budget_resets_on_a_new_local_day():
    state = State()
    state.roll_budget(NOON.date())
    state.record_on(NOON, "boost")
    state.record_on(NOON, "boost")
    assert state.switch_ons_today == 2

    state.roll_budget((NOON + timedelta(days=1)).date())
    assert state.switch_ons_today == 0


def test_the_budget_is_not_reset_within_the_same_day():
    state = State()
    state.roll_budget(NOON.date())
    state.record_on(NOON, "boost")
    state.roll_budget(NOON.date())
    assert state.switch_ons_today == 1


def test_recording_a_switch_clears_the_debounce_streaks():
    state = State()
    state.note_surplus(NOON, True)
    state.note_offcond(NOON, True)
    state.record_on(NOON, "boost")
    assert state.surplus_since is None and state.offcond_since is None


def test_the_state_file_never_contains_anything_secret(tmp_path):
    """The file lives in a public repository, so this is load-bearing."""
    store = StateStore(tmp_path / "state.json")
    state = State()
    state.record_on(NOON, "boost")
    store.save(state)

    written = (tmp_path / "state.json").read_text(encoding="utf-8").lower()
    for forbidden in ("token", "password", "secret", "apikey", "api_key", "serial"):
        assert forbidden not in written
