"""Full-session integration test: the REAL DreadContext + BridgeServer over a
loopback socket against a stateful fake Dread game.

Nothing is mocked but the AP-server ``send_msgs`` sink. The fake dials in to
:class:`BridgeServer`, sends HELLO, the bridge fires ``_on_switch_ready`` which
runs the real bootstrap, the real ``protocol`` / ``context`` code runs end to
end. Exercises:

  * Switch-initiated TCP + HELLO/HELLO_ACK,
  * line-delimited JSON envelope around the existing ``RL.*`` bootstrap,
  * the player collecting pickups → poll → ``LocationChecks`` to AP,
  * AP items delivered via ``RL.ReceivePickup`` landing in the game's
    inventory, in order, exactly once,
  * idempotence by construction (client restart doesn't re-grant),
  * cutscene-safe deferral,
  * goal flag → ``StatusUpdate(CLIENT_GOAL)``.

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


def _ap_id_for(dp: DataPackage, name: str) -> int:
    for ap_id, n in dp._ap_id_to_name.items():
        if n == name:
            return ap_id
    raise KeyError(f"no AP item id for {name!r}")


def _network_item(ap_id: int, sender_slot: int = 1) -> tuple:
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


async def _free_port() -> int:
    """Bind+release a TCP port to discover a free one for this test."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _setup(state: BridgeState | None = None):
    """Build DreadContext + BridgeServer + FakeDreadGame, wire them up, wait
    for HELLO_ACK + bootstrap, then cancel the bridge's spawned poll task so
    the test drives timing.

    Returns ``(ctx, dp, fake)``. Caller is responsible for ``await _teardown(...)``.
    """
    state = state or BridgeState()
    dp = DataPackage(apworld_data_dir=DATA)
    bridge_port = await _free_port()
    ctx = DreadContext(None, None, state=state, datapackage=dp,
                       bridge_port=bridge_port,
                       discovery_port=0)  # 0 = disable discovery in tests
    ctx.send_msgs = unittest.mock.AsyncMock()  # type: ignore[method-assign]
    # Only start the BridgeServer — skip the DiscoveryResponder in tests by
    # not calling start_bridge(). We bind it manually.
    from dread.client.bridge_server import BridgeServer
    ctx._bridge = BridgeServer(
        slot="Samus", seed="test-seed",
        port=bridge_port,
        on_push=ctx._on_switch_push,
        on_active_connected=ctx._on_switch_ready,
        on_active_disconnected=ctx._on_switch_gone,
    )
    await ctx._bridge.start()

    fake = FakeDreadGame()
    await fake.connect(host="127.0.0.1", port=bridge_port)
    await fake.wait_for_hello_ack(timeout=5.0)

    # Wait for bootstrap to complete (ctx._bootstrapped flips when
    # _on_switch_ready returns). _on_switch_ready also starts a poll task.
    assert await _await_until(lambda: ctx._bootstrapped, timeout=10.0), (
        "bootstrap did not complete")
    if ctx._poll_task is not None:
        ctx._poll_task.cancel()
        try:
            await ctx._poll_task
        except asyncio.CancelledError:
            pass
        ctx._poll_task = None
    return ctx, dp, fake


async def _teardown(ctx: DreadContext, fake: FakeDreadGame) -> None:
    await fake.stop()
    if ctx._bridge is not None:
        await ctx._bridge.close()
        ctx._bridge = None


async def _drive(ctx: DreadContext, fake: FakeDreadGame, target: int, max_polls: int = 30):
    for _ in range(max_polls):
        if fake.received_pickups >= target:
            return
        before = fake.received_pickups
        await ctx._poll_once()
        await _await_until(lambda: fake.received_pickups > before, timeout=1.0)


