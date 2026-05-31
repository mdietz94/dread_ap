"""Pure command parsing for DreadClient's ``/``-commands.

Mirrors smo_archipelago/commands.py — pure input string → ParseResult.
The Kivy ClientCommandProcessor in ``context.py`` calls each ``_cmd_*``
method, which delegates here.

Under the inverted topology (Switch dials the PC), there is no Switch IP
to type in — discovery handles it. The ``/dread_connect`` /
``/switch_reconnect`` commands now just force-close the active connection
so the sysmodule redials. ``/switch_host`` and the IP argument to
``/dread_connect`` were removed; ``parse_switch_target`` is gone.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .state import BridgeState

log = logging.getLogger(__name__)


HELP_TEXT = """\
Dread Client commands (type with leading /):
  /dread_status                  show client-side state
  /dread_connect                 drop the active Switch socket (sysmodule will redial)
  /switch_reconnect              alias of /dread_connect
  /setup [ryujinx|sd]            build + deploy the patched sysmodule; romfs patcher then auto-runs on connect
  /poke <lua>                    run arbitrary Lua via PACKET_REMOTE_LUA_EXEC (debug)

The Switch finds the PC automatically via UDP discovery — no IP entry
needed. If discovery isn't working, re-run /setup so the sysmodule's
fallback subnet hint is fresh.

To inject items, use the AP server console:
  /send <slot> <item name>       e.g. /send Samus Missile Tank
"""


@dataclass
class ParseResult:
    info: Optional[str] = None
    error: Optional[str] = None
    quit: bool = False


def parse_command(line: str, state: Optional[BridgeState] = None) -> ParseResult:
    s = line.strip()
    if not s:
        return ParseResult()
    cmd = s.split(None, 1)[0].lower()

    if cmd in ("quit", "exit", "q"):
        return ParseResult(quit=True)
    if cmd in ("help", "?", "h"):
        return ParseResult(info=HELP_TEXT)
    if cmd == "status":
        if state is None:
            return ParseResult(info="status unavailable (no client state attached)")
        n_recv = state.received_count()
        n_coll = len(state.all_collected_ids())
        gs = state.game_state
        return ParseResult(info=(
            f"received_items   = {n_recv}\n"
            f"collected_checks = {n_coll}\n"
            f"scenario         = {gs.scenario_id!r}\n"
            f"game_mode        = {gs.game_mode_id!r}\n"
            f"beaten           = {gs.beaten_since_reboot}\n"
            f"layout_uuid      = {gs.layout_uuid!r}\n"
        ))

    return ParseResult(error=f"unknown command: {cmd!r}; type `help`")
