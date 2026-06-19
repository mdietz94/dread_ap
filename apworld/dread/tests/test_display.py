"""Tests for the Kivy-free GUI formatters in client/display.py.

These are the one bit of presentation logic worth testing without Kivy, so
display.py holds no Kivy import and we exercise it against synthetic snapshots
(the same dict shape state.snapshot() produces).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client.display import (  # noqa: E402
    format_patcher_detail,
    format_patcher_pill,
    format_status_panel,
    format_switch_pill,
    patcher_pill_color,
    switch_pill_color,
)

_GREEN = "#4caf50"
_ORANGE = "#ff9800"
_GRAY = "#888888"
_RED = "#f44336"


def test_pill_color_connected_is_green():
    assert switch_pill_color("connected") == _GREEN


def test_pill_color_disconnected_and_empty_is_gray():
    assert switch_pill_color("disconnected") == _GRAY
    assert switch_pill_color("") == _GRAY


def test_pill_color_error_is_orange():
    assert switch_pill_color("error: [Errno 111] Connection refused") == _ORANGE


def test_switch_pill_states():
    # "connected" / "connected (sw-X)" → green pill showing a Switch is active
    assert "Switch" in format_switch_pill({"switch_conn": "connected"})
    assert _GREEN in format_switch_pill({"switch_conn": "connected"})
    assert "Switch" in format_switch_pill({"switch_conn": "connected (sw-A)"})
    # "listening" → bridge up but no Switch yet → orange + "waiting"
    assert "waiting" in format_switch_pill({"switch_conn": "listening"})
    assert _ORANGE in format_switch_pill({"switch_conn": "listening"})
    # "disconnected" → gray + "idle"
    assert "idle" in format_switch_pill({"switch_conn": "disconnected"})
    # Bootstrap/other errors → orange + "error"
    assert "error" in format_switch_pill({"switch_conn": "bootstrap error: nope"})
    assert _ORANGE in format_switch_pill({"switch_conn": "bootstrap error: nope"})


def test_switch_pill_defaults_to_idle_when_missing():
    # Empty snapshot (no switch_conn key) must not raise and reads as idle.
    assert "idle" in format_switch_pill({})


def test_status_panel_disconnected_defaults():
    s = format_status_panel({})
    assert "Connections" in s
    assert "AP server" in s and "Switch" in s
    # Missing slot/seed/scenario render as a placeholder, not a KeyError.
    assert "—" in s


def test_status_panel_shows_session_counts_and_goal():
    snap = {
        "ap_conn": "connected",
        "switch_conn": "connected",
        "slot": "Samus",
        "seed": "ABC123",
        "scenario": "s010_cave",
        "game_received_pickups": 3,
        "collected_count": 5,
        "beaten": True,
    }
    s = format_status_panel(snap)
    assert "Samus" in s
    assert "ABC123" in s
    assert "s010_cave" in s
    assert "Items delivered    : 3" in s
    assert "Locations checked  : 5" in s
    assert "BEATEN" in s


def test_status_panel_goal_not_yet_when_unbeaten():
    s = format_status_panel({"beaten": False})
    assert "not yet" in s
    assert "BEATEN" not in s


# --- patcher pill ----------------------------------------------------------


def test_patcher_pill_error_is_red_and_loud():
    snap = {"patch_status_level": "error", "patcher_python": "ready (python.exe)"}
    # A failed run wins over a 'ready' interpreter — the whole point is the
    # failure must be visible.
    assert _RED in format_patcher_pill(snap)
    assert "ERROR" in format_patcher_pill(snap)
    assert patcher_pill_color(snap) == _RED


def test_patcher_pill_ok_is_green():
    snap = {"patch_status_level": "ok"}
    assert _GREEN in format_patcher_pill(snap)
    assert "OK" in format_patcher_pill(snap)


def test_patcher_pill_ready_interpreter_is_green_before_any_run():
    snap = {"patcher_python": "ready (python.exe)"}
    assert _GREEN in format_patcher_pill(snap)
    assert "ready" in format_patcher_pill(snap)


def test_patcher_pill_needs_setup_is_orange():
    snap = {"patcher_python": "not installed — see Archipelago tab"}
    assert _ORANGE in format_patcher_pill(snap)
    assert "setup" in format_patcher_pill(snap)


def test_patcher_pill_idle_when_empty():
    assert "idle" in format_patcher_pill({})
    assert _GRAY in format_patcher_pill({})


def test_patcher_detail_shows_failure_message():
    snap = {"patch_status_level": "error",
            "patch_status_msg": "the romfs is NOT a recognized vanilla Dread version"}
    detail = format_patcher_detail(snap)
    assert "FAILED" in detail
    assert "recognized vanilla dread version" in detail.lower()


def test_patcher_detail_falls_back_to_interpreter_status():
    detail = format_patcher_detail({"patcher_python": "ready (python.exe)"})
    assert "python.exe" in detail
    assert "No patch has run yet" in detail


def test_patcher_detail_empty_when_nothing_known():
    assert "No patch has run yet" in format_patcher_detail({})
