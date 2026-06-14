"""DreadContext — CommonContext subclass owning AP + Switch bridge.

Lifecycle inversion (vs the old code): the Switch is now the TCP dialer.
We host a TCP server on ``0.0.0.0:17777`` + a UDP discovery responder on
``0.0.0.0:17776``. Bridge fires :meth:`_on_switch_ready` when a Switch
HELLOs and becomes active; that callback sends the ``RL.*`` bootstrap
and starts the poll loop. On Switch disconnect the bridge either auto-
promotes an inactive Switch (re-firing :meth:`_on_switch_ready`) or
fires :meth:`_on_switch_gone` to stop the poll loop.

The :meth:`_attempt_delivery` semantics are unchanged — we still compose
``RL.ReceivePickup`` Lua and ship it via ``bridge.run_lua``; the only
difference is the wire format on the way.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from CommonClient import CommonContext, ClientCommandProcessor
from NetUtils import ClientStatus

from .commands import parse_command
from .datapackage import DataPackage
from .bridge_server import (
    BridgeServer, ActiveConnInfo, DEFAULT_PORT as BRIDGE_DEFAULT_PORT,
    POLL_LUA_TIMEOUT,
)
from .discovery import DiscoveryResponder, DEFAULT_DISCOVERY_PORT
from . import wire as W
from .protocol import (
    DreadItem,
    ReceivedItemEvent,
    CollectedLocationEvent,
    build_receive_pickup_lua,
    build_kill_player_lua,
    build_lights_out_lua,
    build_read_death_count_lua,
    build_read_inventory_amounts_lua,
    build_read_received_pickups_lua,
    build_read_collected_indices_lua,
    build_mark_collected_lua,
    build_restore_grant_lua,
    build_set_received_pickups_lua,
    pickup_class_for,
    pickup_resource_stage,
)
from .scout_cache import ScoutCache, request_scout
from .state import BridgeState
from .._setup import setup_state_path
from .._setup.deploy import DREAD_TITLE_ID, RYU_MOD_NAME

log = logging.getLogger(__name__)

_CLIENT_LOGGER = __name__.rpartition(".")[0]
_switch_log = logging.getLogger(f"{_CLIENT_LOGGER}.switch")
_ap_log = logging.getLogger("Client")


GAME_NAME = "Metroid Dread"


def _expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))


def _settings_dreadvania_python() -> Optional[str]:
    """Read ``dreadvania_python`` from host.yaml via AP's settings framework.

    Returns the configured path string, or ``None`` if unset or the settings
    module is unavailable (e.g. unit-test isolation without a full AP install).
    """
    try:
        import settings as ap_settings
        val = str(ap_settings.get_settings()["dread_options"]["dreadvania_python"])
        return val if val else None
    except Exception:
        return None


def _field(obj: Any, name: str, idx: int) -> Any:
    """Pluck a field from a NetworkItem-like that may be NamedTuple, dict,
    or plain (positionally-ordered) tuple/list."""
    if hasattr(obj, name):
        return getattr(obj, name)
    if isinstance(obj, dict):
        return obj[name]
    return obj[idx]


# Polling cadence for the periodic Switch-state pull. 2.0s matches
# Randovania's RL.UpdateRDVClient self-scheduling interval.
POLL_INTERVAL_SECONDS = 2.0

# /warp inventory-rewind timings. Game.LoadScenario drops out of INGAME during
# the reload; we wait for it to come back, let the loaded save settle, then diff
# inventory to re-grant whatever the reload reverted. See _restore_after_warp.
WARP_RESTORE_TIMEOUT_SECONDS = 20.0
WARP_RESTORE_POLL_SECONDS = 0.5
WARP_RESTORE_SETTLE_SECONDS = 0.5

# After an incoming DeathLink, the death WE cause must not be re-broadcast. We
# arm a suppression window rather than a sticky flag: the first detected death
# within the window is swallowed (and the window cleared); if no death lands —
# e.g. the kill no-ops because the player was at the main menu — the window
# expires and normal detection resumes, so a later real death isn't lost. The
# window must comfortably exceed the worst-case cutscene-deferral of the kill.
DEATH_SUPPRESS_WINDOW_SECONDS = 15.0

# When more than one received item is waiting (an AP "release" sends a burst),
# we override the bootstrap's lone-item popup/reschedule timings so the backlog
# drains faster than one item every ~7.5s. The grant itself is immediate;
# BURST_RESCHEDULE_SECONDS only gates how soon the next item is accepted, and
# BURST_POPUP_SECONDS is the popup display time. We keep popup < reschedule (same
# 0.5s relationship as the lone-item 7.0/7.5 default) so popups never pile up: a
# popup finishes before the next item lands. 3.0/3.5 roughly halves the per-item
# time during a release while keeping each notification readable. The last item
# of a burst falls back to the lone-item default (7.0/7.5) so the player gets a
# normal, lingering popup once the flood ends.
BURST_POPUP_SECONDS = 3.0
BURST_RESCHEDULE_SECONDS = 3.5


class DreadClientCommandProcessor(ClientCommandProcessor):
    """`/`-prefixed commands typed into the Kivy command bar."""

    def _emit(self, result) -> None:
        if result.error:
            self.output(f"err: {result.error}")
        if result.info:
            for line in result.info.splitlines():
                self.output(line)

    def _cmd_dread_status(self) -> bool:
        """Show client-side state mirror."""
        ctx = self.ctx
        result = parse_command("status", state=ctx.state)
        self._emit(result)
        return True

    def _cmd_bridge_status(self) -> bool:
        """``/bridge_status`` — show the Bridge listener + connected Switches."""
        ctx = self.ctx
        bridge = ctx._bridge
        if bridge is None:
            self.output("bridge: not started")
            return True
        self.output(f"bridge: listening on TCP {bridge.host}:{bridge.port}")
        self.output(f"discovery: UDP 0.0.0.0:{DEFAULT_DISCOVERY_PORT}")
        switches = bridge.list_switches()
        if not switches:
            self.output("no Switches connected (waiting for HELLO)")
            return True
        for s in switches:
            tag = "ACTIVE" if s["active"] else "inactive"
            self.output(
                f"  {tag} {s['device_id']} (peer={s['peer_ip']} "
                f"mod_ver={s['mod_ver']!r} dread={s['dread_ver']!r})"
            )
        return True

    def _cmd_switches(self) -> bool:
        """Alias of /bridge_status."""
        return self._cmd_bridge_status()

    def _cmd_promote_switch(self, device_id: str = "") -> bool:
        """``/promote_switch <device_id>`` — make this Switch the active one."""
        ctx = self.ctx
        bridge = ctx._bridge
        if bridge is None:
            self.output("err: bridge not started")
            return True
        if not device_id:
            self.output("usage: /promote_switch <device_id>")
            return True
        async def _go():
            ok = await bridge.manual_promote(device_id)
            self.output(f"promote {device_id}: {'OK' if ok else 'unknown device_id'}")
        asyncio.ensure_future(_go())
        return True

    def _cmd_poke(self, *lua_words: str) -> bool:
        """``/poke <lua-source>`` — run arbitrary Lua on the active Switch."""
        if not lua_words:
            self.output("usage: /poke <lua-source>")
            return True
        source = " ".join(lua_words)
        asyncio.ensure_future(self.ctx._poke_lua(source))
        return True

    def _cmd_warp(self) -> bool:
        """``/warp`` — softlock recovery: warp to the starting room.

        Wraps the same ``Game.LoadScenario`` call Randovania exposes via
        ZL+ZR at any save station (``Scenario.CheckWarpToStart``), but
        invokable when no save station is reachable. Inventory and
        per-pickup collected bits persist (same primitive every door
        transition uses); the player should save at the starting save
        station to commit them to disk.
        """
        ctx = self.ctx
        async def _go():
            msg = await ctx._warp_to_start()
            self.output(f"warp: {msg}")
        asyncio.ensure_future(_go())
        return True

    def _cmd_setup(self, *_args: str) -> bool:
        """``/setup`` — open the Kivy setup wizard in a new window."""
        from worlds.LauncherComponents import launch_subprocess
        from .. import _run_setup_wizard_no_dreadap
        launch_subprocess(_run_setup_wizard_no_dreadap, name="DreadSetup")
        self.output(
            "Launched setup wizard in a new window. DreadClient stays "
            "open; the wizard walks you through prereqs, vanilla romfs "
            "picker, exlaunch build, and deploy target. Close it any "
            "time — DreadClient is unaffected."
        )
        return True


class DreadContext(CommonContext):
    """Top-level glue. Connects to AP server (inherited) and to the Switch
    (via :class:`BridgeServer`). Forwards AP items to Lua, AP server
    receives collected-checks from the periodic poll."""

    command_processor = DreadClientCommandProcessor
    game = GAME_NAME
    items_handling = 0b001

    def __init__(
        self,
        server_address: Optional[str],
        password: Optional[str],
        *,
        state: BridgeState,
        datapackage: DataPackage,
        bridge_port: int = BRIDGE_DEFAULT_PORT,
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
        expected_mod_ver: str = "",
    ):
        super().__init__(server_address, password)
        self.state = state
        self.datapackage = datapackage
        self.scout_cache = ScoutCache()
        self.bridge_port = bridge_port
        self.discovery_port = discovery_port
        self._expected_mod_ver = expected_mod_ver

        # Bridge + discovery responder created at start_bridge() time.
        self._bridge: Optional[BridgeServer] = None
        self._discovery: Optional[DiscoveryResponder] = None

        # Per-active-Switch session state. Reset on each promote/demote.
        self._poll_task: Optional[asyncio.Task[None]] = None
        # A push handler runs on the bridge read loop and so must never await a
        # run_lua reply (only the read loop can deliver it — that deadlocks).
        # When a ReceivedPickups push reports a cursor REVERT (e.g. a save-reload
        # / warp dropped it), re-delivery is scheduled here and runs OFF the read
        # loop. Tracked + non-overlapping so a burst of reverts can't spawn
        # unbounded tasks; cancelled with the session. See _handle_received_pickups.
        self._revert_delivery_task: Optional[asyncio.Task[None]] = None
        self._bootstrapped: bool = False
        self._active_info: Optional[ActiveConnInfo] = None

        self._goal_reported = False
        self._ap_items: list[Any] = []
        # The AP seed_name we last reconciled the collected-location mirror
        # against. The Switch reports collected pickups by positional
        # pickup_index, which is seed-INDEPENDENT; if the multiworld is
        # regenerated (new seed) while this process keeps running, a stale
        # mirror from the old seed would dedupe-suppress the new seed's
        # same-positioned checks. Tracked so _on_connected can detect the change
        # and drop the mirror. "" until the first Connected.
        self._synced_seed: str = ""
        # Delivery diagnostics: the received index we last attempted to deliver
        # and how many polls we've re-sent it without the game's ReceivedPickups
        # advancing past it (a head-of-line stall — RL.ReceivePickup silently
        # no-ops on an index mismatch or a stuck PendingPickup).
        self._delivery_index: int = -1
        self._delivery_attempts: int = 0
        # Set for the duration of a /warp's inventory-rewind window. While true,
        # _attempt_delivery is suppressed so a remote item isn't granted in the
        # gap between the LoadScenario revert and the restore diff (which would
        # double-count it). See _warp_to_start / _restore_after_warp.
        self._warp_in_progress: bool = False
        self.slot_data: dict = {}
        # AP item name → per-pickup grant amount, from slot_data's `item_amounts`
        # (the seed's Randovania-style `ammo_count` knobs). Overrides the static
        # items.json quantity on the wire-delivery path so a remotely-delivered
        # copy grants the seed's configured amount. Empty ⇒ fall back to
        # items.json (older seeds / offline).
        self._item_amounts: dict[str, int] = {}

        # DeathLink. ``_last_death_count`` is the last value we read from the
        # game's ProgressStat_PlayerDeaths prop; None means "no baseline yet"
        # (don't report on the first read after connect). ``_suppress_death_until``
        # is a monotonic deadline: a death detected before it is one WE caused in
        # response to an incoming DeathLink, so it is not re-broadcast — that
        # terminates the chain instead of echoing it around the room. The window
        # self-expires so a stuck guard can't permanently mute real deaths.
        # See [[dread-deathlink-apis]].
        self._last_death_count: Optional[int] = None
        self._suppress_death_until: float = 0.0
        self.dreadvania_python: Optional[str] = _settings_dreadvania_python()
        self.patcher_python_status: str = ""

    # ---- CommonContext overrides --------------------------------------

    async def server_auth(self, password_requested: bool = False) -> None:
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    def on_package(self, cmd: str, args: dict) -> None:
        if cmd == "Connected":
            asyncio.ensure_future(self._on_connected(args))
        elif cmd == "ReceivedItems":
            asyncio.ensure_future(self._on_received_items(args))
        elif cmd == "LocationInfo":
            n = self.scout_cache.absorb_location_info(args)
            log.debug("absorbed %d scout entries", n)
        elif cmd == "RoomInfo":
            self.state.seed = args.get("seed_name", "")

    async def shutdown(self) -> None:
        await self._stop_active_session()
        if self._discovery is not None:
            self._discovery.stop()
            self._discovery = None
        if self._bridge is not None:
            await self._bridge.close()
            self._bridge = None
        await super().shutdown()

    def run_gui(self) -> None:
        """Lazy-import + start the Kivy UI."""
        from .gui import DreadManager
        self.ui = DreadManager(self)
        self.ui_task = asyncio.create_task(self.ui.async_run(), name="DreadUI")

    # ---- Bridge lifecycle ---------------------------------------------

    async def start_bridge(self) -> None:
        """Bind the BridgeServer + DiscoveryResponder. Idempotent.

        Raises ``OSError`` if either port is in use — the caller (main.py)
        should surface this loudly per the plan. Callers should invoke
        this once at process start; the bridge persists across AP
        connect/disconnect cycles.
        """
        if self._bridge is None:
            self._bridge = BridgeServer(
                slot=self.username or "",
                seed=self.state.seed or "",
                expected_mod_ver=self._expected_mod_ver,
                port=self.bridge_port,
                on_push=self._on_switch_push,
                on_active_connected=self._on_switch_ready,
                on_active_disconnected=self._on_switch_gone,
            )
            await self._bridge.start()
            self.state.set_switch_conn("listening")
        if self._discovery is None:
            self._discovery = DiscoveryResponder(
                tcp_port=self.bridge_port,
                get_seed=lambda: self.state.seed or "",
                port=self.discovery_port,
            )
            await self._discovery.start()

    async def _on_switch_ready(self, info: ActiveConnInfo) -> None:
        """Called by BridgeServer once a Switch HELLOs + becomes active.

        Sends the ``RL.*`` bootstrap and starts polling. Mirrors the old
        ``connect_switch`` flow minus the dial-out half.
        """
        await self._stop_active_session()
        self._active_info = info
        self._bootstrapped = False
        self.state.set_switch_conn(f"connected ({info.device_id})")
        self.state.update_game_state(layout_uuid=info.layout_uuid)
        log.info("Switch %s connected (peer=%s, mod_ver=%s, dread=%s)",
                 info.device_id, info.peer_ip, info.mod_ver, info.dread_ver)
        try:
            await self._send_bootstrap()
        except Exception as exc:
            log.exception("bootstrap failed for %s: %s", info.device_id, exc)
            self.state.set_switch_conn(f"bootstrap error: {exc}")
            self._active_info = None
            return
        self._bootstrapped = True
        log.info("Switch %s bootstrapped; starting poll loop", info.device_id)
        self._poll_task = asyncio.create_task(self._poll_loop(), name="dread-poll")
        # Re-assert Lights Out for this freshly-bootstrapped Switch. Covers the
        # AP-connected-before-Switch ordering; _on_connected covers the reverse.
        await self._apply_lights_out()
        # If AP items were already received before bootstrap finished, drive
        # delivery now so we catch up without waiting one full poll.
        await self._attempt_delivery()

    async def _on_switch_gone(self) -> None:
        """BridgeServer fires this when the active Switch disconnects and
        no inactive is available for auto-promote."""
        log.info("active Switch gone; stopping poll loop")
        await self._stop_active_session()
        self.state.set_switch_conn("listening")
        self._active_info = None

    async def _stop_active_session(self) -> None:
        """Cancel the poll loop and clear per-session state. Idempotent."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        if self._revert_delivery_task is not None:
            self._revert_delivery_task.cancel()
            try:
                await self._revert_delivery_task
            except (asyncio.CancelledError, Exception):
                pass
            self._revert_delivery_task = None
        self._bootstrapped = False

    async def _send_bootstrap(self) -> None:
        """Send the vendored ``RL.*`` bootstrap, chunked to the negotiated
        buffer size. Same logic as before — only the transport changed.

        The Switch's buffer_size used to be read from the API version probe;
        in the new wire it's implicit (Switch tells us via HELLO's mod_ver,
        but our bootstrap chunks are conservatively sized for any plausible
        Switch). We assume a safe 4096-byte chunk size to leave room for the
        JSON envelope inside the 8 KiB line cap.
        """
        assert self._bridge is not None
        from .bootstrap import load_bootstrap_code, chunk_lua_blocks
        BUFFER_SIZE = 4096
        blocks = load_bootstrap_code()
        chunks = chunk_lua_blocks(blocks, BUFFER_SIZE)
        log.info("Sending RL bootstrap: %d blocks in %d chunk(s)",
                 len(blocks), len(chunks))
        for i, chunk in enumerate(chunks):
            resp = await self._bridge.run_lua(chunk)
            if not resp.success:
                raise RuntimeError(
                    f"bootstrap chunk {i + 1}/{len(chunks)} failed: "
                    f"{resp.payload.decode('utf-8', 'replace')[:200]}"
                )

    # ---- AP-driven flows ---------------------------------------------

    async def _on_connected(self, args: dict) -> None:
        self.state.set_ap_conn("connected")
        self.state.slot = self.username or ""
        # seed_name rides on RoomInfo (already absorbed into state.seed by
        # on_package) and not on Connected, so fall back to the stored value.
        self.state.seed = args.get("seed_name", self.state.seed)
        new_seed = self.state.seed or ""
        self._goal_reported = False
        self._ap_items = []
        # Reconcile the collected-location mirror against this connection's seed.
        if new_seed and new_seed != self._synced_seed:
            # A different multiworld generation (or the first connect). The
            # Switch keys collected pickups by positional pickup_index, which is
            # seed-INDEPENDENT, so any location left in the mirror from a prior
            # seed would dedupe-suppress the SAME-positioned (but now
            # different-item) location here — silently dropping this seed's
            # outgoing check. Drop the mirror; the next bitfield push re-derives
            # this seed's collected set from scratch and forwards each anew.
            if self._synced_seed:
                log.info("AP seed changed (%r -> %r); resetting collected-"
                         "location mirror so prior-seed collections don't "
                         "suppress this seed's checks", self._synced_seed, new_seed)
            self.state.clear_received()
            self._synced_seed = new_seed
        else:
            # Same multiworld, fresh socket: re-assert everything we believe is
            # collected. A LocationCheck emitted while the socket was down (a
            # pickup grabbed during an AP disconnect) is otherwise lost for good
            # — the dedupe cache suppresses the re-forward on the next bitfield
            # push. AP ignores already-checked locations, so this is a safe
            # idempotent catch-up.
            collected = sorted(self.state.all_collected_ids())
            if collected:
                log.info("re-syncing %d collected location(s) after reconnect",
                         len(collected))
                await self.send_msgs([{"cmd": "LocationChecks",
                                       "locations": collected}])
        # New connection ⇒ re-baseline death detection on the next poll so a
        # historical death count from a prior session isn't reported now.
        self._last_death_count = None
        self._suppress_death_until = 0.0
        sd = args.get("slot_data")
        if isinstance(sd, dict):
            self.slot_data = sd
            self._item_amounts = {
                str(name): int(qty)
                for name, qty in (sd.get("item_amounts") or {}).items()
            }
        # Mirror the seed's DeathLink choice into the AP connection tag. Sends a
        # ConnectUpdate (we're already Connected here), which is the supported
        # way to toggle the tag post-connect.
        await self.update_death_link(bool(self.slot_data.get("death_link")))
        # Push "Lights Out" to the Switch now that slot_data is known. Covers the
        # Switch-connected-before-AP ordering; _on_switch_ready covers the
        # reverse. No-op if the Switch isn't bootstrapped yet (the later
        # _on_switch_ready call delivers it then).
        await self._apply_lights_out()
        asyncio.ensure_future(self._maybe_auto_patch())
        loc_ids = self.datapackage.all_location_ids()
        if loc_ids:
            await request_scout(self, loc_ids, cache=self.scout_cache)
        # Bridge stays up across AP reconnects — no per-connection action.

    async def _on_received_items(self, args: dict) -> None:
        """Absorb a ``ReceivedItems`` package into the ordered AP-items list,
        then attempt delivery."""
        index = int(args.get("index", 0))
        items = args.get("items") or []
        end = index + len(items)
        if len(self._ap_items) < end:
            self._ap_items.extend([None] * (end - len(self._ap_items)))
        for offset, network_item in enumerate(items):
            self._ap_items[index + offset] = network_item
        await self._attempt_delivery()

    async def _attempt_delivery(self) -> None:
        """Send the one pickup the game is next expecting, if any.

        Semantics unchanged — only the transport call differs (``self._bridge.run_lua``
        in place of ``self.executor.run_lua``)."""
        if self._bridge is None or not self._bridge.is_connected() or not self._bootstrapped:
            return
        if self._warp_in_progress:
            # A /warp is rewinding the post-LoadScenario inventory revert.
            # Suppress delivery until it finishes so we don't grant a remote
            # item the restore diff would then double-count.
            return
        # Only deliver while the player is in the game world. We require a
        # confirmed 'INGAME' (the same gate the bootstrap's RL.UpdateRDVClient
        # uses) rather than merely "not a known menu" — game_mode is empty until
        # the first poll reads it, and at connect (the moment AP streams
        # start_inventory) we must NOT fire a pickup at the title/load menu.
        # Holding self-corrects within one poll: _poll_once reads the mode and
        # then calls us. A pickup delivered on a menu sets a PendingPickup
        # against transient pre-save state that can be orphaned across the
        # menu→save transition, head-of-line-blocking the whole queue.
        mode = self.state.game_mode()
        if mode != "INGAME":
            log.debug("delivery held: game mode is %r, not INGAME", mode or "<unknown>")
            return
        received = self.state.game_received_pickups()
        target = len(self._ap_items)
        if received >= target:
            return
        network_item = self._ap_items[received]
        if network_item is None:
            return
        dread_item, sender = self._resolve_item(network_item)
        if dread_item is None:
            log.error("no Dread mapping for AP item id %s at received index %d; "
                      "delivery stalled", _field(network_item, "item", 0), received)
            return
        message = f"Received {dread_item.ap_item_name} from {sender}"
        if dread_item.progression_stages is not None:
            # Progressive item: send the FULL multi-stage progression with the
            # first tier's class. The game's OnPickedUp grants the next missing
            # tier, so the same progression is sent every time (no client-side
            # tier counting; identical to the seed-baked local pickup).
            progression = [list(stage) for stage in dread_item.progression_stages]
            cls = dread_item.pickup_cls or "RandomizerPowerup"
        else:
            # Per-pickup grant amount: the seed's `item_amounts` (Randovania
            # `ammo_count` knobs) override the static items.json quantity, so a
            # wire-delivered copy grants the same amount as the seed-baked path.
            qty = self._item_amounts.get(dread_item.ap_item_name, dread_item.quantity)
            progression = [pickup_resource_stage(dread_item.patcher_item_id, qty)]
            cls = pickup_class_for(dread_item.patcher_item_id)
        inv_idx = self.state.game_inventory_index()
        # Surface each delivery attempt. The game grants only when the sent
        # received/inventory indices match its live counters, then silently
        # advances ReceivedPickups; if it never advances, we re-send the same
        # index every poll — a head-of-line stall that blocks every later item.
        if received != self._delivery_index:
            self._delivery_index = received
            self._delivery_attempts = 1
            log.info("delivering #%d %s (cls=%s, inv_idx=%d)", received,
                     dread_item.ap_item_name, cls, inv_idx)
        else:
            self._delivery_attempts += 1
            # Thresholds in poll ticks (~POLL_INTERVAL_SECONDS each). A pending
            # ReceivePickup is deferred by design through cutscenes / scene loads
            # (Scenario.IsUserInteractionEnabled), and those routinely exceed a
            # few seconds — so the first warning sits at ~20s, well past any
            # normal deferral. A genuine head-of-line stall is permanent, so
            # waiting longer to cry wolf costs nothing.
            if self._delivery_attempts in (10, 30, 60):
                log.warning(
                    "delivery of #%d %s has not landed after %d attempts "
                    "(game ReceivedPickups stuck at %d, inv_idx=%d) — "
                    "later items are blocked behind it",
                    received, dread_item.ap_item_name, self._delivery_attempts,
                    received, inv_idx)
        # If more items are already queued behind this one (a release), drain the
        # backlog fast; the final item falls back to the bootstrap's lone-item
        # defaults so its popup lingers normally. (target - received) is the
        # number still pending, including this one.
        if target - received > 1:
            popup_seconds = BURST_POPUP_SECONDS
            reschedule_seconds = BURST_RESCHEDULE_SECONDS
        else:
            popup_seconds = None
            reschedule_seconds = None
        lua = build_receive_pickup_lua(
            message=message,
            progression=progression,
            received_pickup_index=received,
            inventory_index=inv_idx,
            cls=cls,
            popup_seconds=popup_seconds,
            reschedule_seconds=reschedule_seconds,
        )
        try:
            await self._bridge.run_lua(lua)
        except (ConnectionError, asyncio.TimeoutError, RuntimeError) as exc:
            log.warning("ReceivePickup send failed for %s: %s; will retry",
                        dread_item.ap_item_name, exc)

    def _sender_name(self, slot_idx: int) -> str:
        if self.slot_info and slot_idx in self.slot_info:
            return self.slot_info[slot_idx].name
        if slot_idx == self.slot:
            return "yourself"
        return f"Player {slot_idx}"

    # ---- DeathLink ----------------------------------------------------

    def on_deathlink(self, data: dict) -> None:
        """An external player died (CommonContext dispatches this from the AP
        message task; our own deaths are filtered upstream by the last_death_link
        timestamp guard). Force-kill Samus on the Switch and mark the resulting
        in-game death as self-induced so we don't re-broadcast it."""
        super().on_deathlink(data)  # updates last_death_link + logs the cause
        self._suppress_death_until = time.monotonic() + DEATH_SUPPRESS_WINDOW_SECONDS
        asyncio.ensure_future(self._kill_switch_player())

    async def _kill_switch_player(self) -> None:
        """Send the kill Lua to the active Switch. Safe to call any time —
        ``RL.KillPlayer`` no-ops outside INGAME and defers through cutscenes."""
        if self._bridge is None or not self._bridge.is_connected() or not self._bootstrapped:
            log.info("DeathLink received but no Switch ready; ignoring kill")
            self._suppress_death_until = 0.0
            return
        try:
            await self._bridge.run_lua(build_kill_player_lua())
        except (ConnectionError, asyncio.TimeoutError, RuntimeError) as exc:
            log.warning("DeathLink kill send failed: %s", exc)
            # Couldn't kill ⇒ no self-death will follow, so clear the guard.
            self._suppress_death_until = 0.0

    # ---- Lights Out ---------------------------------------------------

    async def _apply_lights_out(self) -> None:
        """Enable "Lights Out" race mode on the Switch when the seed asks for it.

        Sends ``RL.LightsOut=true`` + an immediate apply (see
        ``build_lights_out_lua``). Idempotent and safe to call repeatedly: it is
        invoked from BOTH ``_on_connected`` (AP slot_data arrives) and
        ``_on_switch_ready`` (Switch bootstraps), so whichever happens second
        actually delivers it regardless of connection order. A no-op when the
        seed leaves Lights Out off, or before the Switch is bootstrapped.

        Called from the AP-message / connect tasks, never from a Switch push
        handler, so awaiting the run_lua reply is safe (the read loop that
        delivers it is separate — see the no-run_lua-in-push-handler rule)."""
        if not self.slot_data.get("lights_out"):
            return
        if self._bridge is None or not self._bridge.is_connected() or not self._bootstrapped:
            return
        try:
            await self._bridge.run_lua(build_lights_out_lua())
            log.info("Lights Out enabled on Switch")
        except (ConnectionError, asyncio.TimeoutError, RuntimeError) as exc:
            log.warning("Lights Out activation send failed: %s", exc)

    async def _maybe_report_death(self, count: int) -> None:
        """Edge-detect the game's death counter and broadcast to AP.

        ``count`` is the freshly-read ProgressStat_PlayerDeaths value. The first
        read after connect only sets the baseline. A death that we caused via an
        incoming DeathLink is swallowed once (``_suppress_next_death``) so the
        chain terminates instead of echoing."""
        prev = self._last_death_count
        self._last_death_count = count
        if prev is None or count <= prev:
            return  # baseline, or no new death
        if time.monotonic() < self._suppress_death_until:
            self._suppress_death_until = 0.0  # consume the window
            log.info("suppressing self-induced death (incoming DeathLink)")
            return
        if "DeathLink" not in self.tags:
            return
        log.info("player died (death count %d -> %d); broadcasting DeathLink",
                 prev, count)
        who = self._sender_name(self.slot) if self.slot else (self.username or "Samus")
        await self.send_death(f"{who} was killed by the planet ZDR.")

    # ---- /setup auto-run hookup --------------------------------------

    def _dreadvania_install_dir_from_state(self, state: dict) -> Optional[Path]:
        target = state.get("deploy_target")
        if target == "ryujinx":
            root = state.get("ryujinx_root") or ""
            return (Path(root) / "mods" / "contents" / DREAD_TITLE_ID
                    / RYU_MOD_NAME) if root else None
        if target == "sd":
            root = state.get("sd_root") or ""
            return (Path(root) / "atmosphere" / "contents"
                    / DREAD_TITLE_ID) if root else None
        if target == "custom":
            root = state.get("custom_root") or ""
            return (Path(root) / "atmosphere" / "contents"
                    / DREAD_TITLE_ID) if root else None
        return None

    async def _maybe_auto_patch(self) -> None:
        state_path = setup_state_path()
        if not state_path.is_file():
            _ap_log.info(
                "Auto-patch skipped: %s not found. Run /setup once per "
                "machine to record your romfs + deploy paths; subsequent "
                "seeds patch automatically on connect.",
                state_path,
            )
            return
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _ap_log.warning(
                "Auto-patch skipped: %s unreadable (%s). Re-run /setup to "
                "refresh the state file.",
                state_path, exc,
            )
            return

        romfs_raw = state.get("romfs_path")
        if not romfs_raw or not isinstance(romfs_raw, str):
            _ap_log.info(
                "Auto-patch skipped: romfs_path not recorded in %s. Re-run "
                "/setup to pick your extracted Dread romfs folder.",
                state_path,
            )
            return
        romfs_path = Path(romfs_raw)
        if not romfs_path.is_dir():
            _ap_log.warning(
                "Auto-patch skipped: romfs path %s no longer exists. "
                "Re-run /setup and re-pick your extracted romfs folder.",
                romfs_path,
            )
            return

        deploy_dir = self._dreadvania_install_dir_from_state(state)
        if deploy_dir is None:
            _ap_log.info(
                "Auto-patch skipped: deploy target not recorded in %s. "
                "Re-run /setup to pick Ryujinx / SD / Custom folder.",
                state_path,
            )
            return
        if state.get("deploy_target") == "sd":
            # Gate on the SD's `atmosphere` dir (created by the Atmosphere CFW
            # install), NOT the per-title contents dir — the patcher itself
            # creates the latter, so a first-ever deploy to a freshly-mounted
            # card has the atmosphere dir but not yet the per-title dir.
            sd_root = state.get("sd_root") or ""
            atmosphere_dir = Path(sd_root) / "atmosphere" if sd_root else None
            if atmosphere_dir is None or not atmosphere_dir.is_dir():
                _ap_log.info(
                    "Auto-patch skipped: SD card not mounted at %s. The Switch "
                    "will use its existing romfs from the last session.",
                    sd_root,
                )
                return
        if not self.slot_data or "placements" not in self.slot_data:
            _ap_log.info(
                "Auto-patch skipped: slot_data has no placements. Regenerate "
                "the seed with a current apworld (older seeds, generated before "
                "fill_slot_data bundled placements, can't be auto-patched).",
            )
            return

        # The deploy target dictates the patcher's on-disk layout: Ryujinx
        # nests the mod under DreadRandovania, Atmosphere (real Switch / SD,
        # and our "custom" flat layout) writes into contents/<tid>/ with IPS
        # in the global exefs_patches tree. Pick the matching compatibility so
        # the seed doesn't land where the console can't read it.
        mod_compat = "ryujinx" if state.get("deploy_target") == "ryujinx" else "atmosphere"
        _ap_log.info(
            "Auto-patch: writing per-seed romfs overlay to %s (vanilla "
            "romfs: %s, compatibility: %s)",
            deploy_dir, romfs_path, mod_compat,
        )
        asyncio.ensure_future(
            self._run_patch(str(deploy_dir), str(romfs_path), mod_compatibility=mod_compat)
        )

    async def _ensure_patcher_python(self) -> None:
        from ..patcher_pipeline import autodetect_patcher_python, check_dependencies

        def _resolve() -> tuple[Optional[str], str]:
            configured = self.dreadvania_python
            if configured and check_dependencies(configured) is None:
                return configured, f"patcher Python OK: {configured}"
            return autodetect_patcher_python()

        path, message = await asyncio.to_thread(_resolve)
        self.patcher_python_status = message
        if path:
            self.dreadvania_python = path
            self.state.set_patcher_python(f"ready ({Path(path).name})")
            _ap_log.info(message)
        else:
            self.state.set_patcher_python("not installed — see Archipelago tab")
            _ap_log.warning("Patcher setup needed for auto-patch:")
            for line in message.splitlines():
                _ap_log.warning("  %s", line)

    # ---- auto-patch implementation (runs on AP-connect) --------------

    async def _run_patch(
        self,
        dreadvania_dir: str,
        vanilla_romfs_dir: str,
        *,
        mod_compatibility: Optional[str] = None,
    ) -> None:
        from ..patcher_pipeline import patch
        from .._setup.build import collect_build_outputs

        log.info("auto-patch: starting…")

        exefs_overlay = collect_build_outputs() or None
        if not exefs_overlay:
            log.warning(
                "auto-patch: no built sysmodule found to re-assert — the patcher's "
                "upstream subsdk9 (server-mode) will be used and the Switch won't "
                "dial the client. Run /setup's Build + Deploy steps."
            )

        def _do():
            return patch(
                placements=self.slot_data,
                dreadvania_install_dir=Path(_expand(dreadvania_dir)),
                vanilla_romfs_dir=Path(_expand(vanilla_romfs_dir)),
                python_executable=self.dreadvania_python,
                exefs_overlay=exefs_overlay,
                mod_compatibility=mod_compatibility,
            )

        try:
            result = await asyncio.to_thread(_do)
        except Exception as exc:
            log.exception("auto-patch: unhandled exception: %s", exc)
            return

        if result.ok:
            log.info("auto-patch: %s", result.message)
            if result.patcher_input_path:
                log.info("  patcher input: %s", result.patcher_input_path)
            for note in result.notes:
                log.info("  %s", note)
        else:
            log.error("auto-patch: %s", result.message)
            if result.cli_stderr_tail:
                for line in result.cli_stderr_tail.splitlines():
                    log.error("  | %s", line)

    # ---- Switch poll loop --------------------------------------------

    async def _poll_loop(self) -> None:
        """Every POLL_INTERVAL_SECONDS, ask the Switch for collected
        locations + game state, and forward to AP."""
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                if self._bridge is None or not self._bridge.is_connected():
                    return
                try:
                    await self._poll_once()
                except (ConnectionError, asyncio.TimeoutError, RuntimeError) as exc:
                    # asyncio.TimeoutError stringifies to '' — include the
                    # class name so the warning is actionable.
                    msg = str(exc) or type(exc).__name__
                    log.warning("Switch poll failed: %s; will retry", msg)
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise

    async def _poll_once(self) -> None:
        if self._bridge is None or not self._bootstrapped:
            return
        # Every poll query uses POLL_LUA_TIMEOUT (not the 45 s bootstrap
        # ceiling): a death triggers a scenario reload that stalls the
        # game-thread Lua tick for tens of seconds, and these calls serialize
        # on the bridge's per-connection exec_lock. A long timeout here would
        # let the first call that lands in that window head-of-line-block new
        # checks and /warp for ~45 s. Short timeout ⇒ it drains and retries
        # next tick instead.
        T = POLL_LUA_TIMEOUT
        # Read the game mode FIRST so we can skip the heavy idempotent queries
        # while the game thread is stalled (reload/cutscene). Refreshing the
        # mode also lets _attempt_delivery fire only while the player is in the
        # game world (not the title/load menu): delivering on a menu sets a
        # PendingPickup against transient pre-save state that can be orphaned
        # across the menu→save transition, head-of-line-blocking the delivery
        # queue (RL.ReceivePickup ignores resends while a pending is set).
        # Mid-cutscene deliveries are still safe because the bootstrap's
        # GivePendingPickup defers them until Scenario.IsUserInteractionEnabled.
        mode_resp = await self._bridge.run_lua(
            "return tostring(Game.GetCurrentGameModeID())", timeout=T
        )
        ingame = False
        if mode_resp.success and mode_resp.payload is not None:
            mode = mode_resp.payload.decode("utf-8", "replace")
            self.state.update_game_state(game_mode_id=mode)
            ingame = mode == "INGAME"
        # The three pickup/inventory pushes are only meaningful in-world, and
        # firing them during a death reload just wastes timeouts and risks
        # orphaned pushes. Skip them until the reload settles back to INGAME;
        # the next tick (2 s) picks up anything collected during the gap.
        if ingame:
            await self._bridge.run_lua(
                "RL.GetInventoryAndSend(); return ''", timeout=T)
            await self._bridge.run_lua(
                "RL.GetCollectedIndicesAndSend(); return ''", timeout=T)
            await self._bridge.run_lua(
                "RL.GetReceivedPickupsAndSend(); return ''", timeout=T)
        # Goal + death detection run regardless of INGAME: the win sequence and
        # the death reload are themselves non-INGAME states.
        state_resp = await self._bridge.run_lua(
            "return tostring(Init.bBeatenSinceLastReboot)", timeout=T
        )
        if state_resp.success and state_resp.payload == b"true":
            self.state.update_game_state(beaten_since_reboot=True)
            await self._maybe_report_goal()
        if "DeathLink" in self.tags:
            death_resp = await self._bridge.run_lua(
                build_read_death_count_lua(), timeout=T)
            if death_resp.success:
                try:
                    await self._maybe_report_death(int(death_resp.payload))
                except ValueError:
                    log.debug("unparseable death count: %r", death_resp.payload[:32])
        await self._attempt_delivery()

    # ---- Push handlers (JSON dataclass dispatch) ---------------------

    async def _on_switch_push(self, msg: Any) -> None:
        """Bridge fans push messages here. ``msg`` is a wire dataclass."""
        if isinstance(msg, W.Collected):
            await self._handle_collected(msg)
            return
        if isinstance(msg, W.Inventory):
            self._handle_inventory(msg)
            return
        if isinstance(msg, W.ReceivedPickups):
            await self._handle_received_pickups(msg)
            return
        if isinstance(msg, W.GameState):
            await self._handle_game_state(msg)
            return
        if isinstance(msg, W.Log):
            self.state.add_log(msg.msg)
            _switch_log.info("[switch] %s", msg.msg)
            return
        if isinstance(msg, W.LayoutUuid):
            self.state.update_game_state(layout_uuid=msg.value)
            return
        log.debug("ignoring push of type %s", type(msg).__name__)

    async def _handle_collected(self, msg: W.Collected) -> None:
        """Parse the ``hex`` bitfield and emit ``LocationChecks`` for any
        newly-collected pickup_indices."""
        try:
            bitfield = bytes.fromhex(msg.hex) if msg.hex else b""
        except ValueError:
            log.warning("Collected.hex was not valid hex: %r", msg.hex[:64])
            return
        new_loc_ids: list[int] = []
        for byte_idx, byte_val in enumerate(bitfield):
            if not byte_val:
                continue
            for bit in range(8):
                if not (byte_val & (1 << bit)):
                    continue
                pickup_index = byte_idx * 8 + bit
                loc_id = self.datapackage.pickup_index_to_location_id(pickup_index)
                if loc_id is None:
                    log.debug("collected pickup_index %d has no known location; skipping",
                              pickup_index)
                    continue
                pickup = self.datapackage.location_id_to_pickup(loc_id)
                evt = CollectedLocationEvent(location_id=loc_id, pickup=pickup)
                if self.state.mark_collected(evt):
                    new_loc_ids.append(loc_id)
        if new_loc_ids:
            log.info("forwarding %d collected location(s) to AP", len(new_loc_ids))
            await self.send_msgs([{"cmd": "LocationChecks",
                                   "locations": new_loc_ids}])

    async def _handle_received_pickups(self, msg: W.ReceivedPickups) -> None:
        """Record the game's ``Blackboard.ReceivedPickups`` count (the delivery
        cursor) and log newly-confirmed items into the diagnostics mirror.

        Like the old binary-wire handler, this runs on the bridge's read loop;
        it must NOT call ``run_lua`` directly (and so must not ``await
        _attempt_delivery``, which does). Delivery is normally driven from the
        poll task and the AP-message task. The one exception is a cursor REVERT
        (``count < previous``: a save-reload / warp rewound the game's delivery
        cursor, so the remote items past the new lower cursor must be re-sent):
        that re-delivery is *scheduled* onto a tracked task that runs OFF the
        read loop, never awaited here. See ``_schedule_revert_delivery``.
        """
        count = msg.count
        previous = self.state.game_received_pickups()
        if count > previous:
            for idx in range(previous, min(count, len(self._ap_items))):
                ni = self._ap_items[idx]
                if ni is None:
                    continue
                dread_item, sender = self._resolve_item(ni)
                if dread_item is not None:
                    self.state.append_received(ReceivedItemEvent(
                        item=dread_item, sender=sender, inventory_index=idx))
            log.debug("game ReceivedPickups advanced %d -> %d", previous, count)
        self.state.set_game_received_pickups(count)
        if count != previous:
            # The game's delivery cursor moved: an advance confirms the pending
            # item landed; a revert (save-reload / warp) rewinds it. Either way
            # the stall tracking for the OLD cursor is now stale — a fresh
            # attempt cycle begins here. Reset it so the "has not landed after N
            # attempts" warning counts only CONSECUTIVE no-progress sends of one
            # index, and never SUMS across separate (successful) re-deliveries of
            # the same index after a save-reload. Without this, three successful
            # Morph-Ball deliveries (one original + two re-deliveries the player
            # triggered by reloading a pre-pickup save) accumulated into a "not
            # landed after 3 attempts" false alarm on the third send — which
            # itself landed normally.
            self._delivery_attempts = 0
            self._delivery_index = -1
        # The cursor moved — drive the next delivery from here, not just the 2s
        # poll. Either way it must run OFF the read loop, since _attempt_delivery
        # awaits a run_lua reply only this loop can read (awaiting here deadlocks).
        if count > previous:
            # Forward advance: a received backlog ("release") — each grant's
            # count-advance push clocks the next item. Spawn an attempt per push;
            # an over-eager/duplicate send is a no-op (the game rejects any
            # ReceivePickup whose index doesn't match its live counter), so the
            # forward path does NOT want the revert path's single-flight
            # coalescing — that would drop a push landing while a prior attempt is
            # still finishing, stalling the drain.
            asyncio.ensure_future(self._attempt_delivery())
        elif count < previous:
            # Cursor reverted (save-reload / warp): re-deliver the dropped remote
            # items via the coalesced path (which also yields to an in-flight warp
            # doing its own restore).
            log.debug("game ReceivedPickups reverted %d -> %d; scheduling "
                      "re-delivery off the read loop", previous, count)
            self._schedule_revert_delivery()

    def _schedule_revert_delivery(self) -> None:
        """Schedule a single ``_attempt_delivery`` to run off the read loop.

        Called from ``_handle_received_pickups`` (which runs ON the read loop and
        therefore cannot await ``run_lua``) for the cursor-revert case
        (save-reload / warp). The task is tracked and non-overlapping: a burst of
        revert pushes coalesces into at most one in-flight delivery task, which
        re-reads the latest cursor when it fires, so we never spawn
        unbounded/overlapping tasks nor fight the warp path. The warp path does
        its own restore under ``_warp_in_progress``; ``_attempt_delivery`` already
        no-ops while that flag is set, so a revert push observed mid-warp won't
        double-deliver. (The forward backlog-drain case does NOT use this — see
        ``_handle_received_pickups`` for why it spawns per-push instead.)
        """
        if self._warp_in_progress:
            # The warp path is rewinding the cursor itself and will re-deliver
            # via its own restore; don't race it. _attempt_delivery would no-op
            # anyway, but skip the task churn.
            return
        if self._revert_delivery_task is not None and \
                not self._revert_delivery_task.done():
            # A delivery is already queued/running; it will read the latest
            # cursor when it fires, so coalesce this push into it.
            return
        self._revert_delivery_task = asyncio.create_task(
            self._attempt_delivery(), name="dread-delivery-off-loop")

    def _resolve_item(self, network_item: Any) -> tuple[Optional[DreadItem], str]:
        item_id = _field(network_item, "item", 0)
        sender_idx = _field(network_item, "player", 2)
        dread_item = self.datapackage.ap_id_to_dread(int(item_id))
        if dread_item is None:
            return None, ""
        return dread_item, self._sender_name(int(sender_idx))

    def _handle_inventory(self, msg: W.Inventory) -> None:
        self.state.set_game_inventory_index(int(msg.index))
        stashed = {f"slot{i}": int(round(v)) for i, v in enumerate(msg.inventory)}
        self.state.set_inventory(stashed)

    async def _handle_game_state(self, msg: W.GameState) -> None:
        self.state.update_game_state(scenario_id=msg.scenario,
                                     beaten_since_reboot=msg.beaten)
        if msg.beaten:
            await self._maybe_report_goal()

    async def _maybe_report_goal(self) -> None:
        if self._goal_reported:
            return
        self._goal_reported = True
        log.info("Goal reached — reporting to AP server")
        await self.send_msgs([{"cmd": "StatusUpdate",
                               "status": ClientStatus.CLIENT_GOAL}])

    # ---- Misc --------------------------------------------------------

    async def _poke_lua(self, source: str) -> None:
        if self._bridge is None or not self._bridge.is_connected():
            log.warning("no active Switch; /poke ignored")
            return
        try:
            resp = await self._bridge.run_lua(source)
        except (RuntimeError, ConnectionError, asyncio.TimeoutError) as exc:
            log.warning("/poke failed: %s", exc)
            return
        log.info("poke reply: success=%s payload=%r", resp.success, resp.payload[:200])

    async def _warp_to_start(self) -> str:
        """Softlock recovery: warp Samus back to the starting actor.

        Same ``Game.LoadScenario`` primitive Randovania exposes via ZL+ZR at a
        save station (custom_scenario.lua:CheckWarpToStart). The crucial
        difference: upstream only fires it AT a save station, where state was
        just committed, so nothing is lost. Our ``/warp`` fires from anywhere —
        and ``Game.LoadScenario`` reloads Samus from the last SAVE (it is NOT
        checkpoint-continuous like a door transition), so every pickup collected
        since the last save reverts. Remote AP items self-heal (their
        ``ReceivedPickups`` cursor reverts too, so ``_attempt_delivery`` re-sends
        them), but the player's OWN seed-baked pickups are granted locally and
        AP never re-delivers them — so without a rewind they're lost for good
        (the reported missing-Energy-Tank bug). So we snapshot inventory + the
        delivery cursor before the warp and re-grant whatever reverted once the
        reload settles (see :meth:`_restore_after_warp`). The diff makes this a
        no-op for anything that did NOT revert.

        Gated client-side on (a) bridge connected + bootstrap done, and
        in-Lua on (b) ``Game.GetCurrentGameModeID() == "INGAME"``,
        (c) ``not RL.IsInBossArena()`` — refusing to warp out of a boss arena,
        which corrupts the encounter (the Kraid brick: re-entry breaks the
        fight, death-respawn bricks the game) — (c2) ``not RL.IsInNavRoom()`` and
        (c3) ``not RL.IsInSaveRoom()`` — refusing to warp out of a Navigation
        (Adam) room or a save station, where the conversation / save dialog
        survives the reload and strands an undismissable box (these collision-camera
        detections live in ``lua/warp_guard.lua``) — and (d)
        ``Scenario.IsUserInteractionEnabled(true)``, so a /warp issued from the
        title screen, a boss fight, a Nav/save room, or mid-cutscene returns a
        human-readable "blocked" reason instead of firing into invalid state.
        Returns the status string the caller surfaces to the user."""
        if self._bridge is None or not self._bridge.is_connected():
            return "no active Switch connected"
        if not self._bootstrapped:
            return "bootstrap not complete (wait for the connect handshake)"

        # Snapshot BEFORE the warp so we can rewind whatever the reload reverts:
        # inventory (re-granted), the delivery cursor (rewound), and the set of
        # collected locations (re-asserted so reverted pickups don't respawn and
        # become re-collectable for a duplicate).
        try:
            inv_before = await self._read_inventory_amounts()
            recv_before = await self._read_received_pickups()
            collected_before = await self._read_collected_indices()
        except (ConnectionError, asyncio.TimeoutError, RuntimeError) as exc:
            log.warning("/warp pre-snapshot failed: %s", exc)
            return f"failed reading state before warp: {exc}"

        # Suppress remote delivery across the whole warp+restore window (see
        # _attempt_delivery) so a delivery tick can't grant an item the restore
        # diff would double-count.
        self._warp_in_progress = True
        try:
            src = (
                'if Game.GetCurrentGameModeID() ~= "INGAME" then return "not_ingame" end '
                # Refuse to warp out of a boss arena — Game.LoadScenario mid-fight
                # corrupts the encounter (the Kraid brick). RL.IsInBossArena is
                # defined by lua/warp_guard.lua in our bootstrap; guard the call
                # so an older/partial VM (function nil) degrades to allowing warp
                # rather than erroring.
                'if RL.IsInBossArena and RL.IsInBossArena() then return "in_boss" end '
                # Refuse to warp out of a Navigation (Adam) room — the conversation
                # keeps Samus controllable but leaves a dialogue box that
                # Game.LoadScenario doesn't tear down, stranding an undismissable
                # text box. RL.IsInNavRoom is defined by lua/warp_guard.lua; guard
                # the call so a pre-bootstrap VM (function nil) degrades to allowing
                # the warp. A Nav room is a safe hub, so blocking here is free.
                'if RL.IsInNavRoom and RL.IsInNavRoom() then return "in_nav" end '
                # Refuse to warp from a save station — same stranded-dialog risk as
                # a Nav room (the save box survives LoadScenario), and a save room
                # is a safe hub where /warp is never needed (just save + reload).
                # RL.IsInSaveRoom is defined by lua/warp_guard.lua; guard the call
                # so a pre-bootstrap VM degrades to allowing the warp.
                'if RL.IsInSaveRoom and RL.IsInSaveRoom() then return "in_save" end '
                'if not Scenario.IsUserInteractionEnabled(true) then return "no_interaction" end '
                'Game.LoadScenario("c10_samus", Init.sStartingScenario, Init.sStartingActor, "", 1) '
                'return "ok"'
            )
            try:
                resp = await self._bridge.run_lua(src)
            except (RuntimeError, ConnectionError, asyncio.TimeoutError) as exc:
                log.warning("/warp failed: %s", exc)
                return f"failed: {exc}"
            body = resp.payload.decode("utf-8", "replace").strip()
            if not resp.success:
                return f"lua error: {body[:200]!r}"
            if body == "not_ingame":
                return "blocked: not in-game (title or load menu)"
            if body == "in_boss":
                return ("blocked: you're in a boss arena — warping mid-fight "
                        "corrupts the encounter (re-entry/respawn can brick the "
                        "game). If you're stuck, reload your last save from the "
                        "title screen.")
            if body == "in_nav":
                return ("blocked: you're in a Navigation (Adam) room — warping "
                        "mid-conversation strands the dialogue box (you'd keep "
                        "control with an undismissable text box). Finish talking "
                        "to Adam or step out of the room, then /warp.")
            if body == "in_save":
                return ("blocked: you're at a save station — no need to /warp from "
                        "a safe room, and a warp here can strand the save dialog. "
                        "Save and reload from the title if you're stuck, or step "
                        "out of the room before /warp.")
            if body == "no_interaction":
                return "blocked: cutscene/cinematic in progress — try again in a moment"
            if body != "ok":
                return f"unexpected reply: {body!r}"
            return await self._restore_after_warp(
                inv_before, recv_before, collected_before)
        finally:
            self._warp_in_progress = False

    async def _restore_after_warp(
        self, inv_before: dict[str, float], recv_before: int,
        collected_before: list[int],
    ) -> str:
        """Rewind everything the ``Game.LoadScenario`` warp reverted.

        Waits for the reload to return to INGAME, then:
          * re-grants every item whose amount dropped, via its ``OnPickedUp``
            (the same per-item class delivery uses);
          * re-asserts the ``Location_Collected_*`` prop for every location that
            was collected pre-warp, so reverted pickups don't respawn and become
            re-collectable for a duplicate (``OnPickedUp`` does NOT gate on the
            prop — re-asserting it stops the actor respawning at the next
            scenario load, which is what prevents the dupe);
          * rewinds the ``ReceivedPickups`` cursor to its pre-warp value so the
            remote items in the restored delta aren't ALSO re-delivered by
            ``_attempt_delivery`` (the game's index-match guards against
            double-grant anyway, but this keeps the cursor honest)."""
        assert self._bridge is not None
        if not await self._await_ingame(WARP_RESTORE_TIMEOUT_SECONDS):
            log.warning("/warp: game did not return to INGAME within %ss; "
                        "skipping inventory restore",
                        WARP_RESTORE_TIMEOUT_SECONDS)
            return ("warped, but could not confirm the reload finished — if any "
                    "items are missing, reconnect to re-sync")
        # Let the loaded save settle so GetItemAmount reflects the post-load state.
        await asyncio.sleep(WARP_RESTORE_SETTLE_SECONDS)
        try:
            inv_after = await self._read_inventory_amounts()
        except (ConnectionError, asyncio.TimeoutError, RuntimeError) as exc:
            log.warning("/warp post-snapshot failed: %s", exc)
            return f"warped, but failed reading inventory after warp: {exc}"

        deficits: list[tuple[str, int]] = []
        for item_id, before in inv_before.items():
            qty = int(round(before - inv_after.get(item_id, 0.0)))
            if qty > 0:
                deficits.append((item_id, qty))

        restored: list[str] = []
        for item_id, qty in deficits:
            cls = pickup_class_for(item_id)
            try:
                await self._bridge.run_lua(
                    build_restore_grant_lua(item_id, qty, cls))
            except (ConnectionError, asyncio.TimeoutError, RuntimeError) as exc:
                log.warning("/warp restore of %s x%d failed: %s",
                            item_id, qty, exc)
                continue
            restored.append(f"{item_id} x{qty}")
            log.info("/warp restored %s x%d (cls=%s)", item_id, qty, cls)

        # Re-assert collected locations so reverted pickups can't be re-collected.
        if collected_before:
            try:
                await self._bridge.run_lua(
                    build_mark_collected_lua(collected_before))
                log.info("/warp re-asserted %d collected location(s)",
                         len(collected_before))
            except (ConnectionError, asyncio.TimeoutError, RuntimeError) as exc:
                log.warning("/warp could not re-assert collected locations: %s",
                            exc)

        # Rewind the delivery cursor so restored remote items aren't re-sent.
        try:
            await self._bridge.run_lua(build_set_received_pickups_lua(recv_before))
            self.state.set_game_received_pickups(recv_before)
        except (ConnectionError, asyncio.TimeoutError, RuntimeError) as exc:
            log.warning("/warp could not reset ReceivedPickups to %d: %s",
                        recv_before, exc)

        if not restored:
            return "warped — no pickups needed restoring"
        return ("warped — restored {n} reverted pickup(s): {names}. Save at the "
                "starting save station to commit them.").format(
                    n=len(restored), names=", ".join(restored))

    async def _read_inventory_amounts(self) -> dict[str, float]:
        """Read live per-item amounts (``ITEM_id -> amount``) straight from the
        game, parsed from the self-describing string in the run_lua reply."""
        assert self._bridge is not None
        resp = await self._bridge.run_lua(build_read_inventory_amounts_lua())
        body = resp.payload.decode("utf-8", "replace").strip() if resp.payload else ""
        amounts: dict[str, float] = {}
        for pair in body.split(";"):
            key, sep, val = pair.partition("=")
            if not sep:
                continue
            try:
                amounts[key.strip()] = float(val)
            except ValueError:
                continue
        return amounts

    async def _read_received_pickups(self) -> int:
        assert self._bridge is not None
        resp = await self._bridge.run_lua(build_read_received_pickups_lua())
        body = resp.payload.decode("utf-8", "replace").strip() if resp.payload else "0"
        try:
            return int(float(body))
        except ValueError:
            return 0

    async def _read_collected_indices(self) -> list[int]:
        """Read the pickup indices currently marked collected in-game."""
        assert self._bridge is not None
        resp = await self._bridge.run_lua(build_read_collected_indices_lua())
        body = resp.payload.decode("utf-8", "replace").strip() if resp.payload else ""
        indices: list[int] = []
        for tok in body.split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                indices.append(int(tok))
            except ValueError:
                continue
        return indices

    async def _await_ingame(self, timeout: float) -> bool:
        """Poll the game mode until INGAME or ``timeout`` seconds elapse."""
        assert self._bridge is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = await self._bridge.run_lua(
                "return tostring(Game.GetCurrentGameModeID())")
            mode = (resp.payload.decode("utf-8", "replace").strip()
                    if resp.payload else "")
            if mode:
                self.state.update_game_state(game_mode_id=mode)
            if mode == "INGAME":
                return True
            await asyncio.sleep(WARP_RESTORE_POLL_SECONDS)
        return False
