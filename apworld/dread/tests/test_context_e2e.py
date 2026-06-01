"""End-to-end test of the Switch→AP path inside DreadContext.

Asserts that a JSON push (``Collected``, ``GameState``, ``Inventory``, ``Log``)
lands on ``_on_switch_push``, parses correctly, dedupes against BridgeState,
and emits a ``LocationChecks`` message via ``send_msgs`` where appropriate.

Mocks the AP server connection with a tiny ``send_msgs`` capture; no real
bridge / fake switch — we exercise the push handler directly with constructed
wire messages.

Run with:  python -m pytest apworld/dread/tests/test_context_e2e.py -v
"""
from __future__ import annotations

import sys
import unittest.mock
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client import wire as W  # noqa: E402
from dread.client.datapackage import DataPackage  # noqa: E402
from dread.client.state import BridgeState  # noqa: E402

DATA = ROOT / "data"


def _hex_for(pickup_indices: list[int]) -> str:
    """Hex bitfield matching what the bootstrap Lua emits."""
    if not pickup_indices:
        return ""
    max_bit = max(pickup_indices)
    num_bytes = (max_bit // 8) + 1
    buf = bytearray(num_bytes)
    for idx in pickup_indices:
        buf[idx // 8] |= 1 << (idx % 8)
    return buf.hex()


@pytest.fixture
def ctx():
    """Build a DreadContext with mocked AP-server hookup."""
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
    c.send_msgs = unittest.mock.AsyncMock()  # type: ignore[method-assign]
    return c


@pytest.mark.asyncio
async def test_collected_push_emits_location_checks(ctx):
    await ctx._on_switch_push(W.Collected(hex=_hex_for([0, 1, 5])))
    ctx.send_msgs.assert_awaited_once()
    args, _ = ctx.send_msgs.await_args
    msgs = args[0]
    assert len(msgs) == 1
    assert msgs[0]["cmd"] == "LocationChecks"
    expected_ids = [
        ctx.datapackage.pickup_index_to_location_id(0),
        ctx.datapackage.pickup_index_to_location_id(1),
        ctx.datapackage.pickup_index_to_location_id(5),
    ]
    assert sorted(msgs[0]["locations"]) == sorted(expected_ids)
    assert ctx.state.all_collected_ids() == set(expected_ids)


@pytest.mark.asyncio
async def test_duplicate_collected_doesnt_double_send(ctx):
    """Bootstrap dumps the FULL collected set every poll. Identical push
    twice = no second LocationChecks."""
    msg = W.Collected(hex=_hex_for([0, 1, 5]))
    await ctx._on_switch_push(msg)
    await ctx._on_switch_push(msg)
    assert ctx.send_msgs.await_count == 1


@pytest.mark.asyncio
async def test_partial_overlap_only_sends_new(ctx):
    """First push: [0, 1]. Second push: [0, 1, 5]. Only 5 sent the second time."""
    await ctx._on_switch_push(W.Collected(hex=_hex_for([0, 1])))
    await ctx._on_switch_push(W.Collected(hex=_hex_for([0, 1, 5])))
    assert ctx.send_msgs.await_count == 2
    second_args, _ = ctx.send_msgs.await_args_list[1]
    expected_5 = ctx.datapackage.pickup_index_to_location_id(5)
    assert second_args[0][0]["locations"] == [expected_5]


@pytest.mark.asyncio
async def test_unknown_index_skipped(ctx):
    """A pickup_index beyond known locations: skip, don't crash."""
    await ctx._on_switch_push(W.Collected(hex=_hex_for([200])))
    ctx.send_msgs.assert_not_called()


@pytest.mark.asyncio
async def test_empty_collected_emits_nothing(ctx):
    await ctx._on_switch_push(W.Collected(hex=""))
    ctx.send_msgs.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_hex_is_logged_not_sent(ctx):
    """If hex string is not valid hex, log + skip."""
    await ctx._on_switch_push(W.Collected(hex="zznotahex"))
    ctx.send_msgs.assert_not_called()
    assert ctx.state.all_collected_ids() == set()


@pytest.mark.asyncio
async def test_game_state_push_updates_state_and_triggers_goal(ctx):
    await ctx._on_switch_push(W.GameState(scenario="s080_shipyard", beaten=True))
    assert ctx.state.is_beaten() is True
    sent = []
    for call in ctx.send_msgs.await_args_list:
        sent.extend(call.args[0])
    assert any(m.get("cmd") == "StatusUpdate" for m in sent)


@pytest.mark.asyncio
async def test_inventory_push_updates_state_mirror(ctx):
    await ctx._on_switch_push(W.Inventory(index=3, inventory=[1.0, 2.0, 3.5]))
    inv = ctx.state.get_inventory()
    assert inv["slot0"] == 1
    assert inv["slot1"] == 2
    assert inv["slot2"] == 4  # rounded from 3.5
    ctx.send_msgs.assert_not_called()


@pytest.mark.asyncio
async def test_log_push_added_to_log_surface(ctx):
    await ctx._on_switch_push(W.Log(level="info", msg="hello from lua"))
    assert "hello from lua" in ctx.state.last_messages
    ctx.send_msgs.assert_not_called()


@pytest.mark.asyncio
async def test_received_pickups_push_advances_cursor(ctx):
    await ctx._on_switch_push(W.ReceivedPickups(count=5))
    assert ctx.state.game_received_pickups() == 5
    ctx.send_msgs.assert_not_called()
