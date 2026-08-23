"""Command line entry point.

    python -m pool_heater run            one control cycle
    python -m pool_heater probe-solar    verify Solar Manager credentials and readings
    python -m pool_heater probe-zodiac   verify iAquaLink auth, find the serial, read state
    python -m pool_heater show-state     print the persisted state
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime

from .config import Config, ConfigError, Credentials
from .models import Mode
from .notify import Notifier
from .runner import Runner
from .solar_manager import SolarManagerClient, SolarManagerError, id_fields
from .state import StateStore
from .zodiac import ZodiacClient, ZodiacError

DEFAULT_STATE_PATH = "state/pool-heater-state.json"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S%z",
        stream=sys.stdout,
    )


def _parser() -> argparse.ArgumentParser:
    # Shared options are declared on a parent parser so they work either side of
    # the subcommand: `--state X run` and `run --state X` both do the right thing.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", default=os.environ.get("STATE_PATH", DEFAULT_STATE_PATH))
    common.add_argument("-v", "--verbose", action="store_true")

    parser = argparse.ArgumentParser(prog="pool_heater", description=__doc__, parents=[common])
    sub = parser.add_subparsers(dest="subcommand", required=True)

    sub.add_parser("run", parents=[common], help="run one control cycle")
    sub.add_parser("show-state", parents=[common], help="print the persisted state as JSON")
    sub.add_parser("probe-solar", parents=[common], help="read Solar Manager and print what came back")

    zodiac = sub.add_parser(
        "probe-zodiac", parents=[common], help="read the heat pump and print what came back"
    )
    zodiac.add_argument(
        "--send",
        choices=["on", "off", "boost", "ecosilence", "smart"],
        help="send a real command to the heater to confirm the app reflects it",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _setup_logging(args.verbose)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    credentials = Credentials.from_env()

    if args.subcommand == "run":
        return _run(config, credentials, args.state)
    if args.subcommand == "show-state":
        print(json.dumps(StateStore(args.state).load().to_dict(), indent=2, sort_keys=True))
        return 0
    if args.subcommand == "probe-solar":
        return _probe_solar(config, credentials)
    if args.subcommand == "probe-zodiac":
        return _probe_zodiac(config, credentials, getattr(args, "send", None))
    return 2


def _run(config: Config, credentials: Credentials, state_path: str) -> int:
    missing = credentials.missing_for_control()
    if missing and not credentials.any_configured():
        # A repository whose secrets have not been filled in yet. The schedule
        # starts running the moment the workflow lands on the default branch, so
        # treating this as a failure would mean a failure notification every five
        # minutes until setup finishes. It is not a fault; there is just nothing
        # to do yet.
        print(
            "No credentials configured yet, so there is nothing to control.\n"
            "Add the repository secrets, then run the probe workflow:\n  "
            + "\n  ".join(missing),
        )
        return 0
    if missing:
        print("missing credentials: " + ", ".join(missing), file=sys.stderr)
        return 2
    runner = Runner(config, credentials, StateStore(state_path))
    mode = "DRY RUN" if config.dry_run else "LIVE"
    logging.getLogger(__name__).info(
        "pool heater cycle (%s), season %02d/%02d-%02d/%02d, window %s-%s %s",
        mode,
        config.season_start[1], config.season_start[0],
        config.season_end[1], config.season_end[0],
        config.hard_off_end.strftime("%H:%M"),
        config.hard_off_start.strftime("%H:%M"),
        config.timezone,
    )
    result = runner.run_once()
    return 0 if result.ok else 1


def _probe_solar(config: Config, credentials: Credentials) -> int:
    client = SolarManagerClient(credentials, config)
    try:
        client.authenticate()
        print(f"authenticated against Solar Manager: {client.auth_method}")
    except SolarManagerError as exc:
        print(f"Solar Manager authentication failed: {exc}", file=sys.stderr)
        return 1

    if not credentials.solar_sm_id:
        return _discover_sm_id(client)

    try:
        raw = client.stream()
    except SolarManagerError as exc:
        print(f"Solar Manager probe failed: {exc}", file=sys.stderr)
        return 1

    devices = raw.get("devices") if isinstance(raw.get("devices"), list) else []
    summary = {key: value for key, value in raw.items() if key != "devices"}
    print("\n--- live stream (devices omitted) ---")
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))

    if devices:
        print("\n--- devices: find the Easee here and set SOLAR_MANAGER_CAR_DEVICE_ID ---")
        meta = client.device_metadata()
        for device in devices:
            device_id = str(device.get("_id", "?"))
            info = meta.get(device_id, {})
            # Names withheld for the same reason as on the heat pump side.
            print(
                f"  {device_id}  "
                f"type={info.get('type', '?'):16} power={device.get('power', '?')}"
            )

    reading = client.read(datetime.now(config.tz))
    print("\n--- parsed ---")
    print(f"  PV                 {reading.pv_w:>8.0f} W")
    print(f"  consumption        {reading.consumption_w:>8.0f} W")
    print(f"  grid import        {reading.grid_import_w:>8.0f} W")
    print(f"  grid export        {reading.grid_export_w:>8.0f} W")
    print(f"  battery charging   {reading.battery_charge_w:>8.0f} W")
    print(f"  battery discharge  {reading.battery_discharge_w:>8.0f} W")
    print(f"  car charger        {reading.car_w:>8.0f} W")
    soc = "n/a" if reading.soc_pct is None else f"{reading.soc_pct:.0f} %"
    print(f"  battery SoC        {soc:>8}")
    print(f"  SURPLUS            {reading.surplus_w:>8.0f} W  (export + battery charging)")
    print(f"\n  start threshold is {config.on_threshold_w:.0f} W")
    return 0


def _discover_sm_id(client: SolarManagerClient) -> int:
    """Print anything that looks like an SM ID, so it need not be hunted by hand."""
    print(
        "\nSOLAR_MANAGER_SM_ID is not set, so this is asking the API what it knows.\n"
        "Solar Manager documents the endpoints that use the id but not one that\n"
        "lists it, so this is a best effort. If nothing useful appears below, the\n"
        "id is usually in the portal's address bar while you view your installation."
    )
    findings = client.discover_sm_id()
    if not findings:
        print("\nNothing answered. Fall back to the address bar.")
        return 1

    printed = False
    for source, payload in findings:
        identifiers = id_fields(payload)
        if not identifiers:
            continue
        printed = True
        print(f"\n--- {source} ---")
        for key, value in sorted(identifiers.items()):
            print(f"  {key}: {value}")

    if not printed:
        print("\nEndpoints answered, but none carried anything id-shaped.")
        return 1

    print(
        "\nOnly identifier fields are shown: these endpoints also return your name,\n"
        "email and address, and workflow logs on a public repository are public.\n"
        "\n`sm_id` is the one to use. Set it as the SOLAR_MANAGER_SM_ID secret and\n"
        "run this probe again -- it will then read live power figures instead."
    )
    return 0


def _probe_zodiac(config: Config, credentials: Credentials, command: str | None) -> int:
    client = ZodiacClient(credentials, config)
    try:
        client.login()
        print("authenticated against iAquaLink")
    except ZodiacError as exc:
        print(f"iAquaLink login failed: {exc}", file=sys.stderr)
        return 1

    if not credentials.zodiac_serial:
        try:
            devices = client.list_devices()
        except ZodiacError as exc:
            print(f"could not list devices: {exc}", file=sys.stderr)
            return 1
        print("\n--- devices on the account: set ZODIAC_SERIAL to the heat pump's serial ---")
        for device in devices:
            # Names are withheld. People name their equipment after the house or
            # the street, and workflow logs on a public repository are public.
            # The type is what tells you which device is the heat pump anyway.
            print(
                f"  {device.get('serial_number', '?')}  "
                f"type={device.get('device_type', '?')}"
            )
        print("\n  (device names are not shown: they often give away an address)")
        return 0

    if command:
        try:
            if command == "on":
                client.turn_on(config.on_mode, config.setpoint_c)
            elif command == "off":
                client.turn_off()
            else:
                client.set_mode(Mode(command))
        except ZodiacError as exc:
            print(f"command {command!r} failed: {exc}", file=sys.stderr)
            return 1
        print(f"sent {command!r}; check the iAquaLink app reflects it, then re-run this probe")

    try:
        shadow = client.get_shadow()
    except ZodiacError as exc:
        print(f"shadow read failed: {exc}", file=sys.stderr)
        return 1

    from .zodiac import equipment_from_shadow, parse_shadow

    print("\n--- raw equipment block ---")
    print(json.dumps(equipment_from_shadow(shadow), indent=2, sort_keys=True, default=str))

    state = parse_shadow(shadow, config)
    print("\n--- parsed ---")
    print(f"  powered on     {state.on}")
    print(f"  mode           {state.mode.value if state.mode else 'unrecognised'}")
    print(f"  status code    {state.status}")
    print(f"  setpoint       {state.setpoint_c}")
    print(f"  water temp     {state.water_temp_c}")
    print(
        "\n  mode map in use: "
        + ", ".join(f"{mode.value}={code}" for mode, code in config.mode_map.items())
    )
    print(
        "  If the mode above does not match the app, set the heater to each mode in the\n"
        "  app and re-run this probe, then correct ZODIAC_MODE_BOOST / ZODIAC_MODE_SMART /\n"
        "  ZODIAC_MODE_ECOSILENCE to the 'st' values you see."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
