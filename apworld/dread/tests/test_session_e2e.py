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
import re
import sys
import time
import unittest.mock
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client import wire as W  # noqa: E402
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


async def _setup(state: BridgeState | None = None, remote_items: bool = False):
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
                       discovery_port=0,  # 0 = disable discovery in tests
                       remote_items=remote_items)
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

def test_remote_items_constructor_sets_items_handling():
    """remote_items raises items_handling to 0b011 (receive OWN items too) so the
    Connect packet declares it before slot_data arrives; default stays 0b001."""
    dp = DataPackage(apworld_data_dir=DATA)
    local = DreadContext(None, None, state=BridgeState(), datapackage=dp,
                         discovery_port=0)
    assert local.items_handling == 0b001
    assert local._remote_items is False

    remote = DreadContext(None, None, state=BridgeState(), datapackage=dp,
                          discovery_port=0, remote_items=True)
    assert remote.items_handling == 0b011
    assert remote._remote_items is True


@pytest.mark.asyncio
async def test_remote_items_delivers_own_item_over_wire():
    """With remote_items on, an OWN item arriving via ReceivedItems is delivered
    to the game exactly like a cross-world item (same RL.ReceivePickup path)."""
    ctx, dp, fake = await _setup(remote_items=True)
    try:
        assert ctx.items_handling == 0b011
        missile = _ap_id_for(dp, "Missile Tank")
        await ctx._on_received_items({"index": 0, "items": [_network_item(missile)]})
        await _drive(ctx, fake, target=1)
        assert fake.received_pickups == 1
        assert fake.inventory_of(MISSILE_ITEM) == 2
    finally:
        await _teardown(ctx, fake)


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
async def test_item_amounts_override_wire_delivery_quantity():
    """A wire-delivered (multiworld) Missile Tank must grant the seed's
    configured ``item_amounts`` amount, not the static items.json default (2).
    ``_item_amounts`` is populated from slot_data on connect; set it directly
    here since _setup doesn't route through the slot_data handler."""
    ctx, dp, fake = await _setup()
    try:
        ctx._item_amounts = {"Missile Tank": 5}
        missile = _ap_id_for(dp, "Missile Tank")
        await ctx._on_received_items({"index": 0, "items": [_network_item(missile)]})
        await _drive(ctx, fake, target=1)
        assert fake.received_pickups == 1
        # 5 (the option), not the items.json default of 2.
        assert fake.inventory_of(MISSILE_ITEM) == 5
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
async def test_progressive_beam_advances_next_missing_tier_then_saturates():
    """Delivering Progressive Beam repeatedly grants Wide → Plasma → Wave in
    order (the game picks the next missing tier from the full progression the
    client sends every time), and a delivery past the last tier grants nothing
    new while still advancing the cursor exactly-once."""
    ctx, dp, fake = await _setup()
    try:
        beam = _ap_id_for(dp, "Progressive Beam")
        await ctx._on_received_items({"index": 0, "items": [
            _network_item(beam) for _ in range(4)]})
        await _drive(ctx, fake, target=4)
        assert fake.received_pickups == 4
        assert fake.inventory_of("ITEM_WEAPON_WIDE_BEAM") == 1
        assert fake.inventory_of("ITEM_WEAPON_PLASMA_BEAM") == 1
        assert fake.inventory_of("ITEM_WEAPON_WAVE_BEAM") == 1
        # The grants landed one tier per delivery, in order...
        assert [g[0][0] for g in fake.onpickedup_calls[:3]] == [
            "ITEM_WEAPON_WIDE_BEAM",
            "ITEM_WEAPON_PLASMA_BEAM",
            "ITEM_WEAPON_WAVE_BEAM",
        ]
        # ...and the 4th delivery granted nothing (all tiers owned) but still
        # bumped the cursor — idempotent/saturating by construction.
        assert fake.onpickedup_calls[3] == []
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_progressive_beam_restart_does_not_re_advance():
    """A client restart against a game that already owns Wide+Plasma reads
    ReceivedPickups=2 and re-sends nothing — the cursor gate prevents a spurious
    Wave grant even though the same full progression would be re-sent."""
    ctx, dp, fake = await _setup()
    try:
        beam = _ap_id_for(dp, "Progressive Beam")
        await ctx._on_received_items({"index": 0, "items": [
            _network_item(beam), _network_item(beam)]})
        await _drive(ctx, fake, target=2)
        assert fake.inventory_of("ITEM_WEAPON_PLASMA_BEAM") == 1
        assert fake.inventory_of("ITEM_WEAPON_WAVE_BEAM") == 0

        # Re-deliver the same two items (as a fresh client would on reconnect).
        await ctx._on_received_items({"index": 0, "items": [
            _network_item(beam), _network_item(beam)]})
        for _ in range(3):
            await ctx._poll_once()
            await asyncio.sleep(0.02)
        assert fake.received_pickups == 2
        assert fake.inventory_of("ITEM_WEAPON_WAVE_BEAM") == 0  # no over-advance
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_release_burst_drains_fast_and_uses_burst_timings():
    """A release (burst of received items) drains via the post-grant pushes
    without one poll per item, and every item except the last carries the short
    burst popup/reschedule overrides so it isn't gated to ~7.5s each."""
    ctx, dp, fake = await _setup()
    try:
        # Prime the client's game-mode mirror to INGAME (a poll would normally
        # read this) so delivery isn't held by the menu gate. We do this once,
        # NOT per item — the point of this test is that the post-grant pushes
        # clock the backlog without a poll between each item.
        ctx.state.update_game_state(game_mode_id="INGAME")
        missile = _ap_id_for(dp, "Missile Tank")
        items = [_network_item(missile) for _ in range(5)]
        await ctx._on_received_items({"index": 0, "items": items})
        # _on_received_items kicks off delivery; the fake's post-grant pushes
        # then clock each subsequent item — no explicit per-item poll needed.
        assert await _await_until(lambda: fake.received_pickups >= 5, timeout=5.0)
        assert fake.received_pickups == 5
        assert len(fake.onpickedup_calls) == 5
        assert fake.inventory_of(MISSILE_ITEM) == 10

        # Only calls that actually granted matter for the timing assertion;
        # rejected (stale-index) retries are harmless no-ops. Group the granted
        # sources by received index to find the first accepted call per item.
        granted = {}
        for src in fake.receive_srcs:
            m = re.search(r',\s*(\d+)\s*,\s*\d+(?:\s*,[^)]*)?\)\s*$', src)
            if m is None:
                continue
            idx = int(m.group(1))
            granted.setdefault(idx, src)
        # Items 0..3 had more behind them → burst timings (3.0/3.5, rendered by
        # _lua_number as "3"/"3.5"); item 4 was last → lone-item default (5-arg).
        for idx in range(4):
            assert ", 3, 3.5)" in granted[idx], (idx, granted.get(idx))
        assert granted[4].rstrip().endswith(", 4, 4)"), granted[4]
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
async def test_delivery_held_until_ingame():
    """A pickup must not be sent while the player is on a menu (game mode !=
    INGAME). Delivering there would set a PendingPickup against transient
    pre-save state that can be orphaned across the menu→save transition,
    head-of-line-blocking the queue. The client holds delivery entirely on its
    side until a poll confirms INGAME, then delivers normally. This is the
    early-delivery guard for AP start_inventory, which streams at connect while
    the player may still be at the title/load screen."""
    ctx, dp, fake = await _setup()
    fake.game_mode = "MENU"
    try:
        missile = _ap_id_for(dp, "Missile Tank")
        await ctx._on_received_items({"index": 0, "items": [_network_item(missile)]})
        for _ in range(3):
            await ctx._poll_once()
            await asyncio.sleep(0.02)
        # Held client-side: no ReceivePickup reached the game.
        assert not fake.has_pending
        assert fake.received_pickups == 0
        assert fake.inventory_of(MISSILE_ITEM) == 0

        # Player loads their save → INGAME → delivery proceeds.
        fake.game_mode = "INGAME"
        await _drive(ctx, fake, target=1)
        assert fake.received_pickups == 1
        assert fake.inventory_of(MISSILE_ITEM) == 2
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


