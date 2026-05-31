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
    p.add_argument("--listen-host", default="0.0.0.0",
                   help="Bind address for the Switch TCP listener (default: 0.0.0.0)")
    p.add_argument("--listen-port", type=int, default=17777,
                   help="TCP port the Switch sysmodule dials in on (default: 17777)")
    p.add_argument("--discovery-port", type=int, default=17779,
                   help="UDP port the Switch sysmodule probes for the bridge "
                        "(default: 17779)")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


async def main(args: argparse.Namespace) -> None:
    logging_setup.setup(args.log_level)
    log.info("DreadClient starting (listening for Switch on TCP %s:%d, UDP discovery :%d)",
             args.listen_host, args.listen_port, args.discovery_port)

    state = BridgeState()
    dp = DataPackage(apworld_data_dir=_resolve_apworld_data())

    server_addr = args.connect if args.connect else None
    ctx = DreadContext(
        server_addr,
        args.password or None,
        state=state,
        datapackage=dp,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        discovery_port=args.discovery_port,
    )
    if args.name:
        ctx.auth = args.name

    # Start the UDP discovery responder + TCP listener. The Switch sysmodule
    # finds us via UDP probes (loopback + /24 sweep) and TCP-connects to the
    # listener; the per-connection lifetime runs in DreadContext.
    asyncio.create_task(ctx.start_switch_listener(), name="dread-switch-listen")

    # Find a Python that can run the patcher (and tell the user how to install
    # open-dread-rando in the Archipelago tab if none qualifies).
    asyncio.create_task(ctx._ensure_patcher_python(), name="dread-patcher-python")

    if args.connect:
        asyncio.create_task(ctx.connect(), name="initial-ap-connect")

    # Kivy GUI when a display server is available (gui_enabled is False on
    # headless hosts); DREAD_NOGUI=1 forces CLI-only (used by the e2e smoke
    # test). run_gui lazy-imports gui.py so Kivy is never pulled headless.
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
