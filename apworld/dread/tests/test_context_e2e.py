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


def _quiet_connect_deps(ctx, monkeypatch):
    """Neutralise the side effects _on_connected fires besides the collected-
    location reconciliation we're testing (scout request, death-link tag,
    auto-patch), so send_msgs only captures the re-sync we assert on."""
    import dread.client.context as ctxmod
    monkeypatch.setattr(ctxmod, "request_scout", unittest.mock.AsyncMock())
    ctx.update_death_link = unittest.mock.AsyncMock()  # type: ignore[method-assign]
    ctx._maybe_auto_patch = unittest.mock.AsyncMock()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_seed_change_resets_collected_mirror(ctx, monkeypatch):
    """Regression: a regenerated seed in the same client process must drop the
    collected-location mirror. pickup_index is seed-independent, so a location
    collected under the old seed would otherwise dedupe-suppress the SAME
    position (a different player's item now) under the new seed — silently
    losing that outgoing check. (This is exactly how an outgoing Morph Ball got
    dropped: collected under one seed, then re-collected under a regenerated
    seed where it held the co-op partner's Morph Ball, but never re-forwarded.)
    """
    _quiet_connect_deps(ctx, monkeypatch)

    await ctx._on_connected({"seed_name": "AP-aaaa", "slot_data": {}})
    await ctx._on_switch_push(W.Collected(hex=_hex_for([5])))
    loc5 = ctx.datapackage.pickup_index_to_location_id(5)
    assert ctx.state.all_collected_ids() == {loc5}

    # Regenerate → connect to a DIFFERENT seed. Mirror must reset.
    await ctx._on_connected({"seed_name": "AP-bbbb", "slot_data": {}})
    assert ctx.state.all_collected_ids() == set()

    # Re-collecting the same position now forwards again (the new seed's check).
    ctx.send_msgs.reset_mock()
    await ctx._on_switch_push(W.Collected(hex=_hex_for([5])))
    ctx.send_msgs.assert_awaited_once()
    args, _ = ctx.send_msgs.await_args
    assert args[0][0]["cmd"] == "LocationChecks"
    assert args[0][0]["locations"] == [loc5]


@pytest.mark.asyncio
async def test_same_seed_reconnect_resyncs_collected(ctx, monkeypatch):
    """A reconnect to the SAME seed must re-assert every known-collected
    location. A LocationCheck emitted while the socket was down (a pickup
    grabbed during an AP disconnect) is otherwise lost for good — the dedupe
    cache suppresses the re-forward on the next bitfield push."""
    _quiet_connect_deps(ctx, monkeypatch)

    await ctx._on_connected({"seed_name": "AP-aaaa", "slot_data": {}})
    await ctx._on_switch_push(W.Collected(hex=_hex_for([0, 5])))
    ids = sorted({ctx.datapackage.pickup_index_to_location_id(i) for i in (0, 5)})

    ctx.send_msgs.reset_mock()
    await ctx._on_connected({"seed_name": "AP-aaaa", "slot_data": {}})
    ctx.send_msgs.assert_awaited_once()
    args, _ = ctx.send_msgs.await_args
    assert args[0][0]["cmd"] == "LocationChecks"
    assert sorted(args[0][0]["locations"]) == ids
    # Same-seed reconnect does NOT wipe the mirror.
    assert ctx.state.all_collected_ids() == set(ids)


@pytest.mark.asyncio
async def test_first_connect_records_seed_without_resync(ctx, monkeypatch):
    """The first connect just baselines the seed — nothing collected yet, so no
    LocationChecks should be emitted by the connect itself."""
    _quiet_connect_deps(ctx, monkeypatch)
    await ctx._on_connected({"seed_name": "AP-aaaa", "slot_data": {}})
    assert ctx._synced_seed == "AP-aaaa"
    location_checks = [
        m for call in ctx.send_msgs.await_args_list for m in call.args[0]
        if m.get("cmd") == "LocationChecks"
    ]
    assert location_checks == []


@pytest.mark.asyncio
async def test_connect_subscribes_warp_storage_key(ctx, monkeypatch):
    """Connect binds a seed+slot scoped DataStorage key and subscribes to it, so
    the visited-warp set survives a client restart."""
    _quiet_connect_deps(ctx, monkeypatch)
    await ctx._on_connected({"seed_name": "AP-aaaa", "slot_data": {}})
    assert ctx._warp_visited_key == f"dread_warp_visited_AP-aaaa_{ctx.team}_{ctx.slot}"
    assert ctx._warp_visited_key in ctx.stored_data_notification_keys


@pytest.mark.asyncio
async def test_seed_change_clears_visited_and_rekeys(ctx, monkeypatch):
    """A different seed drops the in-memory visited set (a station reached in the
    prior seed isn't necessarily reached here) and rebinds to the new seed's key;
    that key's own Retrieved restores the correct set."""
    _quiet_connect_deps(ctx, monkeypatch)
    await ctx._on_connected({"seed_name": "AP-aaaa", "slot_data": {}})
    ctx._visited_saves.add(("s030_baselab", "collision_camera_000"))

    await ctx._on_connected({"seed_name": "AP-bbbb", "slot_data": {}})
    assert ctx._visited_saves == set()
    assert ctx._warp_visited_key == f"dread_warp_visited_AP-bbbb_{ctx.team}_{ctx.slot}"


