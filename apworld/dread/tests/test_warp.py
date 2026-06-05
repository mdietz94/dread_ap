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


def _smart_bridge(ctx, *, inventory_before="", inventory_after="",
                  received="0", mode="INGAME"):
    """A bridge whose run_lua dispatches by Lua content, modelling the full
    warp+restore round-trip (snapshot → LoadScenario → re-read → restore).
    The first inventory read returns ``inventory_before``; later ones return
    ``inventory_after``."""
    reads: list[str] = []

    async def dispatch(src):
        if "RandomizerPowerup.GetItemAmount" in src:
            reads.append(src)
            body = inventory_before if len(reads) == 1 else inventory_after
            return Response(success=True, payload=body.encode())
        if "return tostring(RL.ReceivedPickups())" in src:
            return Response(success=True, payload=received.encode())
        if "Game.LoadScenario(" in src:
            return Response(success=True, payload=b"ok")
        if "Game.GetCurrentGameModeID" in src:
            return Response(success=True, payload=mode.encode())
        return Response(success=True, payload=b"")

    bridge = unittest.mock.MagicMock()
    bridge.is_connected.return_value = True
    bridge.run_lua = unittest.mock.AsyncMock(side_effect=dispatch)
    ctx._bridge = bridge
    ctx._bootstrapped = True
    return bridge


@pytest.mark.asyncio
async def test_warp_ok_restores_reverted_pickup(ctx):
    # A Speed Booster Upgrade (resource item) is in inventory before the warp
    # but gone after the LoadScenario reload → the restore must re-grant it.
    bridge = _smart_bridge(
        ctx, inventory_before="ITEM_ENERGY_TANKS=1", inventory_after="",
        received="0", mode="INGAME",
    )
    msg = await ctx._warp_to_start()
    assert "warped" in msg
    assert "restored" in msg
    srcs = [c.args[0] for c in bridge.run_lua.await_args_list]
    joined = "\n".join(srcs)
    # The warp primitive: matches Randovania's CheckWarpToStart, gated on
    # game-mode and user-interaction. A wrong scenario name (e.g. dropping
    # "c10_samus") would silently no-op.
    assert "Game.LoadScenario" in joined
    assert "Init.sStartingScenario" in joined
    assert "Init.sStartingActor" in joined
    assert 'Game.GetCurrentGameModeID() ~= "INGAME"' in joined
    assert "Scenario.IsUserInteractionEnabled(true)" in joined
    # The reverted item was re-granted via a direct OnPickedUp.
    assert any("ITEM_ENERGY_TANKS" in s and ".OnPickedUp(" in s for s in srcs)


@pytest.mark.asyncio
async def test_warp_ok_no_revert_is_a_noop(ctx):
    # Inventory identical before/after → nothing to restore.
    _smart_bridge(ctx, inventory_before="ITEM_ENERGY_TANKS=1",
                  inventory_after="ITEM_ENERGY_TANKS=1", mode="INGAME")
    msg = await ctx._warp_to_start()
    assert "warped" in msg
    assert "no pickups needed restoring" in msg


@pytest.mark.asyncio
async def test_warp_blocked_not_ingame(ctx):
    _stub_bridge(
        ctx, connected=True, response=Response(success=True, payload=b"not_ingame")
    )
    msg = await ctx._warp_to_start()
    assert "blocked" in msg
    assert "not in-game" in msg


@pytest.mark.asyncio
async def test_warp_blocked_in_boss_arena(ctx):
    bridge = _stub_bridge(
        ctx, connected=True, response=Response(success=True, payload=b"in_boss")
    )
    msg = await ctx._warp_to_start()
    assert "blocked" in msg
    assert "boss arena" in msg
    # The warp src must probe RL.IsInBossArena before LoadScenario, guarded so a
    # pre-bootstrap VM (function nil) degrades to allowing the warp.
    src = next(c.args[0] for c in bridge.run_lua.await_args_list
               if "Game.LoadScenario" in c.args[0])
    assert "RL.IsInBossArena and RL.IsInBossArena()" in src
    assert src.index("RL.IsInBossArena") < src.index("Game.LoadScenario")


@pytest.mark.asyncio
async def test_warp_blocked_in_nav_room(ctx):
    bridge = _stub_bridge(
        ctx, connected=True, response=Response(success=True, payload=b"in_nav")
    )
    msg = await ctx._warp_to_start()
    assert "blocked" in msg
    assert "Navigation" in msg
    # The warp src must probe RL.IsInNavRoom before LoadScenario, guarded so a
    # pre-bootstrap VM (function nil) degrades to allowing the warp.
    src = next(c.args[0] for c in bridge.run_lua.await_args_list
               if "Game.LoadScenario" in c.args[0])
    assert "RL.IsInNavRoom and RL.IsInNavRoom()" in src
    assert src.index("RL.IsInNavRoom") < src.index("Game.LoadScenario")
    # Boss check precedes the nav check (both before the LoadScenario).
    assert src.index("RL.IsInBossArena") < src.index("RL.IsInNavRoom")


@pytest.mark.asyncio
async def test_warp_blocked_in_save_room(ctx):
    bridge = _stub_bridge(
        ctx, connected=True, response=Response(success=True, payload=b"in_save")
    )
    msg = await ctx._warp_to_start()
    assert "blocked" in msg
    assert "save station" in msg
    src = next(c.args[0] for c in bridge.run_lua.await_args_list
               if "Game.LoadScenario" in c.args[0])
    assert "RL.IsInSaveRoom and RL.IsInSaveRoom()" in src
    assert src.index("RL.IsInSaveRoom") < src.index("Game.LoadScenario")
    # Order: boss → nav → save, all before the interaction/cutscene check.
    assert src.index("RL.IsInNavRoom") < src.index("RL.IsInSaveRoom")
    assert src.index("RL.IsInSaveRoom") < src.index("IsUserInteractionEnabled")


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
