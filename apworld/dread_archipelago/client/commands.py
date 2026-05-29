"""Pure command parsing for DreadClient's ``/``-commands.

Mirrors smo_archipelago/commands.py — pure input string → ParseResult.
The Kivy ClientCommandProcessor in ``context.py`` calls each ``_cmd_*``
method, which delegates here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .state import BridgeState

log = logging.getLogger(__name__)


HELP_TEXT = """\
Dread Client commands (type with leading /):
  /dread_status                            show client-side state
  /switch_host <ip>                        point the client at a Switch IP (or 'localhost' for Ryujinx)
  /switch_reconnect                        drop and re-dial the Switch
  /patch <dreadvania-dir> <vanilla-romfs>  build + deploy the AP-shaped mod from this session
  /poke <lua>                              run arbitrary Lua via PACKET_REMOTE_LUA_EXEC (debug)

To inject items, use the AP server console:
  /send <slot> <item name>                 e.g. /send Samus Missile Tank
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