# ---- only the slot's OWN locations go on the wire (issue #172) --------------
#
# World._compute_dropped_locations omits pickups a full loadout can't reach under
# the slot's options (e.g. the 8 Speedbooster-gated spots with that trick
# disabled), so the slot holds a SUBSET of the static data-package table.
# LocationScouts on an id the slot lacks raises KeyError inside AP's MultiServer,
# which drops the client -> endless reconnect loop.

_DROPPED = 31208          # "Cataris: Dairon Transport Access", Speedbooster-gated


def _connected_args(ctx, *, dropped: set[int] = frozenset(), **extra) -> dict:
    """A Connected packet whose location lists cover the data package minus
    ``dropped`` — the shape the server sends for a slot with dropped locations."""
    held = [i for i in ctx.datapackage.all_location_ids() if i not in dropped]
    return {"seed_name": "AP-aaaa", "slot_data": {},
            "missing_locations": held[1:], "checked_locations": held[:1], **extra}


@pytest.mark.asyncio
async def test_scout_skips_locations_the_slot_does_not_have(ctx, monkeypatch):
    """Regression for #172: a dropped location must never reach LocationScouts."""
    import dread.client.context as ctxmod
    scout = unittest.mock.AsyncMock()
    monkeypatch.setattr(ctxmod, "request_scout", scout)
    ctx.update_death_link = unittest.mock.AsyncMock()  # type: ignore[method-assign]
    ctx._maybe_auto_patch = unittest.mock.AsyncMock()  # type: ignore[method-assign]

    all_ids = ctx.datapackage.all_location_ids()
    assert _DROPPED in all_ids                      # pin the fixture to real data
    await ctx._on_connected(_connected_args(ctx, dropped={_DROPPED}))

    scout.assert_awaited_once()
    requested = scout.await_args.args[1]
    assert _DROPPED not in requested
    assert sorted(requested) == sorted(i for i in all_ids if i != _DROPPED)


@pytest.mark.asyncio
async def test_scout_requests_full_table_when_nothing_is_dropped(ctx, monkeypatch):
    """The common case (no drops) is unchanged — every location is scouted."""
    import dread.client.context as ctxmod
    scout = unittest.mock.AsyncMock()
    monkeypatch.setattr(ctxmod, "request_scout", scout)
    ctx.update_death_link = unittest.mock.AsyncMock()  # type: ignore[method-assign]
    ctx._maybe_auto_patch = unittest.mock.AsyncMock()  # type: ignore[method-assign]

    await ctx._on_connected(_connected_args(ctx))
    assert (sorted(scout.await_args.args[1])
            == sorted(ctx.datapackage.all_location_ids()))


@pytest.mark.asyncio
async def test_scout_unfiltered_without_server_location_lists(ctx, monkeypatch):
    """A Connected packet with no location lists leaves us no filter to apply, so
    fall back to the full table rather than scouting nothing."""
    import dread.client.context as ctxmod
    scout = unittest.mock.AsyncMock()
    monkeypatch.setattr(ctxmod, "request_scout", scout)
    ctx.update_death_link = unittest.mock.AsyncMock()  # type: ignore[method-assign]
    ctx._maybe_auto_patch = unittest.mock.AsyncMock()  # type: ignore[method-assign]

    await ctx._on_connected({"seed_name": "AP-aaaa", "slot_data": {}})
    assert (sorted(scout.await_args.args[1])
            == sorted(ctx.datapackage.all_location_ids()))


@pytest.mark.asyncio
async def test_nav_hint_skips_own_location_not_in_slot(ctx, monkeypatch):
    """A plaque pointing at a location this slot doesn't hold must not be hinted
    — an own-slot LocationScouts there is the same fatal server-side KeyError."""
    from dread.client.protocol import NAV_HINT_STATIONS
    _quiet_connect_deps(ctx, monkeypatch)
    await ctx._on_connected(_connected_args(ctx, dropped={_DROPPED}))

    sent: list = []
    ctx.send_msgs = unittest.mock.AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda m: sent.extend(m))
    ctx._nav_hint_by_camera = {NAV_HINT_STATIONS[0]: [(ctx.slot, _DROPPED)]}
    await ctx._register_nav_hints(NAV_HINT_STATIONS[0])
    assert sent == []

    # A held location at the same station still registers.
    held = next(i for i in ctx.datapackage.all_location_ids() if i != _DROPPED)
    ctx._nav_hint_by_camera = {NAV_HINT_STATIONS[0]: [(ctx.slot, held)]}
    await ctx._register_nav_hints(NAV_HINT_STATIONS[0])
    assert sent == [{"cmd": "LocationScouts", "locations": [held],
                     "create_as_hint": 2}]
