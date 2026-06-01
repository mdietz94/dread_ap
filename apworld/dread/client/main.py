"""DreadClient entry point.

Counterpart to smo_archipelago.client.main. The Switch is the dialer:
DreadClient binds a TCP server on ``0.0.0.0:17777`` and a UDP discovery
responder on ``0.0.0.0:17776``. Switches in the same LAN discover the
PC's IP via the responder + the sysmodule's /24 sweep, then dial us.

Standalone usage from inside an Archipelago checkout:

    python vendor/Archipelago/Launcher.py "Dread Client" \\
        --connect localhost:38281 --name Samus

Headless usage (no GUI; useful for smoke tests):

    DREAD_NOGUI=1 python -m worlds.dread.client.main \\
        --connect localhost:38281 --name Samus
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import Utils
from CommonClient import gui_enabled, get_base_parser

from . import logging_setup
from .bridge_server import DEFAULT_PORT as BRIDGE_DEFAULT_PORT
from .context import DreadContext
from .datapackage import DataPackage
from .discovery import DEFAULT_DISCOVERY_PORT
from .state import BridgeState

log = logging.getLogger("DreadClient")


def _resolve_apworld_data() -> Optional[Path]:
    """Folder-install case may want to point at a specific data dir.
    Returns None to let DataPackage fall through to importlib.resources,
    which works for both folder + .apworld-zip installs."""
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = get_base_parser(description="Metroid Dread Archipelago Client")
    p.prog = "DreadClient"
    p.add_argument("--name", default=None, help="AP slot name to connect as")
    p.add_argument("--bridge-port", type=int, default=BRIDGE_DEFAULT_PORT,
                   help=f"TCP port the Switch dials in on (default: {BRIDGE_DEFAULT_PORT})")
    p.add_argument("--discovery-port", type=int, default=DEFAULT_DISCOVERY_PORT,
                   help=f"UDP discovery port (default: {DEFAULT_DISCOVERY_PORT})")
    p.add_argument("--expected-mod-ver", default="",
                   help="If set, reject HELLO from Switches whose mod_ver doesn't match (default: any)")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


async def main(args: argparse.Namespace) -> None:
    logging_setup.setup(args.log_level)
    log.info("DreadClient starting")
    log.info("Bridge TCP %s, Discovery UDP %s",
             args.bridge_port, args.discovery_port)

    state = BridgeState()
    dp = DataPackage(apworld_data_dir=_resolve_apworld_data())

    server_addr = args.connect if args.connect else None
    ctx = DreadContext(
        server_addr,
        args.password or None,
        state=state,
        datapackage=dp,
        bridge_port=args.bridge_port,
        discovery_port=args.discovery_port,
        expected_mod_ver=args.expected_mod_ver,
    )
    if args.name:
        ctx.auth = args.name

    # Bind the bridge listener + discovery responder immediately so any
    # Switch already up can connect during AP-side setup. A bind failure
    # is fatal per the plan — surface loudly rather than silently degrade.
    try:
        await ctx.start_bridge()
    except OSError as exc:
        log.error("Could not start bridge listener on TCP %d / UDP %d: %s",
                  args.bridge_port, args.discovery_port, exc)
        log.error("Another DreadClient (or some other process) may be using "
                  "those ports. Pass --bridge-port / --discovery-port to "
                  "pick alternatives, or close the conflicting process.")
        return

    # Find a Python that can run the patcher (and tell the user how to install
    # open-dread-rando in the Archipelago tab if none qualifies).
    asyncio.create_task(ctx._ensure_patcher_python(), name="dread-patcher-python")

    if args.connect:
        asyncio.create_task(ctx.connect(), name="initial-ap-connect")

    use_gui = gui_enabled and not os.environ.get("DREAD_NOGUI")
    if use_gui:
        ctx.run_gui()
    ctx.run_cli()

    try:
        await ctx.exit_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("shutdown requested")
    finally:
        await ctx.shutdown()


def launch(*launch_args: str) -> None:
    """Launcher entry point. Called from the Component's launch_client."""
    args = parse_args(list(launch_args))
    Utils.init_logging("DreadClient", exception_logger="Client")
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":  # pragma: no cover
    launch(*sys.argv[1:])