@pytest.mark.asyncio
async def test_received_pickups_revert_push_redelivers_off_read_loop():
    """A ``ReceivedPickups`` cursor REVERT pushed straight from the game (a
    save-reload that dropped the delivery cursor, NOT routed through ``/warp``)
    must re-deliver the dropped remote items — driven OFF the bridge read loop.

    ``_handle_received_pickups`` runs ON the read loop. The old code awaited
    ``_attempt_delivery`` there, which awaits a ``run_lua`` reply that ONLY the
    read loop can deliver → deadlock. This test pushes the revert directly (so
    the handler runs on the read loop) and asserts the dropped items are
    re-granted. Under the old code the read loop would be wedged inside that
    ``run_lua`` await and re-delivery would never complete, so the whole test is
    bounded by ``asyncio.wait_for`` — a regression deadlocks and fails fast
    rather than hanging the suite."""
    async def body():
        ctx, dp, fake = await _setup()
        try:
            missile = _ap_id_for(dp, "Missile Tank")
            items = [_network_item(missile) for _ in range(3)]
            await ctx._on_received_items({"index": 0, "items": items})
            await _drive(ctx, fake, target=3)
            assert fake.received_pickups == 3
            assert fake.inventory_of(MISSILE_ITEM) == 6
            # Sync the client's mirror of the game's two counters via one poll
            # (game_received_pickups + game_inventory_index) so the post-revert
            # ReceivePickup carries indices that match the fake's live counters.
            await ctx._poll_once()
            assert await _await_until(
                lambda: ctx.state.game_received_pickups() == 3)

            # The game reloads a save at received_pickups=1 and pushes the lower
            # cursor straight to us (no /warp; _warp_in_progress stays False).
            fake.received_pickups = 1
            fake.inventory_index = 1
            fake.inventory[MISSILE_ITEM] = 2
            fake.collected_pickup_indices.clear()
            # A real reload reverts BOTH counters; push the lower inventory index
            # first so the client's mirror matches the game (the post-revert
            # ReceivePickup must carry inv_idx == the game's live InventoryIndex
            # or the fake refuses the grant), then push the lower delivery cursor.
            await fake.push(W.Inventory(index=1, inventory=[]))
            await fake.push(W.ReceivedPickups(count=1))

            # (a) The read loop is NOT wedged. Push a marker log line AFTER the
            #     revert and assert the client processes it — the read loop only
            #     services this if _handle_received_pickups returned (it did NOT
            #     await a run_lua reply). Under the old code the read loop is
            #     blocked inside _attempt_delivery awaiting a reply only it can
            #     read, so this marker never arrives within the window.
            marker = "revert-liveness-probe-xyz"
            await fake.push(W.Log(level="info", msg=marker))
            assert await _await_until(
                lambda: marker in ctx.state.last_messages, timeout=3.0), (
                "read loop did not process a post-revert push — deadlocked?")

            # (b) The handler scheduled a tracked off-loop task rather than
            #     driving delivery on the read loop itself.
            assert ctx._revert_delivery_task is not None

            # (c) The dropped remote items are actually re-delivered: the off-loop
            #     task re-grants one (1->2) and the poll loop finishes the rest,
            #     exactly as the normal one-per-tick delivery path does.
            assert await _await_until(
                lambda: fake.received_pickups >= 2, timeout=3.0)
            await _drive(ctx, fake, target=3)
            assert fake.received_pickups == 3
            assert fake.inventory_of(MISSILE_ITEM) == 6
        finally:
            await _teardown(ctx, fake)

    # A deadlock here would hang indefinitely; bound it so a regression fails.
    await asyncio.wait_for(body(), timeout=20.0)


