"""DreadClient entry point.

Counterpart to smo_archipelago.client.main. Simpler — Dread doesn't need
SwitchServer or UDP discovery; the PC dials the Switch directly.

Standalone usage from inside an Archipelago checkout:

    python vendor/Archipelago/Launcher.py "Dread Client" \\
        --connect localhost:38281 --name Samus --switch-host 192.168.1.42

Headless usage (no GUI; useful for the Phase 3 smoke test):

    DREAD_NOGUI=1 python -m worlds.dread.client.main \\
        --connect localhost:38281 --name Samus --switch-host 127.0.0.1
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
from .context import DreadContext
from .datapackage import DataPackage
from .state import BridgeState

log = logging.getLogger("DreadClient")


def _resolve_apworld_data() -> Optional[Path]:
    """Legacy path-based loader. Kept for back-compat with the
    folder-install case where tests/dev tooling may want to point at a
    specific data dir. Returns None to let DataPackage fall through to
    importlib.resources, which works for both folder + .apworld-zip
    installs.
    """
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = get_base_parser(description="Metroid Dread Archipelago Client")
    p.prog = "DreadClient"
    p.add_argument("--name", default=None, help="AP slot name to connect as")
    p.add_argument("--switch-host", default="127.0.0.1",
                   help="Switch / Ryujinx IP (default 127.0.0.1)")
    p.add_argument("--switch-port", type=int, default=6969,
                   help="exlaunch socket port (default 6969)")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


async def main(args: argparse.Namespace) -> None:
    logging_setup.setup(args.log_level)
    log.info("DreadClient starting")
    log.info("Switch: %s:%d", args.switch_host, args.switch_port)

    state = BridgeState()
    dp = DataPackage(apworld_data_dir=_resolve_apworld_data())

    server_addr = args.connect if args.connect else None
    ctx = DreadContext(
        server_addr,
        args.password or None,
        state=state,
        datapackage=dp,
        switch_host=args.switch_host,
        switch_port=args.switch_port,
    )
    if args.name:
        ctx.auth = args.name

    # Try to bring the Switch up eagerly so /switch_status is informative
    # even before AP connect. Failure is non-fatal — /switch_reconnect
    # retries from the command line.
    asyncio.create_task(ctx.connect_switch(), name="initial-switch-dial")

    if args.connect:
        asyncio.create_task(ctx.connect(), name="initial-ap-connect")

    use_gui = gui_enabled and not os.environ.get("DREAD_NOGUI")
    if use_gui:
        # gui.py is not implemented yet (Phase 3 deferred); fall back to CLI.
        log.info("GUI not implemented in this build; running headless")
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
