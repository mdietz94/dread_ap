"""Tests for the ``/warp`` softlock-recovery command.

Covers the three client-side gates (no bridge / no bootstrap / connected),
the three Lua-side replies (``ok`` / ``not_ingame`` / ``no_interaction``),
and the transport-error fallbacks. The Lua source itself is asserted by
substring — exact whitespace isn't load-bearing, but the LoadScenario
call and the two guard reads must be present.
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client.datapackage import DataPackage  # noqa: E402
from dread.client.state import BridgeState  # noqa: E402
from dread.client.wire import Response  # noqa: E402

DATA = ROOT / "data"


@pytest.fixture
def ctx():
    from dread.client.context import DreadContext

    state = BridgeState()
    dp = DataPackage(apworld_data_dir=DATA)
    c = DreadContext(
        server_address=None,
        password=None,
        state=state,
        datapackage=dp,
        bridge_port=0,
        discovery_port=0,
    )
    return c


def _stub_bridge(ctx, *, connected: bool, response: Response | Exception):
    """Wire a minimal _bridge with is_connected() + run_lua() and mark
    bootstrap done. ``response`` is either a Response to return or an
    Exception to raise."""
    bridge = unittest.mock.MagicMock()
    bridge.is_connected.return_value = connected
    if isinstance(response, Exception):
        bridge.run_lua = unittest.mock.AsyncMock(side_effect=response)
    else:
        bridge.run_lua = unittest.mock.AsyncMock(return_value=response)
    ctx._bridge = bridge
    ctx._bootstrapped = True
    return bridge


@pytest.mark.asyncio
async def test_warp_no_bridge(ctx):
    msg = await ctx._warp_to_start()
    assert "no active Switch" in msg


@pytest.mark.asyncio
async def test_warp_bridge_not_connected(ctx):
    _stub_bridge(ctx, connected=False, response=Response(success=True, payload=b"ok"))
    msg = await ctx._warp_to_start()
    assert "no active Switch" in msg


@pytest.mark.asyncio
async def test_warp_bootstrap_pending(ctx):
    _stub_bridge(ctx, connected=True, response=Response(success=True, payload=b"ok"))
    ctx._bootstrapped = False
    msg = await ctx._warp_to_start()
    assert "bootstrap not complete" in msg


@pytest.mark.asyncio
async def test_warp_ok_payload(ctx):
    bridge = _stub_bridge(
        ctx, connected=True, response=Response(success=True, payload=b"ok")
    )
    msg = await ctx._warp_to_start()
    assert "warped" in msg
    assert "save" in msg
    # The Lua we send: matches Randovania's CheckWarpToStart primitive,
    # gated on game-mode and user-interaction. Any of these missing would
    # be a real regression — a wrong scenario name (e.g. dropping the
    # "c10_samus" character package) would silently no-op.
    lua = bridge.run_lua.await_args.args[0]
    assert "Game.LoadScenario" in lua
    assert "Init.sStartingScenario" in lua
    assert "Init.sStartingActor" in lua
    assert 'Game.GetCurrentGameModeID() ~= "INGAME"' in lua
    assert "Scenario.IsUserInteractionEnabled(true)" in lua


@pytest.mark.asyncio
async def test_warp_blocked_not_ingame(ctx):
    _stub_bridge(
        ctx, connected=True, response=Response(success=True, payload=b"not_ingame")
    )
    msg = await ctx._warp_to_start()
    assert "blocked" in msg
    assert "not in-game" in msg


@pytest.mark.asyncio
async def test_warp_blocked_cutscene(ctx):
    _stub_bridge(
        ctx, connected=True, response=Response(success=True, payload=b"no_interaction")
    )
    msg = await ctx._warp_to_start()
    assert "blocked" in msg
    assert "cutscene" in msg


@pytest.mark.asyncio
async def test_warp_lua_error_surfaces(ctx):
    _stub_bridge(
        ctx, connected=True, response=Response(success=False, payload=b"syntax error near X")
    )
    msg = await ctx._warp_to_start()
    assert "lua error" in msg


@pytest.mark.asyncio
async def test_warp_timeout_surfaces(ctx):
    _stub_bridge(ctx, connected=True, response=asyncio.TimeoutError("waited too long"))
    msg = await ctx._warp_to_start()
    assert "failed" in msg


@pytest.mark.asyncio
async def test_warp_connection_error_surfaces(ctx):
    _stub_bridge(ctx, connected=True, response=ConnectionError("peer reset"))
    msg = await ctx._warp_to_start()
    assert "failed" in msg


@pytest.mark.asyncio
async def test_warp_unknown_payload(ctx):
    _stub_bridge(
        ctx, connected=True, response=Response(success=True, payload=b"surprise")
    )
    msg = await ctx._warp_to_start()
    assert "unexpected" in msg
