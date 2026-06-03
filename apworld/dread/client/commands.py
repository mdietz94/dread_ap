"""Pure command parsing for DreadClient's ``/``-commands.

Mirrors smo_archipelago/commands.py — pure input string → ParseResult.
The Kivy ClientCommandProcessor in ``context.py`` calls each ``_cmd_*``
method, which delegates here for the pure-text ones (``status``, ``help``).
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
  /bridge_status                           show TCP/UDP bridge + connected Switches
  /switches                                alias of /bridge_status
  /promote_switch <device_id>              make this Switch the active one
  /warp                                    warp to the starting room (softlock recovery)
  /setup                                   open the setup wizard (prereqs, build, deploy)
  /poke <lua>                              run arbitrary Lua via lua_exec (debug)

Tip: at any save station, hold ZL+ZR while selecting Cancel to warp
to the starting location (built into the patched seed).

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
