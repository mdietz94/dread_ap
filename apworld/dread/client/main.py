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
    # Defaults are None so we can tell "user passed nothing" (→ use the
    # remembered host, else 127.0.0.1) from "user explicitly chose a host".
    p.add_argument("--switch-host", default=None,
                   help="Switch / Ryujinx IP (default: remembered, else 127.0.0.1)")
    p.add_argument("--switch-port", type=int, default=None,
                   help="exlaunch socket port (default: remembered, else 6969)")
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


async def main(args: argparse.Namespace) -> None:
    logging_setup.setup(args.log_level)
    log.info("DreadClient starting")

    # Resolve the Switch target: explicit CLI flag wins; otherwise reuse the
    # host/port remembered from the last successful connect; else the Ryujinx
    # loopback default.
    from .context import _load_user_config
    cfg = _load_user_config()
    switch_host = args.switch_host or cfg.get("switch_host") or "127.0.0.1"
    switch_port = args.switch_port or cfg.get("switch_port") or 6969
    log.info("Switch: %s:%d", switch_host, switch_port)

    state = BridgeState()
    dp = DataPackage(apworld_data_dir=_resolve_apworld_data())

    server_addr = args.connect if args.connect else None
    ctx = DreadContext(
        server_addr,
        args.password or None,
        state=state,
        datapackage=dp,
        switch_host=switch_host,
        switch_port=switch_port,
    )
    if args.name:
        ctx.auth = args.name

    # Supervise the Switch wire: dial now and keep retrying with exponential
    # backoff (the initial dial often loses the race with Dreadvania's boot).
    asyncio.create_task(ctx._switch_supervisor(), name="dread-switch-supervisor")

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
