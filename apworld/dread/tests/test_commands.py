"""Tests for the command parser."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client.commands import parse_command  # noqa: E402
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


def test_help_mentions_udp_discovery():
    r = parse_command("help")
    assert r.info and "UDP discovery" in r.info


# ---- /dread_connect command — drops active conn so Switch redials -------

class _FakeConnectCtx:
    """Minimal stand-in for DreadContext for the command-processor test."""

    def __init__(self):
        self.disconnect_called = False

    async def disconnect_active_switch(self):
        self.disconnect_called = True


def _run_connect() -> _FakeConnectCtx:
    """Invoke /dread_connect under a running loop (the command schedules
    disconnect_active_switch via asyncio.ensure_future, which needs a
    live loop)."""
    from dread.client.context import DreadClientCommandProcessor

    async def go():
        ctx = _FakeConnectCtx()
        proc = DreadClientCommandProcessor(ctx)
        proc.output = lambda _msg: None  # swallow CLI echo
        proc._cmd_dread_connect()
        await asyncio.sleep(0)
        return ctx

    return asyncio.run(go())


def test_dread_connect_drops_active_conn():
    ctx = _run_connect()
    assert ctx.disconnect_called is True


def test_switch_reconnect_is_alias_for_dread_connect():
    """``/switch_reconnect`` keeps working as the deprecated alias."""
    from dread.client.context import DreadClientCommandProcessor

    async def go():
        ctx = _FakeConnectCtx()
        proc = DreadClientCommandProcessor(ctx)
        proc.output = lambda _msg: None
        proc._cmd_switch_reconnect()
        await asyncio.sleep(0)
        return ctx

    ctx = asyncio.run(go())
    assert ctx.disconnect_called is True
