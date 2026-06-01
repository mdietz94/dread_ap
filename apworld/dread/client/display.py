"""Kivy-free formatting for the DreadClient GUI.

Pure functions: ``state.snapshot()`` dict in, Kivy BBCode-markup string out.
Deliberately holds NO Kivy import so it stays unit-testable in the headless
pytest env (gui.py — which DOES pull Kivy — only ever calls into here).

Colors carry connection state: green = healthy, orange = problem,
gray = idle.
"""
from __future__ import annotations

_GREEN = "#4caf50"
_ORANGE = "#ff9800"
_GRAY = "#888888"


def _conn_color(conn: str) -> str:
    """Map a Switch-connection string to a status color.

    Recognised states:
      * ``connected (...)``   → green (active Switch up and bootstrapped)
      * ``listening``         → orange (bridge up, no Switch yet)
      * ``disconnected`` / "" → gray
      * anything else (e.g. ``bootstrap error: …``) → orange
    """
    c = (conn or "").strip().lower()
    if c.startswith("connected"):
        return _GREEN
    if c in ("", "disconnected"):
        return _GRAY
    return _ORANGE


def switch_pill_color(conn: str) -> str:
    """Public alias used by the top-bar pill."""
    return _conn_color(conn)


def _colored(text: str, color: str) -> str:
    return f"[color={color}]{text}[/color]"


def format_switch_pill(snap: dict) -> str:
    """One-line bridge / Switch status for the top-bar pill.

    Short by design — long error text lives in the status panel + log pane.
    """
    conn = snap.get("switch_conn", "disconnected") or "disconnected"
    color = _conn_color(conn)
    c = conn.strip().lower()
    if c.startswith("connected"):
        label = "Bridge: 1 Switch"
    elif c == "listening":
        label = "Bridge: waiting"
    elif c in ("", "disconnected"):
        label = "Bridge: idle"
    else:
        label = "Bridge: error"
    return _colored(label, color)


def format_status_panel(snap: dict) -> str:
    """At-a-glance client state for the left half of the "Dread" tab."""
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

    patcher = snap.get("patcher_python") or "—"
    if patcher == "—":
        patcher_color = _GRAY
    elif patcher.startswith("ready"):
        patcher_color = _GREEN
    else:
        patcher_color = _ORANGE

    # AP coloring: "connected" exactly = green; anything else handled as
    # before. (AP server state uses the older single-word convention.)
    ap_color = (_GREEN if ap_conn.strip().lower() == "connected"
                else _GRAY if ap_conn.strip().lower() in ("", "disconnected")
                else _ORANGE)

    lines = [
        "[b]Connections[/b]",
        f"  AP server : {_colored(ap_conn, ap_color)}",
        f"  Switch    : {_colored(switch_conn, _conn_color(switch_conn))}",
        f"  Patcher   : {_colored(patcher, patcher_color)}",
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