@pytest.mark.asyncio
async def test_received_pickups_revert_during_warp_is_not_double_driven():
    """A revert push observed while a ``/warp`` is in flight must NOT spawn a
    competing off-loop delivery — the warp path owns the restore. The
    ``_warp_in_progress`` guard in ``_schedule_revert_delivery`` skips it."""
    ctx, dp, fake = await _setup()
    try:
        ctx._warp_in_progress = True
        # Drive a revert push by hand; with the warp flag set, no task spawns.
        await ctx._handle_received_pickups(W.ReceivedPickups(count=0))
        assert ctx._revert_delivery_task is None
    finally:
        ctx._warp_in_progress = False
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_warp_restores_locally_collected_pickup():
    """Reproduces the missing-Energy-Tank bug. A local own-item pickup taken
    since the last save is granted by the game's seed-baked OnPickedUp and AP
    never re-delivers it. The /warp's Game.LoadScenario reloads from save, which
    reverts it — so the restore must re-grant it or it is lost for good."""
    ctx, dp, fake = await _setup()
    try:
        etank = "ITEM_ENERGY_TANKS"
        fake.save()                          # baseline save: no E-tank
        fake.collect(7, grants={etank: 1})   # local pickup, NOT saved
        assert fake.inventory_of(etank) == 1

        msg = await ctx._warp_to_start()
        assert "warped" in msg
        # Survived the warp (restored), not reverted to the save's 0.
        assert fake.inventory_of(etank) == 1
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_warp_then_recollect_does_not_duplicate():
    """After the warp restores the reverted item, returning to the pickup must
    NOT yield a second copy. The warp reverts the Location_Collected_* bit (so
    the actor would respawn); the restore re-asserts it, so the pickup stays
    'taken' and can't be re-collected."""
    ctx, dp, fake = await _setup()
    try:
        etank = "ITEM_ENERGY_TANKS"
        fake.save()
        fake.collect(7, grants={etank: 1})
        assert fake.inventory_of(etank) == 1

        msg = await ctx._warp_to_start()
        assert "warped" in msg
        assert fake.inventory_of(etank) == 1
        assert 7 in fake.collected_pickup_indices  # re-asserted, not reverted

        # Player walks back and touches the spot — the actor isn't there.
        fake.collect(7, grants={etank: 1})
        assert fake.inventory_of(etank) == 1        # still 1, not 2
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_warp_does_not_double_grant_remote_items():
    """A remote AP item delivered (but not yet saved) is reverted by the warp
    just like a local one. The restore re-grants exactly the lost amount and
    rewinds the ReceivedPickups cursor so a later poll does NOT also re-deliver
    it (which would double the item)."""
    ctx, dp, fake = await _setup()
    try:
        missile = _ap_id_for(dp, "Missile Tank")
        await ctx._on_received_items({"index": 0, "items": [_network_item(missile)]})
        await _drive(ctx, fake, target=1)
        assert fake.inventory_of(MISSILE_ITEM) == 2
        assert fake.received_pickups == 1

        # No save since delivery → warp reverts inventory + the cursor.
        msg = await ctx._warp_to_start()
        assert "warped" in msg
        assert fake.inventory_of(MISSILE_ITEM) == 2    # restored, not 4
        assert fake.received_pickups == 1              # cursor rewound

        # A subsequent poll must not re-deliver (cursor still matches).
        await ctx._poll_once()
        await asyncio.sleep(0.05)
        assert fake.inventory_of(MISSILE_ITEM) == 2
        assert fake.received_pickups == 1
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_warp_blocked_outside_ingame_does_not_touch_inventory():
    """A /warp from a menu is refused in-Lua (not_ingame) before LoadScenario,
    so no revert and no restore happen."""
    ctx, dp, fake = await _setup()
    try:
        fake.game_mode = "MENU"
        fake.collect(3, grants={"ITEM_ENERGY_TANKS": 1})
        msg = await ctx._warp_to_start()
        assert "blocked" in msg
        assert fake.inventory_of("ITEM_ENERGY_TANKS") == 1
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_warp_blocked_in_boss_arena_does_not_revert():
    """The Kraid-brick case: a /warp issued from inside a boss arena is refused
    in-Lua (in_boss) before LoadScenario, so the save is never reloaded and
    nothing reverts. The player stays in the fight rather than corrupting it."""
    ctx, dp, fake = await _setup()
    try:
        etank = "ITEM_ENERGY_TANKS"
        fake.save()
        fake.collect(3, grants={etank: 1})   # local pickup since save
        fake.in_boss_arena = True
        msg = await ctx._warp_to_start()
        assert "blocked" in msg
        assert "boss arena" in msg
        # No LoadScenario fired → inventory untouched (not reverted to save's 0).
        assert fake.inventory_of(etank) == 1
        assert 3 in fake.collected_pickup_indices
    finally:
        await _teardown(ctx, fake)


