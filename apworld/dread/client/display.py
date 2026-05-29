"""Kivy-free formatting for the DreadClient GUI.

Pure functions: ``state.snapshot()`` dict in, Kivy BBCode-markup string out.
Deliberately holds NO Kivy import so it stays unit-testable in the headless
pytest env (gui.py — which DOES pull Kivy — only ever calls into here). This
is the one bit of presentation logic that's worth testing, so unlike SMO's
inlined ``_format_odyssey`` we keep it in its own module.

Colors carry connection state, mirroring the SMO Switch-pill convention:
green = connected/healthy, orange = problem/error, gray = idle/disconnected.
"""
from __future__ import annotations

# Status colors (hex, no leading '#': callers wrap in [color=...]).
_GREEN = "#4caf50"
_ORANGE = "#ff9800"
_GRAY = "#888888"


def _conn_color(conn: str) -> str:
    """Map a connection string to a status color.

    ``"connected"`` → green; ``"disconnected"``/empty → gray; anything else
    (e.g. ``"error: [Errno 111] Connection refused"``) → orange.
    """
    c = (conn or "").strip().lower()
    if c == "connected":
        return _GREEN
    if c in ("", "disconnected"):
        return _GRAY
    return _ORANGE


def switch_pill_color(conn: str) -> str:
    """Public alias used by the top-bar Switch pill."""
    return _conn_color(conn)


def _colored(text: str, color: str) -> str:
    return f"[color={color}]{text}[/color]"


def format_switch_pill(snap: dict) -> str:
    """One-line Switch status for the top-bar pill, colored by state.

    Short by design — the pill auto-fits its width to this text, and a long
    ``error: …`` string would crowd out the AP server-address input. The full
    error text lives in the status panel + log pane.
    """
    conn = snap.get("switch_conn", "disconnected") or "disconnected"
    color = _conn_color(conn)
    c = conn.strip().lower()
    if c == "connected":
        label = "Switch: connected"
    elif c in ("", "disconnected"):
        label = "Switch: off"
    else:
        label = "Switch: error"
    return _colored(label, color)


def format_status_panel(snap: dict) -> str:
    """At-a-glance client state for the left half of the "Dread" tab.

    Shows the two wires (AP + Switch), the slot/seed, the current in-game
    scenario, item-delivery / location-check counts, and goal status. Returns
    Kivy BBCode markup (the caller renders it in a ``markup=True`` Label).
    """
    ap_conn = snap.get("ap_conn", "disconnected") or "disconnected"
    switch_conn = snap.get("switch_conn", "disconnected") or "disconnected"
    slot = snap.get("slot") or "—"
    seed = snap.get("seed") or "—"
    scenario = snap.get("scenario") or "—"
    delivered = snap.get("game_received_pickups", 0)
    checked = snap.get("collected_count", 0)
    beaten = bool(snap.get("beaten", False))

    goal = (_colored("BEATEN — goal reported", _GREEN)
            if beaten else _colored("not yet", _GRAY))

    lines = [
        "[b]Connections[/b]",
        f"  AP server : {_colored(ap_conn, _conn_color(ap_conn))}",
        f"  Switch    : {_colored(switch_conn, _conn_color(switch_conn))}",
        "",
        "[b]Session[/b]",
        f"  Slot      : {slot}",
        f"  Seed      : {seed}",
        f"  Scenario  : {scenario}",
        "",
        "[b]Progress[/b]",
        f"  Items delivered    : {delivered}",
        f"  Locations checked  : {checked}",
        f"  Goal               : {goal}",
    ]
    return "\n".join(lines)
