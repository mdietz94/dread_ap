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
    format_status_panel,
    format_switch_pill,
    switch_pill_color,
)

_GREEN = "#4caf50"
_ORANGE = "#ff9800"
_GRAY = "#888888"


def test_pill_color_connected_is_green():
    assert switch_pill_color("connected") == _GREEN


def test_pill_color_disconnected_and_empty_is_gray():
    assert switch_pill_color("disconnected") == _GRAY
    assert switch_pill_color("") == _GRAY


def test_pill_color_error_is_orange():
    assert switch_pill_color("error: [Errno 111] Connection refused") == _ORANGE


def test_switch_pill_states():
    assert "connected" in format_switch_pill({"switch_conn": "connected"})
    assert _GREEN in format_switch_pill({"switch_conn": "connected"})
    assert "off" in format_switch_pill({"switch_conn": "disconnected"})
    assert "error" in format_switch_pill({"switch_conn": "error: nope"})
    assert _ORANGE in format_switch_pill({"switch_conn": "error: nope"})


def test_switch_pill_defaults_to_off_when_missing():
    # Empty snapshot (no switch_conn key) must not raise and reads as off.
    assert "off" in format_switch_pill({})


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
