"""Full-session integration test: the REAL DreadContext over a loopback socket
against a stateful fake Dread game.

Nothing is mocked but the AP-server ``send_msgs`` sink. The context dials
:class:`FakeDreadGame` over TCP, sends the real ``RL.*`` bootstrap, the real
``lua_executor`` read loop demuxes frames, and the real ``protocol``/``context``
code runs end to end. Exercises:

  * connect → handshake → API probe → bootstrap (RL.* defined on the Switch),
  * the player collecting pickups → poll → ``LocationChecks`` to AP,
  * AP items delivered via ``RL.ReceivePickup`` landing in the game's inventory,
    in order, exactly once,
  * idempotence by construction: a client restart against a live game does NOT
    re-grant (the game's ReceivedPickups counter is the cursor),
  * cutscene-safety: a pickup delivered mid-cinematic is held pending (not
    dropped, not counted) until interaction resumes,
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


async def _connect(fake: FakeDreadGame, state: BridgeState | None = None):
    """Build a DreadContext wired to the fake (with a captured send_msgs), let
    it bootstrap, then cancel the background poll so the test drives timing."""
    state = state or BridgeState()
    dp = DataPackage(apworld_data_dir=DATA)
    ctx = DreadContext(None, None, state=state, datapackage=dp,
                       switch_host="127.0.0.1", switch_port=fake.port)
    ctx.send_msgs = unittest.mock.AsyncMock()  # type: ignore[method-assign]
    await ctx.connect_switch()
    assert ctx.executor is not None, "connect_switch failed (dial or bootstrap)"
    assert ctx._bootstrapped and fake.bootstrapped, "bootstrap not completed"
    if ctx._poll_task is not None:
        ctx._poll_task.cancel()
        try:
            await ctx._poll_task
        except asyncio.CancelledError:
            pass
        ctx._poll_task = None
    return ctx, dp


async def _drive(ctx: DreadContext, fake: FakeDreadGame, target: int, max_polls: int = 30):
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
    fake = FakeDreadGame()
    await fake.start()
    ctx, dp = await _connect(fake)
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
        await ctx.executor.close()
        await fake.stop()


@pytest.mark.asyncio
async def test_bootstrap_defines_rl_namespace_on_connect():
    """The connect must send the RL.* bootstrap (the ROM only has stubs).
    Without it nothing else on the wire works."""
    fake = FakeDreadGame()
    await fake.start()
    ctx, _ = await _connect(fake)
    try:
        assert fake.bootstrapped
        # The final block flips the flag; earlier chunks carry the functions.
        joined = "\n".join(fake.bootstrap_chunks)
        assert "RL.Bootstrap=true" in joined
        assert "function RL.ReceivePickup" in joined
        assert "function RL.GetCollectedIndicesAndSend" in joined
    finally:
        await ctx.executor.close()
        await fake.stop()


@pytest.mark.asyncio
async def test_collected_dedup_across_polls():
    fake = FakeDreadGame()
    await fake.start()
    ctx, dp = await _connect(fake)
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
        await ctx.executor.close()
        await fake.stop()


@pytest.mark.asyncio
async def test_multiple_items_delivered_in_order_exactly_once():
    fake = FakeDreadGame()
    await fake.start()
    ctx, dp = await _connect(fake)
    try:
        missile = _ap_id_for(dp, "Missile Tank")
        items = [_network_item(missile) for _ in range(3)]
        await ctx._on_received_items({"index": 0, "items": items})
        await _drive(ctx, fake, target=3)
        assert fake.received_pickups == 3
        assert len(fake.onpickedup_calls) == 3      # each granted exactly once
        assert fake.inventory_of(MISSILE_ITEM) == 6  # 3 tanks * 2
    finally:
        await ctx.executor.close()
        await fake.stop()


@pytest.mark.asyncio
async def test_client_restart_does_not_double_grant():
    """Idempotent by construction: a fresh client against a game that already
    applied N pickups reads ReceivedPickups=N and delivers nothing extra."""
    fake = FakeDreadGame()
    await fake.start()
    try:
        ctx1, dp = await _connect(fake)
        missile = _ap_id_for(dp, "Missile Tank")
        items = [_network_item(missile), _network_item(missile)]
        await ctx1._on_received_items({"index": 0, "items": items})
        await _drive(ctx1, fake, target=2)
        assert fake.inventory_of(MISSILE_ITEM) == 4
        assert fake.received_pickups == 2
        await ctx1.executor.close()

        # Restart: fresh state/cursor. AP resends both items from index 0.
        ctx2, _ = await _connect(fake)
        await ctx2._on_received_items({"index": 0, "items": items})
        # Let it poll a few times; nothing should be re-granted.
        for _ in range(3):
            await ctx2._poll_once()
            await asyncio.sleep(0.02)
        assert fake.inventory_of(MISSILE_ITEM) == 4    # unchanged
        assert fake.received_pickups == 2
        assert len(fake.onpickedup_calls) == 2
        await ctx2.executor.close()
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_cutscene_delivery_is_deferred_not_dropped():
    """A pickup delivered mid-cinematic is held pending — not granted, not
    counted — until interaction resumes, then granted exactly once. This is the
    upstream RL.ReceivePickup/GivePendingPickup contract that resolves risk #1."""
    fake = FakeDreadGame()
    fake.in_cutscene = True
    await fake.start()
    ctx, dp = await _connect(fake)
    try:
        missile = _ap_id_for(dp, "Missile Tank")
        await ctx._on_received_items({"index": 0, "items": [_network_item(missile)]})
        # Re-attempt a few times while the cutscene plays.
        for _ in range(3):
            await ctx._poll_once()
            await asyncio.sleep(0.02)
        assert fake.inventory_of(MISSILE_ITEM) == 0   # held, not granted
        assert fake.received_pickups == 0             # counter unmoved
        assert fake.has_pending                       # one pickup queued

        # Cutscene ends → the pending pickup is granted exactly once.
        fake.end_cutscene()
        assert fake.inventory_of(MISSILE_ITEM) == 2
        assert fake.received_pickups == 1
        assert not fake.has_pending
    finally:
        await ctx.executor.close()
        await fake.stop()


