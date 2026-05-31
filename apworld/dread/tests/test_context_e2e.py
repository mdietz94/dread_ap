"""End-to-end test of the Switch→AP path inside DreadContext.

Asserts that a PACKET_COLLECTED_INDICES push lands on ``_on_switch_push``,
parses the ``locations:`` + bitfield payload, maps to AP location_ids via
the datapackage, dedupes against BridgeState, and emits a ``LocationChecks``
message via ``send_msgs``.

Mocks the AP server connection with a tiny ``send_msgs`` capture; mocks
the Switch executor entirely (we don't need real sockets here, only the
push handler).

Run with:  python -m pytest apworld/dread/tests/test_context_e2e.py -v
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest.mock
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client import lua_packets as lp  # noqa: E402
from dread.client.datapackage import DataPackage  # noqa: E402
from dread.client.state import BridgeState  # noqa: E402

DATA = ROOT / "data"


def _bitfield_for(pickup_indices: list[int]) -> bytes:
    """Build a ``locations:`` + bitfield payload that the Switch would emit."""
    if not pickup_indices:
        return b"locations:"
    max_bit = max(pickup_indices)
    num_bytes = (max_bit // 8) + 1
    buf = bytearray(num_bytes)
    for idx in pickup_indices:
        buf[idx // 8] |= 1 << (idx % 8)
    return b"locations:" + bytes(buf)


@pytest.fixture
def ctx():
    """Build a DreadContext with mocked AP-server hookup."""
    from dread.client.context import DreadContext  # noqa: E402

    state = BridgeState()
    dp = DataPackage(apworld_data_dir=DATA)
    c = DreadContext(
        server_address=None,
        password=None,
        state=state,
        datapackage=dp,
        switch_host="127.0.0.1",
    )
    # Replace send_msgs with an awaitable mock that records calls.
    c.send_msgs = unittest.mock.AsyncMock()  # type: ignore[method-assign]
    return c


@pytest.mark.asyncio
async def test_collected_indices_push_emits_location_checks(ctx):
    # Build a payload claiming pickup_indices 0, 1, 5 are collected.
    payload = _bitfield_for([0, 1, 5])
    resp = lp.Response(success=True, payload=payload)
    await ctx._on_switch_push(lp.PacketType.COLLECTED_INDICES, resp)

    # send_msgs should have been called exactly once with three AP location_ids.
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
    # State should also have absorbed them.
    assert ctx.state.all_collected_ids() == set(expected_ids)


@pytest.mark.asyncio
async def test_duplicate_indices_dont_double_send(ctx):
    """The bootstrap Lua dumps the FULL collected set on every poll tick.
    Sending an identical push twice must not emit a second LocationChecks."""
    payload = _bitfield_for([0, 1, 5])
    resp = lp.Response(success=True, payload=payload)

    await ctx._on_switch_push(lp.PacketType.COLLECTED_INDICES, resp)
    await ctx._on_switch_push(lp.PacketType.COLLECTED_INDICES, resp)

    # Only one send_msgs call: the second push had no new IDs after dedup.
    assert ctx.send_msgs.await_count == 1


@pytest.mark.asyncio
async def test_partial_overlap_only_sends_new_indices(ctx):
    """First push: [0, 1]. Second push: [0, 1, 5]. Only 5 should be sent
    the second time."""
    first = _bitfield_for([0, 1])
    second = _bitfield_for([0, 1, 5])

    await ctx._on_switch_push(lp.PacketType.COLLECTED_INDICES,
                              lp.Response(success=True, payload=first))
    await ctx._on_switch_push(lp.PacketType.COLLECTED_INDICES,
                              lp.Response(success=True, payload=second))

    assert ctx.send_msgs.await_count == 2
    second_call_args, _ = ctx.send_msgs.await_args_list[1]
    second_msgs = second_call_args[0]
    expected_5_id = ctx.datapackage.pickup_index_to_location_id(5)
    assert second_msgs[0]["locations"] == [expected_5_id]


@pytest.mark.asyncio
async def test_unknown_index_is_skipped(ctx):
    """A bootstrap Lua might emit a pickup_index outside the 0..148 range
    (e.g. if Randovania extends the model). Defensive default: skip + don't
    crash, don't send a LocationChecks with stray IDs."""
    # Use a payload where bit 200 is set — beyond our 149 known pickups.
    payload = _bitfield_for([200])
    resp = lp.Response(success=True, payload=payload)
    await ctx._on_switch_push(lp.PacketType.COLLECTED_INDICES, resp)
    # No LocationChecks should fire because no known location matched.
    ctx.send_msgs.assert_not_called()


@pytest.mark.asyncio
async def test_empty_bitfield_emits_nothing(ctx):
    """Just the ``locations:`` prefix with no bitfield bytes — nothing
    collected yet."""
    resp = lp.Response(success=True, payload=b"locations:")
    await ctx._on_switch_push(lp.PacketType.COLLECTED_INDICES, resp)
    ctx.send_msgs.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_prefix_is_logged_not_sent(ctx):
    """If the payload lacks the ``locations:`` prefix, we log + skip rather
    than guessing at a different parser."""
    resp = lp.Response(success=True, payload=b"surprise_format:\x01\x02\x03")
    await ctx._on_switch_push(lp.PacketType.COLLECTED_INDICES, resp)
    ctx.send_msgs.assert_not_called()
    # State unchanged.
    assert ctx.state.all_collected_ids() == set()


@pytest.mark.asyncio
async def test_game_state_push_updates_state_and_triggers_goal(ctx):
    """When ``Init.bBeatenSinceLastReboot`` flips true, the goal report fires.
    The PACKET_GAME_STATE push carries this directly."""
    resp = lp.Response(success=True, payload=b"s080_shipyard;true")
    await ctx._on_switch_push(lp.PacketType.GAME_STATE, resp)
    assert ctx.state.is_beaten() is True
    # One StatusUpdate goal message goes out
    sent = []
    for call in ctx.send_msgs.await_args_list:
        sent.extend(call.args[0])
    assert any(m.get("cmd") == "StatusUpdate" for m in sent)


@pytest.mark.asyncio
async def test_new_inventory_push_updates_state_mirror(ctx):
    """A NEW_INVENTORY push stashes a positional snapshot."""
    payload = json.dumps({"index": 3, "inventory": [1.0, 2.0, 3.5]}).encode("utf-8")
    resp = lp.Response(success=True, payload=payload)
    await ctx._on_switch_push(lp.PacketType.NEW_INVENTORY, resp)
    inv = ctx.state.get_inventory()
    assert inv["slot0"] == 1
    assert inv["slot1"] == 2
    assert inv["slot2"] == 4  # rounded from 3.5
    ctx.send_msgs.assert_not_called()


@pytest.mark.asyncio
async def test_log_message_push_added_to_log_surface(ctx):
    resp = lp.Response(success=True, payload=b"hello from lua")
    await ctx._on_switch_push(lp.PacketType.LOG_MESSAGE, resp)
    assert "hello from lua" in ctx.state.last_messages
    ctx.send_msgs.assert_not_called()
