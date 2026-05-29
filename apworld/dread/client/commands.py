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
  /dread_connect [ip[:port]]               (re)dial the Switch; optional ip re-points first
  /switch_host <ip>                        point the client at a Switch IP (or 'localhost' for Ryujinx)
  /switch_reconnect                        drop and re-dial the Switch (alias of /dread_connect)
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


def parse_switch_target(s: str) -> tuple[str, Optional[int]]:
    """Parse a ``host`` or ``host:port`` Switch target.

    Used by ``/dread_connect`` and the GUI reconnect popup. Returns
    ``(host, port)`` where ``port`` is ``None`` when the caller gave only a
    host (the existing ``switch_port`` is kept). Raises ``ValueError`` on an
    empty host or a non-numeric / out-of-range port so the caller can report
    a usage error instead of silently dialing the wrong place.

    IPv6 literals aren't supported — every documented Dread/Ryujinx setup
    reaches the console over LAN IPv4 — so a multi-colon string is rejected
    rather than mis-split.
    """
    text = (s or "").strip()
    if not text:
        raise ValueError("empty switch target")
    if ":" not in text:
        return text, None
    host, _, port_str = text.rpartition(":")
    host = host.strip()
    port_str = port_str.strip()
    if not host or ":" in host:
        raise ValueError(f"invalid switch target: {s!r}")
    try:
        port = int(port_str)
    except ValueError:
        raise ValueError(f"invalid port: {port_str!r}") from None
    if not (1 <= port <= 65535):
        raise ValueError(f"port out of range: {port}")
    return host, port