@pytest.mark.asyncio
async def test_game_restart_without_save_redelivers_lost_items():
    """Player gets 3 AP items, saves at item 1, then restarts WITHOUT saving.
    The game's Blackboard reverts to the save snapshot (ReceivedPickups=1,
    InventoryIndex=1, missile count=2). The PC client must accept the regression
    and re-deliver items 1 and 2, leaving the saved item 0 alone."""
    fake = FakeDreadGame()
    await fake.start()
    ctx, dp = await _connect(fake)
    try:
        missile = _ap_id_for(dp, "Missile Tank")
        items = [_network_item(missile) for _ in range(3)]
        await ctx._on_received_items({"index": 0, "items": items})
        await _drive(ctx, fake, target=3)
        assert fake.received_pickups == 3
        assert fake.inventory_of(MISSILE_ITEM) == 6
        assert len(fake.onpickedup_calls) == 3

        # Simulate the save snapshot at received_pickups=1 (one AP missile
        # already saved), then a restart-without-save: Blackboard reverts to
        # that snapshot, collected bitfield clears.
        fake.received_pickups = 1
        fake.inventory_index = 1
        fake.inventory[MISSILE_ITEM] = 2
        fake.collected_pickup_indices.clear()

        # Drive polls. Items 1 and 2 should re-deliver; item 0 must NOT.
        await _drive(ctx, fake, target=3)
        assert fake.received_pickups == 3
        assert fake.inventory_of(MISSILE_ITEM) == 6
        assert len(fake.onpickedup_calls) == 5  # 3 pre-restart + 2 re-delivered
    finally:
        await ctx.executor.close()
        await fake.stop()


@pytest.mark.asyncio
async def test_inventory_index_regression_alone_resumes_delivery():
    """Inventory-only regression (player saved an AP item, then collected and
    lost a Dread-local pickup): ReceivedPickups unchanged, InventoryIndex drops.
    Subsequent AP item must still deliver once the mirror catches up."""
    fake = FakeDreadGame()
    await fake.start()
    ctx, dp = await _connect(fake)
    try:
        missile = _ap_id_for(dp, "Missile Tank")
        await ctx._on_received_items({"index": 0, "items": [_network_item(missile)]})
        await _drive(ctx, fake, target=1)
        assert fake.received_pickups == 1
        assert fake.inventory_index == 1

        # Player collects a local pickup in-world (InventoryIndex bumps to 2),
        # then restarts without saving — InventoryIndex reverts to 1, but
        # ReceivedPickups stayed at the saved value of 1.
        fake.inventory_index = 2
        fake.inventory_index = 1

        # A second AP item arrives.
        await ctx._on_received_items({"index": 1, "items": [_network_item(missile)]})
        await _drive(ctx, fake, target=2)
        assert fake.received_pickups == 2
        assert fake.inventory_of(MISSILE_ITEM) == 4
        assert len(fake.onpickedup_calls) == 2
    finally:
        await ctx.executor.close()
        await fake.stop()
