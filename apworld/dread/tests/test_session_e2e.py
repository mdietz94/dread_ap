"""Full-session integration test: the REAL DreadContext over a loopback
socket against a stateful fake Dread game.

Under the SMO-style inverted topology, the PC listens (UDP discovery +
TCP) and the Switch dials in. The fake mirrors that — it UDP-probes the
context's discovery responder, then TCP-connects to the advertised
listener, then runs the same wire protocol.

Nothing is mocked but the AP-server ``send_msgs`` sink. The context
runs the real bootstrap + read loop + delivery code end-to-end.
Exercises:

  * listener startup → fake UDP-discovers → fake TCP-dials → handshake
    → API probe → bootstrap (RL.* defined on the Switch),
  * the player collecting pickups → poll → ``LocationChecks`` to AP,
  * AP items delivered via ``RL.ReceivePickup`` landing in the game's
    inventory, in order, exactly once,
  * idempotence by construction: a Switch reconnect against a live game
    does NOT re-grant (the game's ReceivedPickups counter is the cursor),
  * cutscene-safety: a pickup delivered mid-cinematic is held pending
    (not dropped, not counted) until interaction resumes,
  * the goal flag → ``StatusUpdate(CLIENT_GOAL)``.

Run with:  python -m pytest apworld/dread/tests/test_session_e2e.py -v
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client.context import DreadContext  # noqa: E402
from dread.client.datapackage import DataPackage  # noqa: E402
from dread.client.state import BridgeState  # noqa: E402
from dread.tests.fakeswitch import FakeDreadGame  # noqa: E402

from NetUtils import ClientStatus  # noqa: E402

DATA = ROOT / "data"
MISSILE_ITEM = "ITEM_WEAPON_MISSILE_MAX"


# ---- helpers --------------------------------------------------------------

def _ap_id_for(dp: DataPackage, name: str) -> int:
    for ap_id, n in dp._ap_id_to_name.items():
        if n == name:
            return ap_id
    raise KeyError(f"no AP item id for {name!r}")


def _network_item(ap_id: int, sender_slot: int = 1) -> tuple:
    """A NetworkItem-shaped tuple (item, location, player, flags)."""
    return (ap_id, 0, sender_slot, 0)


def _all_sent(ctx: DreadContext) -> list[dict]:
    out: list[dict] = []
    for call in ctx.send_msgs.await_args_list:
        out.extend(call.args[0])
    return out


async def _await_until(predicate, timeout: float = 2.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def _make_ctx() -> tuple[DreadContext, DataPackage]:
    state = BridgeState()
    dp = DataPackage(apworld_data_dir=DATA)
    ctx = DreadContext(
        None, None, state=state, datapackage=dp,
        listen_host="127.0.0.1", listen_port=0, discovery_port=0,
    )
    ctx.send_msgs = unittest.mock.AsyncMock()  # type: ignore[method-assign]
    return ctx, dp


async def _start_session(
    ctx: DreadContext, fake: FakeDreadGame,
) -> None:
    """Boot the bridge listener, have the fake discover + dial in, wait for
    the bootstrap to complete, then cancel the auto-running poll task so
    tests can step polls deterministically.
    """
    await ctx.start_switch_listener()
    await fake.dial(discovery_port=ctx.discovery_port)
    assert await _await_until(lambda: ctx._bootstrapped, timeout=5.0), \
        "bootstrap did not complete"
    assert ctx._active_conn is not None
    assert fake.bootstrapped
    if ctx._poll_task is not None:
        ctx._poll_task.cancel()
        try:
            await ctx._poll_task
        except (asyncio.CancelledError, Exception):
            pass
        ctx._poll_task = None


async def _teardown(ctx: DreadContext, fake: FakeDreadGame) -> None:
    """Tear the session down in the right order: close the fake (its
    socket dies → the bridge accept handler unwinds), then shutdown the
    bridge."""
    await fake.disconnect()
    await ctx.shutdown()


async def _drive(
    ctx: DreadContext, fake: FakeDreadGame, target: int, max_polls: int = 30,
) -> None:
    """Poll until the game has confirmed ``target`` received pickups (delivery
    self-clocks one item per poll as the counter advances)."""
    for _ in range(max_polls):
        if fake.received_pickups >= target:
            return
        before = fake.received_pickups
        await ctx._poll_once()
        await _await_until(lambda: fake.received_pickups > before, timeout=1.0)


# ---- tests ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_session_happy_path():
    ctx, dp = _make_ctx()
    fake = FakeDreadGame()
    await _start_session(ctx, fake)
    try:
        # The player collects pickups 0 and 5 in-world.
        fake.collect(0, 5)
        await ctx._poll_once()
        assert await _await_until(lambda: ctx.send_msgs.await_count >= 1)

        checks = [m for m in _all_sent(ctx) if m.get("cmd") == "LocationChecks"]
        forwarded: set[int] = set()
        for m in checks:
            forwarded.update(m["locations"])
        expected = {dp.pickup_index_to_location_id(0), dp.pickup_index_to_location_id(5)}
        assert None not in expected
        assert expected <= forwarded

        # An AP item arrives → delivered via RL.ReceivePickup → granted in-game.
        missile = _ap_id_for(dp, "Missile Tank")
        await ctx._on_received_items({"index": 0, "items": [_network_item(missile)]})
        await _drive(ctx, fake, target=1)
        assert fake.inventory_of(MISSILE_ITEM) == 2  # one Missile Tank = +2
        assert fake.received_pickups == 1

        # The run is beaten → goal reported exactly once.
        fake.beaten = True
        await ctx._poll_once()
        assert await _await_until(
            lambda: any(m.get("cmd") == "StatusUpdate" for m in _all_sent(ctx)))
        statuses = [m for m in _all_sent(ctx) if m.get("cmd") == "StatusUpdate"]
        assert statuses[-1]["status"] == ClientStatus.CLIENT_GOAL
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_bootstrap_defines_rl_namespace_on_connect():
    """The connect must send the RL.* bootstrap (the ROM only has stubs).
    Without it nothing else on the wire works."""
    ctx, _ = _make_ctx()
    fake = FakeDreadGame()
    await _start_session(ctx, fake)
    try:
        assert fake.bootstrapped
        joined = "\n".join(fake.bootstrap_chunks)
        assert "RL.Bootstrap=true" in joined
        assert "function RL.ReceivePickup" in joined
        assert "function RL.GetCollectedIndicesAndSend" in joined
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_collected_dedup_across_polls():
    ctx, dp = _make_ctx()
    fake = FakeDreadGame()
    await _start_session(ctx, fake)
    try:
        fake.collect(0, 1)
        await ctx._poll_once()
        assert await _await_until(lambda: ctx.send_msgs.await_count >= 1)
        first = ctx.send_msgs.await_count

        await ctx._poll_once()
        await asyncio.sleep(0.05)
        assert ctx.send_msgs.await_count == first, "duplicate collected set re-sent"

        fake.collect(9)
        await ctx._poll_once()
        assert await _await_until(lambda: ctx.send_msgs.await_count > first)
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_multiple_items_delivered_in_order_exactly_once():
    ctx, dp = _make_ctx()
    fake = FakeDreadGame()
    await _start_session(ctx, fake)
    try:
        missile = _ap_id_for(dp, "Missile Tank")
        items = [_network_item(missile) for _ in range(3)]
        await ctx._on_received_items({"index": 0, "items": items})
        await _drive(ctx, fake, target=3)
        assert fake.received_pickups == 3
        assert len(fake.onpickedup_calls) == 3
        assert fake.inventory_of(MISSILE_ITEM) == 6  # 3 tanks * 2
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_switch_reconnect_does_not_double_grant():
    """Idempotent by construction: after the Switch disconnects and
    redials, AP resending the same items does NOT re-grant. The game's
    ReceivedPickups counter (preserved across the disconnect) is the cursor."""
    ctx, dp = _make_ctx()
    fake = FakeDreadGame()
    await _start_session(ctx, fake)
    try:
        missile = _ap_id_for(dp, "Missile Tank")
        items = [_network_item(missile), _network_item(missile)]
        await ctx._on_received_items({"index": 0, "items": items})
        await _drive(ctx, fake, target=2)
        assert fake.inventory_of(MISSILE_ITEM) == 4
        assert fake.received_pickups == 2

        # Switch drops + redials. Game state survives on the fake; PC's
        # accept handler unwinds and the listener stays up for the
        # incoming reconnect.
        await fake.disconnect()
        assert await _await_until(
            lambda: ctx._active_conn is None, timeout=3.0)

        fake2 = FakeDreadGame()
        # Carry over game state so we can verify no double-grant.
        fake2.received_pickups = fake.received_pickups
        fake2.inventory_index = fake.inventory_index
        fake2.inventory = dict(fake.inventory)
        fake2.collected_pickup_indices = set(fake.collected_pickup_indices)
        await fake2.dial(discovery_port=ctx.discovery_port)
        assert await _await_until(lambda: ctx._bootstrapped, timeout=5.0)
        if ctx._poll_task is not None:
            ctx._poll_task.cancel()
            try:
                await ctx._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            ctx._poll_task = None

        # AP re-sends both items from index 0; nothing should regrant.
        await ctx._on_received_items({"index": 0, "items": items})
        for _ in range(3):
            await ctx._poll_once()
            await asyncio.sleep(0.02)
        assert fake2.inventory_of(MISSILE_ITEM) == 4    # unchanged
        assert fake2.received_pickups == 2
        assert len(fake2.onpickedup_calls) == 0   # nothing re-granted after reconnect

        await fake2.disconnect()
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_cutscene_delivery_is_deferred_not_dropped():
    """A pickup delivered mid-cinematic is held pending — not granted, not
    counted — until interaction resumes, then granted exactly once. This is the
    upstream RL.ReceivePickup/GivePendingPickup contract that resolves risk #1."""
    ctx, dp = _make_ctx()
    fake = FakeDreadGame()
    fake.in_cutscene = True
    await _start_session(ctx, fake)
    try:
        missile = _ap_id_for(dp, "Missile Tank")
        await ctx._on_received_items({"index": 0, "items": [_network_item(missile)]})
        for _ in range(3):
            await ctx._poll_once()
            await asyncio.sleep(0.02)
        assert fake.inventory_of(MISSILE_ITEM) == 0
        assert fake.received_pickups == 0
        assert fake.has_pending

        fake.end_cutscene()
        assert fake.inventory_of(MISSILE_ITEM) == 2
        assert fake.received_pickups == 1
        assert not fake.has_pending
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_game_restart_without_save_redelivers_lost_items():
    """Player gets 3 AP items, saves at item 1, then restarts WITHOUT saving.
    The game's Blackboard reverts to the save snapshot (ReceivedPickups=1,
    InventoryIndex=1, missile count=2). The PC client must accept the regression
    and re-deliver items 1 and 2, leaving the saved item 0 alone."""
    ctx, dp = _make_ctx()
    fake = FakeDreadGame()
    await _start_session(ctx, fake)
    try:
        missile = _ap_id_for(dp, "Missile Tank")
        items = [_network_item(missile) for _ in range(3)]
        await ctx._on_received_items({"index": 0, "items": items})
        await _drive(ctx, fake, target=3)
        assert fake.received_pickups == 3
        assert fake.inventory_of(MISSILE_ITEM) == 6
        assert len(fake.onpickedup_calls) == 3

        # Save snapshot at received_pickups=1, then restart-without-save.
        fake.received_pickups = 1
        fake.inventory_index = 1
        fake.inventory[MISSILE_ITEM] = 2
        fake.collected_pickup_indices.clear()

        await _drive(ctx, fake, target=3)
        assert fake.received_pickups == 3
        assert fake.inventory_of(MISSILE_ITEM) == 6
        assert len(fake.onpickedup_calls) == 5  # 3 pre-restart + 2 re-delivered
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_inventory_index_regression_alone_resumes_delivery():
    """Inventory-only regression (player saved an AP item, then collected and
    lost a Dread-local pickup): ReceivedPickups unchanged, InventoryIndex drops.
    Subsequent AP item must still deliver once the mirror catches up."""
    ctx, dp = _make_ctx()
    fake = FakeDreadGame()
    await _start_session(ctx, fake)
    try:
        missile = _ap_id_for(dp, "Missile Tank")
        await ctx._on_received_items({"index": 0, "items": [_network_item(missile)]})
        await _drive(ctx, fake, target=1)
        assert fake.received_pickups == 1
        assert fake.inventory_index == 1

        fake.inventory_index = 2
        fake.inventory_index = 1

        await ctx._on_received_items({"index": 1, "items": [_network_item(missile)]})
        await _drive(ctx, fake, target=2)
        assert fake.received_pickups == 2
        assert fake.inventory_of(MISSILE_ITEM) == 4
        assert len(fake.onpickedup_calls) == 2
    finally:
        await _teardown(ctx, fake)
