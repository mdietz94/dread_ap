"""Tests for the command parser."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client.commands import parse_command, parse_switch_target  # noqa: E402
from dread.client.state import BridgeState  # noqa: E402


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


def test_help_mentions_dread_connect():
    r = parse_command("help")
    assert r.info and "/dread_connect" in r.info


# ---- parse_switch_target -------------------------------------------------

def test_parse_switch_target_host_only():
    assert parse_switch_target("1.2.3.4") == ("1.2.3.4", None)


def test_parse_switch_target_host_and_port():
    assert parse_switch_target("1.2.3.4:6969") == ("1.2.3.4", 6969)


def test_parse_switch_target_hostname():
    assert parse_switch_target("localhost") == ("localhost", None)


def test_parse_switch_target_tolerates_spaces():
    assert parse_switch_target("  localhost : 7000 ") == ("localhost", 7000)


@pytest.mark.parametrize("bad", ["", "1.2.3.4:abc", "1.2.3.4:99999",
                                 "1.2.3.4:0", "::1", "host:"])
def test_parse_switch_target_rejects_bad(bad):
    with pytest.raises(ValueError):
        parse_switch_target(bad)


# ---- /dread_connect command ---------------------------------------------

class _FakeConnectCtx:
    """Minimal stand-in for DreadContext for the command-processor test."""

    def __init__(self):
        self.switch_host = "127.0.0.1"
        self.switch_port = 6969
        self.reconnect_called = False

    async def reconnect_switch(self):
        self.reconnect_called = True


def _run_connect(arg: str) -> _FakeConnectCtx:
    """Invoke /dread_connect under a running loop (the command schedules
    reconnect_switch via asyncio.ensure_future, which needs a live loop)."""
    from dread.client.context import DreadClientCommandProcessor

    async def go():
        ctx = _FakeConnectCtx()
        proc = DreadClientCommandProcessor(ctx)
        proc.output = lambda _msg: None  # swallow CLI echo
        proc._cmd_dread_connect(arg)
        await asyncio.sleep(0)  # let the scheduled reconnect task run
        return ctx

    return asyncio.run(go())


def test_dread_connect_no_arg_reconnects_current_target():
    ctx = _run_connect("")
    assert ctx.switch_host == "127.0.0.1"
    assert ctx.switch_port == 6969
    assert ctx.reconnect_called is True


def test_dread_connect_repoints_then_reconnects():
    ctx = _run_connect("1.2.3.4:7000")
    assert ctx.switch_host == "1.2.3.4"
    assert ctx.switch_port == 7000
    assert ctx.reconnect_called is True


def test_dread_connect_bad_target_does_not_reconnect():
    ctx = _run_connect("1.2.3.4:nope")
    assert ctx.switch_host == "127.0.0.1"  # unchanged
    assert ctx.reconnect_called is False
