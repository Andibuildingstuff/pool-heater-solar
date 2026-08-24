"""The command line must at least import.

The logbook change renamed the notifier and left a stale import in __main__,
so every scheduled run crashed before doing anything -- while the suite stayed
green, because nothing in it exercised the entry point. These tests run the CLI
the same way the workflow does, so import-time breakage fails here first.
"""

from __future__ import annotations

import importlib
import pkgutil
import subprocess
import sys

import pool_heater


def test_every_module_in_the_package_imports():
    for module in pkgutil.iter_modules(pool_heater.__path__):
        importlib.import_module(f"pool_heater.{module.name}")
    importlib.import_module("pool_heater.__main__")


def test_the_cli_starts_the_way_the_workflow_starts_it():
    """python -m pool_heater, in a fresh interpreter, exactly as Actions runs it."""
    result = subprocess.run(
        [sys.executable, "-m", "pool_heater", "--help"],
        capture_output=True, text=True, timeout=30,
        env={"PATH": "", "PYTHONPATH": "src"},
    )
    assert result.returncode == 0, result.stderr


def test_each_subcommand_parses_its_own_help():
    for command in ("run", "loop", "probe-solar", "probe-zodiac", "show-state"):
        result = subprocess.run(
            [sys.executable, "-m", "pool_heater", command, "--help"],
            capture_output=True, text=True, timeout=30,
            env={"PATH": "", "PYTHONPATH": "src"},
        )
        assert result.returncode == 0, f"{command}: {result.stderr}"
