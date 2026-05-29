"""Tests for the command parser."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread_archipelago.client.commands import parse_command  # noqa: E402
from dread_archipelago.client.state import BridgeState  # noqa: E402


def test_empty_line_is_noop():
    r = parse_command("")
    assert r.info is None and r.error is None and r.quit is False


def test_quit_variants():
    for w in ("quit", "exit", "q", "QUIT"):
        assert parse_command(w).quit is True


def test_help_returns_help_text():
    r = parse_command("help")
    assert r.info and "Dread Client commands" in r.info


def test_unknown_command_returns_error():
    r = parse_command("notathing")
    assert r.error and "unknown" in r.error


def test_status_with_state():
    s = BridgeState()
    s.update_game_state(scenario_id="s010_cave", beaten_since_reboot=False)
    r = parse_command("status", state=s)
    assert r.info and "s010_cave" in r.info
    assert "received_items" in r.info


def test_status_without_state():
    r = parse_command("status")
    assert r.info and "unavailable" in r.info