def _bounces(ctx: DreadContext) -> list[dict]:
    return [m for m in _all_sent(ctx) if m.get("cmd") == "Bounce"]


@pytest.mark.asyncio
async def test_deathlink_outbound_on_player_death():
    """With DeathLink on, the first poll baselines the death count and a
    subsequent in-world death broadcasts a single Bounce to AP."""
    ctx, dp, fake = await _setup()
    try:
        await ctx.update_death_link(True)
        assert "DeathLink" in ctx.tags

        await ctx._poll_once()  # baseline (death_count == 0)
        assert _bounces(ctx) == [], "baseline poll should not broadcast"

        fake.die()
        await ctx._poll_once()
        bounces = _bounces(ctx)
        assert len(bounces) == 1
        assert "DeathLink" in bounces[0]["tags"]

        # No new death ⇒ no further broadcast (edge-triggered, not level).
        await ctx._poll_once()
        assert len(_bounces(ctx)) == 1
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_deathlink_inbound_kills_player_and_does_not_echo():
    """An incoming DeathLink force-kills Samus; the resulting in-game death is
    swallowed once so the chain terminates instead of echoing. A later natural
    death still broadcasts."""
    ctx, dp, fake = await _setup()
    try:
        await ctx.update_death_link(True)
        await ctx._poll_once()  # baseline

        # Another player's death arrives (distinct time ⇒ not our own).
        ctx.on_deathlink({"time": 123.0, "source": "Player2", "cause": "boom"})
        assert await _await_until(lambda: fake.death_count == 1), (
            "kill Lua never reached the Switch")

        # The self-induced death must NOT be re-broadcast.
        await ctx._poll_once()
        assert _bounces(ctx) == [], "self-induced death echoed back to AP"

        # A genuine subsequent death IS broadcast.
        fake.die()
        await ctx._poll_once()
        assert len(_bounces(ctx)) == 1
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_deathlink_suppress_window_expires():
    """An armed-but-unconsumed suppression window (e.g. the kill no-opped at the
    main menu) must self-expire so a later real death is still broadcast."""
    ctx, dp, fake = await _setup()
    try:
        await ctx.update_death_link(True)
        await ctx._poll_once()  # baseline

        # Simulate a stale window from a kill that never produced a death.
        ctx._suppress_death_until = time.monotonic() - 1.0

        fake.die()
        await ctx._poll_once()
        assert len(_bounces(ctx)) == 1, "expired window wrongly muted a real death"
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_deathlink_off_does_not_poll_or_broadcast():
    """With the tag off, a death is neither detected nor broadcast."""
    ctx, dp, fake = await _setup()
    try:
        assert "DeathLink" not in ctx.tags
        await ctx._poll_once()
        fake.die()
        await ctx._poll_once()
        assert _bounces(ctx) == []
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_lights_out_enabled_reaches_switch():
    """With ``lights_out`` set in slot_data, the client pushes the activation to
    a bootstrapped Switch so the in-game map-hide hook turns on."""
    ctx, dp, fake = await _setup()
    try:
        # Mirror _on_connected populating slot_data, then re-apply (the bootstrap
        # already ran with empty slot_data, so this is the delivering call).
        ctx.slot_data = {"lights_out": True}
        await ctx._apply_lights_out()
        assert await _await_until(lambda: fake.lights_out_enabled), (
            "Lights Out activation never reached the Switch")
    finally:
        await _teardown(ctx, fake)


@pytest.mark.asyncio
async def test_lights_out_off_does_not_activate():
    """With Lights Out off (default slot_data), the client must NOT send the
    activation — the map stays visible and existing seeds are unaffected."""
    ctx, dp, fake = await _setup()
    try:
        assert not ctx.slot_data.get("lights_out")
        await ctx._apply_lights_out()
        # Give any erroneous send a chance to land before asserting absence.
        await asyncio.sleep(0.05)
        assert fake.lights_out_enabled is False
    finally:
        await _teardown(ctx, fake)
