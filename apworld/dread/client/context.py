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
from typing import Any, Iterable, Optional

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
    build_read_death_count_lua,
    build_read_inventory_amounts_lua,
    build_read_received_pickups_lua,
    build_read_collected_indices_lua,
    build_mark_collected_lua,
    build_restore_grant_lua,
    build_set_received_pickups_lua,
    build_warp_src,
    build_read_current_subarea_lua,
    pickup_class_for,
    pickup_resource_stage,
    NAV_HINT_STATIONS,
    WARP_TARGETS_BY_REGION,
    WARP_TARGET_BY_CAMERA,
    _lua_string,
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

    ``dreadvania_python`` is an ``OptionalUserFilePath``, which AP resolves
    relative to its user-data directory. A BLANK setting (the default — "let
    the client auto-detect") therefore resolves to the user folder itself
    (``os.path.join(user_path(), "") == "<...>/Archipelago/"``), a directory.
    A directory is never a Python interpreter, so treat any value that resolves
    to a directory as unset and fall through to autodetection — otherwise the
    blank default gets handed to ``subprocess`` as an "interpreter" and exec'ing
    a directory raises PermissionError. A non-existent path is still returned so
    a genuine typo surfaces the "configured Python not found" diagnostic.
    """
    try:
        import settings as ap_settings
        val = str(ap_settings.get_settings()["dread_options"]["dreadvania_python"])
    except Exception:
        return None
    if not val or os.path.isdir(val):
        return None
    return val


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

    def _cmd_warp(self, *args: str) -> bool:
        """``/warp [region [station]] | list`` — softlock recovery via
        ``Game.LoadScenario``.

        ``/warp`` (no arg) warps to the STARTING save station — the same
        ``Scenario.CheckWarpToStart`` primitive Randovania exposes via ZL+ZR at a
        save station, but invokable from anywhere. ``/warp <region> [station]``
        warps to a specific station you have VISITED — a save, navigation
        ("Adam"), or map station (e.g. ``/warp dairon``, ``/warp dairon west``,
        ``/warp dairon nav north``, ``/warp dairon map``) — the recovery for a
        player who toggled themselves out of a region's only local route (e.g. the
        Cataris thermal-device trap that can sever the through-room path to
        Dairon). It restores access you already had; you can't warp to a station
        you never reached. ``/warp list`` shows the recorded targets. Inventory and
        per-pickup collected bits persist (same primitive every door transition
        uses); save at the destination to commit them.
        """
        ctx = self.ctx
        region = args[0] if args else ""
        # Station names can be multi-word ("nav north"), so take the rest.
        station = " ".join(args[1:])
        async def _go():
            if not region:
                msg = await ctx._warp_to_start()
            elif region.lower() == "list":
                msg = ctx._warp_list()
            else:
                msg = await ctx._warp_to_region(region, station)
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

        # Scenarios the active Switch has reported being in (via game_state) —
        # used for friendlier /warp errors ("you've been to Dairon but not at a
        # save station there yet").
        self._visited_scenarios: set[str] = set()
        # Warp targets (save / nav / map stations) the player has stood in, as
        # (scenario, collision_camera) keys into protocol.WARP_TARGET_BY_CAMERA.
        # Accumulated each poll from the live subarea the warp guard tracks; gates
        # ``/warp <region> [station]`` so it only restores access you ALREADY had.
        # Client-owned, so it survives a Lua re-bootstrap (reconnect); reset only
        # on a fresh client session. (Named ``_visited_saves`` from when save
        # stations were the only warp targets.) Mirrored into AP DataStorage
        # (``_warp_visited_key``) so a client RESTART — not just a reconnect —
        # can keep warping: the recovery use case (you're stuck) is exactly when
        # you may be unable to re-reach a station to re-record it.
        self._visited_saves: set[tuple[str, str]] = set()
        # AP DataStorage key holding the visited set, seed+slot scoped so it never
        # bleeds across seeds. Set in _on_connected once slot/seed are known; None
        # while offline / pre-connect (persistence is then a no-op).
        self._warp_visited_key: Optional[str] = None

        # Nav Station hint registration. Built from slot_data on connect:
        # (scenario, camera) -> [(location_owner_slot, location_id), ...] for the
        # plaque shown at that Nav Station. When the player reaches the room, the
        # revealed location is registered as a FREE AP server hint (CreateHints /
        # LocationScouts create_as_hint). ``_registered_nav_hints`` dedupes so a
        # re-visit doesn't re-send.
        self._nav_hint_by_camera: dict[tuple[str, str], list[tuple[int, int]]] = {}
        self._registered_nav_hints: set[tuple[int, int]] = set()

        # The location_ids THIS slot actually holds, from the Connected packet
        # (``missing_locations | checked_locations`` — the same union AP's
        # CommonContext keeps as ``server_locations``). It is a SUBSET of the
        # static data-package table: World._compute_dropped_locations drops
        # pickups that a full loadout still can't reach under the slot's options
        # (e.g. the 8 Speedbooster-gated spots when that trick is disabled), and
        # those AP locations are never created. Sending a dropped id in
        # ``LocationScouts`` / ``CreateHints`` raises KeyError inside AP's
        # MultiServer, which kills the connection — so every location id we put
        # on the wire is filtered through this set. Empty until Connected (and
        # for pre-slot_data seeds where the server sent no lists), which
        # ``_slot_locations`` treats as "no filter available".
        self._server_locations: set[int] = set()

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
        # (don't report on the first read after connect). Only readings taken
        # while a save is loaded may set or lower it — see _maybe_report_death,
        # which explains why a menu reading is not a real zero.
        # ``_suppress_death_until``
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
        elif cmd == "Retrieved":
            # Initial sync of the persisted visited-warp set (response to the Get
            # set_notify issued). Merge server -> local; if local also holds
            # entries the server lacks (observed while AP was disconnected), push
            # the union back so they survive too.
            if self._absorb_visited_storage(args):
                asyncio.ensure_future(self._persist_visited())
        elif cmd == "SetReply":
            # Another client (same slot, different device) recorded a station —
            # merge it. Merge only, never push, so our own Set can't ping-pong.
            self._absorb_visited_storage(args)

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

    def _absorb_server_locations(self, args: dict) -> None:
        """Populate ``_server_locations`` from a ``Connected`` packet.

        Read straight out of ``args`` rather than off ``CommonContext``'s
        ``server_locations`` attribute so we don't depend on AP's package-handling
        order (our handler is scheduled from ``on_package``)."""
        found: set[int] = set()
        for key in ("missing_locations", "checked_locations"):
            for loc in args.get(key) or ():
                try:
                    found.add(int(loc))
                except (TypeError, ValueError):
                    continue
        self._server_locations = found
        known = len(self.datapackage.all_location_ids())
        if found and len(found) < known:
            log.info("slot holds %d of the %d data-package locations; %d were "
                     "dropped at generation (unreachable under this slot's "
                     "options) and will not be scouted or hinted",
                     len(found), known, known - len(found))

    def _slot_locations(self, location_ids: Iterable[int]) -> list[int]:
        """Filter ``location_ids`` down to the ones this slot actually holds.

        AP's MultiServer indexes ``ctx.locations[slot]`` directly for
        ``LocationScouts`` / own-slot ``CreateHints``; an id the slot doesn't have
        raises ``KeyError`` there, which aborts the command handler and drops the
        client (see issue #172 — the client then reconnect-loops forever). Passing
        no filter (pre-Connected, or a server that sent empty lists) returns the
        input unchanged."""
        ids = [int(i) for i in location_ids]
        if not self._server_locations:
            return ids
        return [i for i in ids if i in self._server_locations]

    async def _on_connected(self, args: dict) -> None:
        self.state.set_ap_conn("connected")
        self.state.slot = self.username or ""
        # Record the slot's real location set before anything sends location ids.
        self._absorb_server_locations(args)
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
            # A station visited in the PRIOR seed isn't necessarily reached in
            # THIS one, so drop the in-memory set; this seed's own DataStorage key
            # (below) restores the correct set for it.
            self._visited_saves.clear()
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
        # Subscribe to the persisted visited-warp set for THIS seed+slot. The Get
        # set_notify issues lands as a ``Retrieved`` that repopulates
        # _visited_saves (so a restarted client keeps its /warp targets), and the
        # SetNotify keeps multiple clients on the same slot in sync. Scoped by seed
        # so it never restores a different seed's stations. Offline (no seed) ⇒
        # left None, persistence is a no-op.
        if new_seed and self.slot is not None:
            self._warp_visited_key = (
                f"dread_warp_visited_{new_seed}_{self.team}_{self.slot}")
            self.set_notify(self._warp_visited_key)
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
            self._build_nav_hint_map(sd)
        # Mirror the seed's DeathLink choice into the AP connection tag. Sends a
        # ConnectUpdate (we're already Connected here), which is the supported
        # way to toggle the tag post-connect.
        await self.update_death_link(bool(self.slot_data.get("death_link")))
        asyncio.ensure_future(self._maybe_auto_patch())
        # Scout only what the slot actually holds — a dropped location id here is
        # a hard server-side KeyError + disconnect. See _slot_locations.
        loc_ids = self._slot_locations(self.datapackage.all_location_ids())
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
        ``RL.KillPlayer`` no-ops outside INGAME, defers through cutscenes, and
        DROPS the kill while Samus is in a Save/Nav/Map station (see
        ``lua/deathlink.lua``). It returns a status string; on any outcome that
        won't produce the self-death we armed ``_suppress_death_until`` for, we
        clear the window so a later genuine death still broadcasts (only
        ``killed`` / ``deferred`` will actually yield a death to swallow)."""
        if self._bridge is None or not self._bridge.is_connected() or not self._bootstrapped:
            log.info("DeathLink received but no Switch ready; ignoring kill")
            self._suppress_death_until = 0.0
            return
        try:
            resp = await self._bridge.run_lua(build_kill_player_lua())
        except (ConnectionError, asyncio.TimeoutError, RuntimeError) as exc:
            log.warning("DeathLink kill send failed: %s", exc)
            # Couldn't kill ⇒ no self-death will follow, so clear the guard.
            self._suppress_death_until = 0.0
            return
        status = resp.payload.decode("utf-8", "replace").strip()
        if status == "in_safe_room":
            log.info("DeathLink kill dropped: player is at a save/nav/map station")
        if status not in ("killed", "deferred"):
            # No self-death will follow (dropped, at a menu, or no player), so
            # don't leave a window armed that could mute a real death.
            self._suppress_death_until = 0.0

    async def _maybe_report_death(
        self, count: Optional[int], ingame: bool
    ) -> None:
        """Edge-detect the game's death counter and broadcast to AP.

        ``count`` is the freshly-read ProgressStat_PlayerDeaths value, or None
        when the prop is absent (no save loaded). ``ingame`` is this tick's
        ``Game.GetCurrentGameModeID() == 'INGAME'``.

        Only an IN-GAME reading may establish or LOWER the baseline. Outside
        the game world the prop reads absent or 0 — a fresh boot's title screen
        has no save context — so a menu reading is not a real "0 deaths". The
        client used to accept it: on an emulator restart (or any quit-to-title)
        the baseline fell to 0 at the title screen, and loading the save then
        looked like the whole file's death count arriving at once, broadcasting
        a spurious DeathLink on every reload. A rise is still honoured in ANY
        mode, because the post-death reload is itself a non-INGAME state and
        the increment can land mid-reload; only downward/first moves are
        gated. A death that we caused via an incoming DeathLink is swallowed
        once (the suppression window) so the chain terminates instead of
        echoing."""
        if count is None:
            # No reading at all. Leave any existing baseline alone rather than
            # clearing it — the prop can also read absent transiently during
            # the post-death reload, and clearing there would swallow that
            # death on the way back in.
            return
        prev = self._last_death_count
        if prev is None:
            if ingame:
                self._last_death_count = count
            return  # baseline (in-world readings only)
        if count < prev:
            # Went backwards: an earlier save file was loaded, or the game
            # rebooted. Re-baseline silently, but only from in-world — a
            # title-screen 0 must not drag the baseline down, or the next
            # save load reads as a pile of deaths.
            if ingame:
                log.info("death count went backwards (%d -> %d); re-baselining",
                         prev, count)
                self._last_death_count = count
            return
        if count == prev:
            return
        self._last_death_count = count
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
        # The bridge-IP override the user set on the Deploy page (empty ⇒
        # auto-detect via detect_lan_ip in patch()). Threaded through so the
        # re-asserted rom:/ap_config.json targets the same LAN the Deploy step
        # would have — see patcher_pipeline.patch step 4(c).
        bridge_host = state.get("bridge_host") or None
        _ap_log.info(
            "Auto-patch: writing per-seed romfs overlay to %s (vanilla "
            "romfs: %s, compatibility: %s, bridge_host: %s)",
            deploy_dir, romfs_path, mod_compat, bridge_host or "(auto)",
        )
        asyncio.ensure_future(
            self._run_patch(
                str(deploy_dir), str(romfs_path),
                mod_compatibility=mod_compat, bridge_host=bridge_host,
            )
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
        bridge_host: Optional[str] = None,
    ) -> None:
        from ..patcher_pipeline import patch
        from .._setup.build import resolve_build_outputs

        log.info("auto-patch: starting…")

        # The patcher runs `<py> -m open_dread_rando`, so it needs a REAL Python
        # interpreter. When the client IS the frozen Archipelago launcher,
        # sys.executable is not usable and self.dreadvania_python must be
        # resolved first. _ensure_patcher_python runs at startup, but auto-patch
        # fires on AP-connect and can win that race — so (re)resolve here if it
        # hasn't produced a usable interpreter yet, then abort with an
        # actionable status rather than letting patch() invoke the launcher.
        if not self.dreadvania_python:
            await self._ensure_patcher_python()
        if not self.dreadvania_python:
            detail = self.patcher_python_status or (
                "No usable Python found for the patcher. Install Python 3 and "
                "the open_dread_rando deps, then re-run /setup so it records "
                "the interpreter."
            )
            self.state.set_patch_status("error", detail)
            _ap_log.error(
                "Auto-patch skipped: no usable patcher Python. Click the "
                "'Patcher' status at the top for setup steps.")
            return

        # MUST be resolve_build_outputs (prefers the apworld's bundled prebuilt
        # sysmodule), NOT collect_build_outputs (local source-build dir only).
        # The patcher writes the UPSTREAM server-mode subsdk9 into exefs via
        # open_dread_rando_exlaunch.include_depackager(); we re-assert our
        # bridge subsdk9 over it. On the normal prebuilt-release path there is
        # no local source build, so collect_build_outputs() returned {} and the
        # re-assert silently no-op'd — leaving the upstream :6969 server binary,
        # so the Switch never dialed the client. See the delivery-protocol notes.
        exefs_overlay = resolve_build_outputs() or None
        if not exefs_overlay:
            log.warning(
                "auto-patch: no sysmodule found to re-assert (neither a bundled "
                "prebuilt nor a local build) — the patcher's upstream subsdk9 "
                "(server-mode :6969) will be used and the Switch won't dial the "
                "client. Reinstall the apworld (it ships a prebuilt sysmodule) "
                "or run /setup's Build step."
            )

        def _do():
            return patch(
                placements=self.slot_data,
                dreadvania_install_dir=Path(_expand(dreadvania_dir)),
                vanilla_romfs_dir=Path(_expand(vanilla_romfs_dir)),
                python_executable=self.dreadvania_python,
                exefs_overlay=exefs_overlay,
                mod_compatibility=mod_compatibility,
                bridge_host=bridge_host,
            )

        try:
            result = await asyncio.to_thread(_do)
        except Exception as exc:
            log.exception("auto-patch: unhandled exception: %s", exc)
            # Surface on the always-visible pill + the main Archipelago tab so
            # the failure isn't buried in the Dread-tab log.
            self.state.set_patch_status("error", f"unhandled exception: {exc}")
            _ap_log.error(
                "Auto-patch failed (unhandled exception: %s). Click the "
                "'Patcher' status at the top for details.", exc)
            return

        if result.ok:
            log.info("auto-patch: %s", result.message)
            if result.patcher_input_path:
                log.info("  patcher input: %s", result.patcher_input_path)
            for note in result.notes:
                log.info("  %s", note)
            self.state.set_patch_status("ok", result.message)
        else:
            log.error("auto-patch: %s", result.message)
            if result.cli_stderr_tail:
                for line in result.cli_stderr_tail.splitlines():
                    log.error("  | %s", line)
            # Record the full detail for the pill popup, and put a one-line
            # pointer on the main Archipelago tab (which is visible by default)
            # so a failed patch is noticed without opening the Dread tab.
            detail = result.message
            if result.cli_stderr_tail:
                detail = f"{detail}\n\n{result.cli_stderr_tail}"
            self.state.set_patch_status("error", detail)
            _ap_log.error(
                "Auto-patch failed: %s — click the 'Patcher' status at the top "
                "(or see the Dread tab) for the full error.",
                result.message.splitlines()[0] if result.message else "see Dread tab")

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
            await self._record_room_visit(T)
        # Goal + death detection run regardless of INGAME: the win sequence and
        # the death reload are themselves non-INGAME states. (The death READ
        # runs in every mode; what the reading is allowed to do to the baseline
        # depends on `ingame` — see _maybe_report_death.)
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
                body = (death_resp.payload or b"").decode("utf-8", "replace")
                body = body.strip()
                count: Optional[int] = None
                if body:
                    try:
                        count = int(body)
                    except ValueError:
                        log.debug("unparseable death count: %r", body[:32])
                        count = None
                # An empty body means the prop is absent (no save loaded) —
                # distinct from a real 0, see _maybe_report_death.
                await self._maybe_report_death(count, ingame)
        await self._attempt_delivery()

    def _build_nav_hint_map(self, slot_data: dict) -> None:
        """Index the slot's Nav Station hints by the room (scenario, camera) that
        shows each, so reaching that room can register the revealed location as a
        free AP hint. ``nav_hints[i]`` is shown at ``NAV_HINT_STATIONS[i]`` (both
        in patcher-template plaque order); each carries ``loc = [owner, id]``."""
        self._nav_hint_by_camera = {}
        nav_hints = slot_data.get("nav_hints") or []
        for i, hint in enumerate(nav_hints):
            if i >= len(NAV_HINT_STATIONS):
                break
            loc = hint.get("loc") if isinstance(hint, dict) else None
            if not loc or len(loc) != 2:
                continue
            station = NAV_HINT_STATIONS[i]
            self._nav_hint_by_camera.setdefault(station, []).append(
                (int(loc[0]), int(loc[1])))

    async def _record_room_visit(self, timeout: float) -> None:
        """Read the live (scenario, collision-camera) the warp guard tracks and
        react to where the player is: record a warp target — save / nav / map
        station — (for ``/warp <region>``) and/or register a Nav Station's hint
        with the AP server. Non-fatal — a failed read or an untracked subarea just
        means nothing happens this tick."""
        assert self._bridge is not None
        try:
            resp = await self._bridge.run_lua(
                build_read_current_subarea_lua(), timeout=timeout)
        except (ConnectionError, asyncio.TimeoutError, RuntimeError):
            return
        if not resp.success or not resp.payload:
            return
        body = resp.payload.decode("utf-8", "replace").strip()
        scenario, _, camera = body.partition(",")
        key = (scenario.strip(), camera.strip())
        if "" in key:
            return
        target = WARP_TARGET_BY_CAMERA.get(key)
        if target is not None and key not in self._visited_saves:
            self._visited_saves.add(key)
            log.info("/warp: recorded %s %s", target.region, target.label)
            await self._persist_visited()
        if key in self._nav_hint_by_camera:
            await self._register_nav_hints(key)

    def _absorb_visited_storage(self, args: dict) -> bool:
        """Merge a ``Retrieved`` / ``SetReply`` payload for the visited-warp key
        into ``_visited_saves`` (server -> local union). Ignores unrelated keys
        and any stored pair that isn't a known warp target (defensive against a
        future table change). Returns True iff the LOCAL set still holds entries
        the stored value lacked — i.e. a push-back to storage is warranted."""
        key = self._warp_visited_key
        if not key:
            return False
        if "keys" in args:                       # Retrieved: {key: value, ...}
            if key not in args["keys"]:
                return False
            value = args["keys"][key]
        elif args.get("key") == key:             # SetReply: flat key/value
            value = args.get("value")
        else:
            return False
        stored: set[tuple[str, str]] = set()
        if isinstance(value, list):
            for pair in value:
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    t = (str(pair[0]), str(pair[1]))
                    if t in WARP_TARGET_BY_CAMERA:
                        stored.add(t)
        before = set(self._visited_saves)
        self._visited_saves |= stored
        restored = self._visited_saves - before
        if restored:
            log.info("/warp: restored %d station(s) from AP storage",
                     len(restored))
        return bool(before - stored)             # local-only entries to push

    async def _persist_visited(self) -> None:
        """Write the visited-warp set to AP DataStorage (full ``replace``). Safe
        no-op if there's no key yet or the AP socket is down (``send_msgs`` drops
        silently when disconnected). Volume is tiny (<=34 entries)."""
        key = self._warp_visited_key
        if not key:
            return
        payload = sorted(list(k) for k in self._visited_saves)
        try:
            await self.send_msgs([{
                "cmd": "Set", "key": key, "default": [], "want_reply": False,
                "operations": [{"operation": "replace", "value": payload}],
            }])
        except Exception as exc:  # noqa: BLE001 — never let persistence break the poll
            log.debug("/warp: persisting visited set failed: %s", exc)

    async def _register_nav_hints(self, station: tuple[str, str]) -> None:
        """Register the Nav Station's revealed location(s) as FREE AP server hints
        (so they show in ``!hints`` / trackers and ping the item owner), once each.

        Own locations go via ``LocationScouts`` ``create_as_hint`` (universally
        supported); a location in another world (an item hint — only valid because
        the item there is ours) goes via ``CreateHints``. Neither costs hint
        points. ``self.slot`` is set once Connected; bail if not."""
        my_slot = getattr(self, "slot", None)
        for owner, loc_id in self._nav_hint_by_camera.get(station, []):
            if (owner, loc_id) in self._registered_nav_hints:
                continue
            self._registered_nav_hints.add((owner, loc_id))
            if my_slot is not None and owner == my_slot:
                if not self._slot_locations([loc_id]):
                    # Not one of our slot's locations — hinting it would KeyError
                    # server-side and drop us. Shouldn't happen (plaques are built
                    # post-fill, and a dropped location is never filled), but the
                    # failure mode is a connection loop, so never risk it.
                    log.warning("skipping Nav Station hint for location %d: not "
                                "in this slot", loc_id)
                    continue
                packet = {"cmd": "LocationScouts", "locations": [loc_id],
                          "create_as_hint": 2}
            else:
                packet = {"cmd": "CreateHints", "player": owner,
                          "locations": [loc_id]}
            try:
                await self.send_msgs([packet])
                log.info("registered Nav Station hint: location %d (slot %d)",
                         loc_id, owner)
            except Exception as exc:  # noqa: BLE001 — never let a hint break the poll
                self._registered_nav_hints.discard((owner, loc_id))
                log.warning("Nav Station hint registration failed: %s", exc)

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
        if msg.scenario:
            self._visited_scenarios.add(msg.scenario)
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
        """Softlock recovery: warp Samus back to the STARTING save station."""
        return await self._warp(
            "Init.sStartingScenario", "Init.sStartingActor",
            "the starting save station")

    def _warp_list(self) -> str:
        """``/warp list`` — the stations recovery can warp to (the save / nav /
        map stations the player has been observed standing in)."""
        visited = [WARP_TARGET_BY_CAMERA[k] for k in self._visited_saves
                   if k in WARP_TARGET_BY_CAMERA]
        if not visited:
            return ("no stations recorded yet — stand at a save, nav, or map "
                    "station for a moment, then it becomes a /warp target")
        visited.sort(key=lambda s: (s.region, s.kind, s.name))
        return "visited stations: " + ", ".join(
            f"{s.region} {s.label}" for s in visited)

    async def _warp_to_region(self, region: str, station: str = "") -> str:
        """``/warp <region> [station]`` — warp to a specific station (save / nav /
        map) the player has VISITED. Recovers someone who toggled themselves out
        of a region's only local route (e.g. the Cataris thermal-device trap
        severing the path to Dairon): it restores access you ALREADY had, never
        grants new.

        Only stations the client has seen the player stand in (``_visited_saves``)
        are eligible — you can't warp somewhere you never reached. With multiple
        eligible stations in a region, ``station`` disambiguates by matching the
        target's kind-prefixed label (e.g. ``west``, ``nav north``, ``map``).
        Otherwise identical to warp-to-start (same guards + the
        inventory/cursor/collected rewind)."""
        key = region.strip().lower()
        stations = WARP_TARGETS_BY_REGION.get(key)
        if stations is None:
            known = ", ".join(sorted(WARP_TARGETS_BY_REGION))
            return f"unknown region {region!r}; try one of: {known}"
        region_name = stations[0].region
        visited = [s for s in stations
                   if (s.scenario, s.camera) in self._visited_saves]
        if not visited:
            if stations[0].scenario in self._visited_scenarios:
                return (f"you've been to {region_name} but I haven't seen you at "
                        "a save, nav, or map station there yet — stand at one for "
                        "a moment, then retry")
            return (f"you haven't been to {region_name} yet this session "
                    "(warp only restores access you already had)")
        if station:
            kw = station.strip().lower()
            matches = [s for s in visited if kw in s.label.lower()]
            if not matches:
                labels = ", ".join(s.label for s in visited)
                return (f"no visited {region_name} station matches "
                        f"{station!r}; visited: {labels}")
            if len(matches) > 1:
                labels = ", ".join(s.label for s in matches)
                return f"{station!r} is ambiguous in {region_name}: {labels}"
            chosen = matches[0]
        elif len(visited) == 1:
            chosen = visited[0]
        else:
            labels = ", ".join(s.label for s in visited)
            return (f"multiple {region_name} stations visited ({labels}) — "
                    f"pick one, e.g. /warp {key} {visited[0].label.lower()}")
        return await self._warp(
            _lua_string(chosen.scenario), _lua_string(chosen.spawn),
            f"the {chosen.region} {chosen.label} station")

    async def _warp(self, scenario_lua: str, actor_lua: str,
                    where: str) -> str:
        """Warp Samus via ``Game.LoadScenario`` to ``scenario_lua``/``actor_lua``
        (Lua expressions — ``Init.sStarting*`` for start, quoted literals for a
        region) and rewind whatever the reload reverts.

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
        fight, death-respawn bricks the game) — (c2) ``not RL.IsInNavRoom()``,
        (c3) ``not RL.IsInSaveRoom()`` and (c4) ``not RL.IsInMapRoom()`` —
        refusing to warp out of a Navigation (Adam) room, a save station, or a
        map station, where the conversation / save dialog / map overlay survives
        the reload and strands an undismissable box (these collision-camera
        detections live in ``lua/warp_guard.lua``) — (c5)
        ``not RL.IsInFlipperTrap()`` — refusing to warp out of the Ghavoran
        Flipper Room / Spider Magnet Elevator, where a mis-turned one-way flipper
        sticks the area and a warp only preserves the broken flip (the fix is a
        checkpoint reload, so we direct the player there) — and (d)
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
            # Guards (boss/nav/save/interaction) all inspect the CURRENT location,
            # so they apply regardless of destination. See protocol.build_warp_src.
            src = build_warp_src(scenario_lua, actor_lua)
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
            if body == "in_map":
                return ("blocked: you're at a map station — a safe room you can "
                        "walk out of, and a warp here can strand the map overlay. "
                        "Step out of the room before /warp.")
            if body == "in_flipper_trap":
                return ("blocked: you're in the Ghavoran Flipper Room / Spider "
                        "Magnet Elevator. If the flipper is stuck, /warp can't fix "
                        "it — the warp keeps the broken flip. LOAD YOUR LAST "
                        "CHECKPOINT from the title screen instead; that reloads the "
                        "save and resets the flipper.")
            if body == "no_interaction":
                return "blocked: cutscene/cinematic in progress — try again in a moment"
            if body != "ok":
                return f"unexpected reply: {body!r}"
            return await self._restore_after_warp(
                inv_before, recv_before, collected_before, where)
        finally:
            self._warp_in_progress = False

    async def _restore_after_warp(
        self, inv_before: dict[str, float], recv_before: int,
        collected_before: list[int], where: str = "the starting save station",
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
        return ("warped — restored {n} reverted pickup(s): {names}. Save at {where}"
                " to commit them.").format(
                    n=len(restored), names=", ".join(restored), where=where)

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