# ---- tests ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_session_happy_path():
    ctx, dp, fake = await _setup()
    try:
        fake.collect(0, 5)
        await ctx._poll_once()
        assert await _await_until(lambda: ctx.send_msgs.await_count >= 1)

        checks = [m for m in _all_sent(ctx) if m.get("cmd") == "LocationChecks"]
        forwarded: set[int] = set()
        for m in checks:
            forwarded.update(m["locations"])
        expected = {dp.pickup_index_to_location_id(0),
                    dp.pickup_index_to_location_id(5)}
        assert None not in expected
        assert expected <= forwarded

        missile = _ap_id_for(dp, "Missile Tank")
        await ctx._on_received_items({"index": 0, "items": [_network_item(missile)]})
        await _drive(ctx, fake, target=1)
        assert fake.inventory_of(MISSILE_ITEM) == 2
        assert fake.received_pickups == 1

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
    ctx, _, fake = await _setup()
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
    ctx, dp, fake = await _setup()
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
    ctx, dp, fake = await _setup()
    try:
        missile = _ap_id_for(dp, "Missile Tank")
        items = [_network_item(missile) for _ in range(3)]
        await ctx._on_received_items({"index": 0, "items": items})
        await _drive(ctx, fake, target=3)
        assert fake.received_pickups == 3
        assert len(fake.onpickedup_calls) == 3
        assert fake.inventory_of(MISSILE_ITEM) == 6
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_client_restart_does_not_double_grant():
    """A fresh client against a game that already applied N pickups reads
    ReceivedPickups=N and delivers nothing extra."""
    ctx1, dp, fake = await _setup()
    try:
        missile = _ap_id_for(dp, "Missile Tank")
        items = [_network_item(missile), _network_item(missile)]
        await ctx1._on_received_items({"index": 0, "items": items})
        await _drive(ctx1, fake, target=2)
        assert fake.inventory_of(MISSILE_ITEM) == 4
        assert fake.received_pickups == 2
        # Close ctx1's bridge so fake's TCP closes too.
        await ctx1._bridge.close()
        ctx1._bridge = None
        await fake.stop()
    finally:
        # ctx1 already torn down.
        pass

    # Fresh setup against the same fake (rebuild the fake too — TCP-level state
    # would otherwise leak). The "fresh client" semantic in the new wire is a
    # new BridgeServer + a new FakeDreadGame dial, but the fake retains its
    # game-state via a deliberate copy of just the counters/inventory.
    fake2 = FakeDreadGame()
    fake2.received_pickups = 2
    fake2.inventory_index = 2
    fake2.inventory[MISSILE_ITEM] = 4
    fake2.onpickedup_calls = list(fake.onpickedup_calls)

    bridge_port = await _free_port()
    dp = DataPackage(apworld_data_dir=DATA)
    ctx2 = DreadContext(None, None, state=BridgeState(), datapackage=dp,
                        bridge_port=bridge_port, discovery_port=0)
    ctx2.send_msgs = unittest.mock.AsyncMock()  # type: ignore[method-assign]
    from dread.client.bridge_server import BridgeServer
    ctx2._bridge = BridgeServer(
        slot="Samus", seed="test-seed", port=bridge_port,
        on_push=ctx2._on_switch_push,
        on_active_connected=ctx2._on_switch_ready,
        on_active_disconnected=ctx2._on_switch_gone,
    )
    await ctx2._bridge.start()
    await fake2.connect("127.0.0.1", bridge_port)
    await fake2.wait_for_hello_ack(5.0)
    assert await _await_until(lambda: ctx2._bootstrapped, timeout=10.0)
    if ctx2._poll_task is not None:
        ctx2._poll_task.cancel()
        try:
            await ctx2._poll_task
        except asyncio.CancelledError:
            pass
        ctx2._poll_task = None

    try:
        missile = _ap_id_for(dp, "Missile Tank")
        await ctx2._on_received_items({"index": 0, "items": [
            _network_item(missile), _network_item(missile),
        ]})
        for _ in range(3):
            await ctx2._poll_once()
            await asyncio.sleep(0.02)
        # No re-grant (cursor matched).
        assert fake2.inventory_of(MISSILE_ITEM) == 4
        assert fake2.received_pickups == 2
        assert len(fake2.onpickedup_calls) == 2
    finally:
        await _teardown(ctx2, fake2)


@pytest.mark.asyncio
async def test_cutscene_delivery_is_deferred_not_dropped():
    ctx, dp, fake = await _setup()
    fake.in_cutscene = True
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
    ctx, dp, fake = await _setup()
    try:
        missile = _ap_id_for(dp, "Missile Tank")
        items = [_network_item(missile) for _ in range(3)]
        await ctx._on_received_items({"index": 0, "items": items})
        await _drive(ctx, fake, target=3)
        assert fake.received_pickups == 3
        assert fake.inventory_of(MISSILE_ITEM) == 6
        assert len(fake.onpickedup_calls) == 3

        # Save snapshot at received_pickups=1.
        fake.received_pickups = 1
        fake.inventory_index = 1
        fake.inventory[MISSILE_ITEM] = 2
        fake.collected_pickup_indices.clear()

        await _drive(ctx, fake, target=3)
        assert fake.received_pickups == 3
        assert fake.inventory_of(MISSILE_ITEM) == 6
        assert len(fake.onpickedup_calls) == 5
    finally:
        await _teardown(ctx, fake)
