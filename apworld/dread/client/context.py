"""DreadContext — CommonContext subclass owning AP + Switch wire.

Counterpart to smo_archipelago.client.context.SMOContext, but dramatically
simpler because:

  1. The Switch-side connection is **outbound** (we dial in to exlaunch on
     port 6969) rather than a TCP server we run. So no SwitchServer.
  2. We have no kingdoms, captures, talkatoo, multi-Switch routing,
     deathlink, or capture-lock gating to implement. Just item flow + goal.
  3. The patcher already does seed-time item placement; runtime is purely
     state synchronization.

The class structure mirrors SMOContext on purpose so the smo_archipelago
patterns (replay-on-reconnect, position-based dedup of AP items_received,
ClientStatus on goal) transfer with minimal cognitive overhead.

Skipping for v0.1: Kivy GUI (gui.py). DreadClient runs headless first; we
add Kivy once the wire flow is proven end-to-end.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

from CommonClient import CommonContext, ClientCommandProcessor
from NetUtils import ClientStatus

from .commands import parse_command
from .datapackage import DataPackage
from .lua_executor import DreadExecutor
from .lua_packets import PacketType, Response
from .protocol import (
    DreadItem,
    ReceivedItemEvent,
    CollectedLocationEvent,
    build_receive_pickup_lua,
)
from .scout_cache import ScoutCache, request_scout
from .state import BridgeState

log = logging.getLogger(__name__)


GAME_NAME = "Metroid Dread"


def _expand(path: str) -> str:
    """Expand ~ and %ENV%/$ENV references — users paste paths from their
    shell, which already does this for them. We want /patch to behave the
    same whether the user typed the env reference verbatim or shell-
    expanded it."""
    return os.path.expandvars(os.path.expanduser(path))


def _field(obj: Any, name: str, idx: int) -> Any:
    """Pluck a field from a NetworkItem-like that may be NamedTuple, dict,
    or plain (positionally-ordered) tuple/list. The AP wire layer is not
    consistent — Connected/scout flows return NamedTuples; some
    ReceivedItems handlers see plain lists post-JSON round-trip. Using
    ``getattr(...) or obj[name]`` is unsafe because (a) NamedTuples
    don't support string subscript, and (b) ``0 or X`` triggers the
    fallback for legitimate zero-valued fields (server slot id = 0)."""
    if hasattr(obj, name):
        return getattr(obj, name)
    if isinstance(obj, dict):
        return obj[name]
    return obj[idx]

# Polling cadence for the periodic Switch-state pull. 2.0s matches
# Randovania's RL.UpdateRDVClient self-scheduling interval.
POLL_INTERVAL_SECONDS = 2.0


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

    def _cmd_switch_host(self, host: str = "") -> bool:
        """Repoint the Switch IP. ``/switch_host 192.168.1.42``"""
        ctx = self.ctx
        if not host:
            self.output(f"current switch_host = {ctx.switch_host!r}")
            return True
        ctx.switch_host = host
        self.output(f"switch_host set to {host!r}; use /switch_reconnect to apply")
        return True

    def _cmd_switch_reconnect(self) -> bool:
        """Drop the current Switch connection and re-dial."""
        ctx = self.ctx
        asyncio.ensure_future(ctx.reconnect_switch())
        self.output("reconnecting to Switch …")
        return True

    def _cmd_poke(self, *lua_words: str) -> bool:
        """``/poke <lua-source>`` — run arbitrary Lua. Debug only."""
        if not lua_words:
            self.output("usage: /poke <lua-source>")
            return True
        source = " ".join(lua_words)
        asyncio.ensure_future(self.ctx._poke_lua(source))
        return True

    def _cmd_patch(self, dreadvania_dir: str = "", vanilla_romfs_dir: str = "") -> bool:
        """``/patch <dreadvania-install-dir> <vanilla-romfs-dir>`` — build
        the AP-shaped mod from this session's slot_data and write it on
        top of an existing Dreadvania install.

        Run once after connecting (and any time the seed changes). The
        Dreadvania install dir is something like
        ``%APPDATA%/Ryujinx/mods/contents/010093801237c000/DreadRandovania``;
        the vanilla romfs dir is your extracted Dread 2.1.0 romfs.
        """
        if not dreadvania_dir or not vanilla_romfs_dir:
            self.output("usage: /patch <dreadvania-install-dir> <vanilla-romfs-dir>")
            self.output(
                "  example: /patch "
                r'"%APPDATA%\Ryujinx\mods\contents\010093801237c000\DreadRandovania" '
                r'"D:\dread\romfs"'
            )
            return True
        ctx = self.ctx
        if not ctx.slot_data or "placements" not in ctx.slot_data:
            self.output(
                "err: slot_data has no placements. Are you connected? Was this seed "
                "generated with a recent DreadWorld build (one that bundles "
                "placements in fill_slot_data)?"
            )
            return True
        # Run the orchestration in a thread so the asyncio loop isn't
        # blocked by the patcher's ~3s subprocess call.
        asyncio.ensure_future(ctx._run_patch(dreadvania_dir, vanilla_romfs_dir))
        return True


class DreadContext(CommonContext):
    """Top-level glue. Connects to AP server (inherited) and to the Switch
    (via :class:`DreadExecutor`). Forwards AP items to Lua, AP server
    receives collected-checks from the periodic poll."""

    command_processor = DreadClientCommandProcessor
    game = GAME_NAME
    items_handling = 0b111  # full remote items

    def __init__(
        self,
        server_address: Optional[str],
        password: Optional[str],
        *,
        state: BridgeState,
        datapackage: DataPackage,
        switch_host: str = "127.0.0.1",
        switch_port: int = 6969,
    ):
        super().__init__(server_address, password)
        self.state = state
        self.datapackage = datapackage
        self.scout_cache = ScoutCache()
        self.switch_host = switch_host
        self.switch_port = switch_port
        self.executor: Optional[DreadExecutor] = None
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._reconnect_lock = asyncio.Lock()
        self._goal_reported = False
        # Per-slot placements payload delivered by the server in the
        # Connected packet (DreadWorld.fill_slot_data bundles it). The
        # /patch command reads from here so users don't need a local seed
        # zip to run the patcher.
        self.slot_data: dict = {}

    # ---- CommonContext overrides --------------------------------------

    async def server_auth(self, password_requested: bool = False) -> None:
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        if not self.auth:
            self.auth = self.username or "Samus"
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
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.executor:
            await self.executor.close()
        await super().shutdown()

    # ---- Switch connection lifecycle ----------------------------------

    async def connect_switch(self) -> None:
        if self.executor is not None:
            return
        self.executor = DreadExecutor(
            host=self.switch_host,
            port=self.switch_port,
            on_push=self._on_switch_push,
        )
        try:
            api = await self.executor.connect()
            self.state.set_switch_conn("connected")
            self.state.update_game_state(layout_uuid=api.layout_uuid)
            log.info("Switch connected: api=%s game=%s layout=%s",
                     api.api_version, api.game_version, api.layout_uuid)
            self._poll_task = asyncio.create_task(self._poll_loop(), name="dread-poll")
        except Exception as exc:
            log.warning("Switch dial failed: %s", exc)
            self.state.set_switch_conn(f"error: {exc}")
            self.executor = None

    async def reconnect_switch(self) -> None:
        async with self._reconnect_lock:
            if self._poll_task:
                self._poll_task.cancel()
                self._poll_task = None
            if self.executor:
                await self.executor.close()
                self.executor = None
            await self.connect_switch()

    # ---- AP-driven flows ---------------------------------------------

    async def _on_connected(self, args: dict) -> None:
        self.state.set_ap_conn("connected")
        self.state.slot = self.username or ""
        self.state.seed = args.get("seed_name", self.state.seed)
        self._goal_reported = False
        # Stash slot_data for /patch. fill_slot_data bundles the placements
        # payload (everything seed_to_patcher_overrides used to extract from
        # the seed zip).
        sd = args.get("slot_data")
        if isinstance(sd, dict):
            self.slot_data = sd
        # Phase 1.5 — once we have all_location_ids from datapackage, scout
        # them all so we know which AP item lives at each pickup before the
        # player collects it (used to compose in-game popup text).
        loc_ids = self.datapackage.all_location_ids()
        if loc_ids:
            await request_scout(self, loc_ids, cache=self.scout_cache)
        # Kick off the Switch wire if it isn't already up.
        if self.executor is None:
            await self.connect_switch()

    async def _on_received_items(self, args: dict) -> None:
        """One ``ReceivedItems`` package; dispatch any items beyond our
        position cursor into Lua."""
        index = int(args.get("index", 0))
        items = args.get("items") or []
        already = self.state.received_count()
        for offset, network_item in enumerate(items):
            pos = index + offset
            if pos < already:
                continue
            await self._deliver_item(network_item, pos)

    async def _deliver_item(self, network_item: Any, pos: int) -> None:
        """Translate one AP NetworkItem to ``RandomizerPowerup.OnPickedUp``
        (no actor) and call it."""
        # NetworkItem is a NamedTuple(item, location, player, flags), but the
        # WebSocket layer hands it back as a plain list/tuple on some paths.
        # The previous `getattr(..., None) or network_item[name]` pattern
        # broke two ways: NamedTuples don't support string-keyed subscript
        # (TypeError on lookup), and `0 or X` short-circuits to X when the
        # field's real value is zero — which happens for server-originated
        # items (player slot 0), e.g. anything sent via the AP `/send`
        # console. Handle both shapes explicitly.
        item_id = _field(network_item, "item", 0)
        sender_idx = _field(network_item, "player", 2)
        dread_item = self.datapackage.ap_id_to_dread(int(item_id))
        if dread_item is None:
            log.warning("no Dread mapping for AP item id %s; dropping", item_id)
            return
        sender = self._sender_name(int(sender_idx))
        message = f"Received {dread_item.ap_item_name} from {sender}"
        progression = [[{"item_id": dread_item.patcher_item_id,
                         "quantity": dread_item.quantity}]]
        lua = build_receive_pickup_lua(
            message=message,
            parent_ref=0,  # 0 = nil parent; Lua side falls back to a generic class
            progression=progression,
            num_pickups=pos + 1,
            inventory_index=pos,
        )
        evt = ReceivedItemEvent(item=dread_item, sender=sender,
                                inventory_index=pos)
        self.state.append_received(evt)
        if self.executor is None:
            log.info("Switch not connected; queuing item %s for replay on next dial",
                     dread_item.ap_item_name)
            return
        try:
            await self.executor.run_lua(lua)
        except Exception as exc:
            log.warning("OnPickedUp failed for %s: %s", dread_item.ap_item_name, exc)

    def _sender_name(self, slot_idx: int) -> str:
        if self.slot_info and slot_idx in self.slot_info:
            return self.slot_info[slot_idx].name
        if slot_idx == self.slot:
            return "yourself"
        return f"Player {slot_idx}"

    # ---- /patch implementation ---------------------------------------

    async def _run_patch(self, dreadvania_dir: str, vanilla_romfs_dir: str) -> None:
        """Run the patcher pipeline against the current session's slot_data.

        Runs in a worker thread to keep the asyncio loop responsive while
        the patcher CLI churns through romfs extraction (~3s typical, up
        to several seconds on cold caches)."""
        # Late import keeps the apworld importable in environments where
        # open_dread_rando isn't installed (e.g. read-only test runners).
        # patcher_pipeline.check_dependencies() handles the real reporting.
        from ..patcher_pipeline import patch

        log.info("/patch: starting…")

        def _do():
            return patch(
                placements=self.slot_data,
                dreadvania_install_dir=Path(_expand(dreadvania_dir)),
                vanilla_romfs_dir=Path(_expand(vanilla_romfs_dir)),
            )

        try:
            result = await asyncio.to_thread(_do)
        except Exception as exc:  # noqa: BLE001
            log.exception("/patch: unhandled exception: %s", exc)
            return

        if result.ok:
            log.info("/patch: %s", result.message)
            if result.patcher_input_path:
                log.info("  patcher input: %s", result.patcher_input_path)
            if result.init_lc_path:
                log.info("  injected telemetry into: %s", result.init_lc_path)
        else:
            log.error("/patch: %s", result.message)
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
                if self.executor is None:
                    return
                try:
                    await self._poll_once()
                except (ConnectionError, asyncio.TimeoutError) as exc:
                    log.warning("Switch poll failed: %s; will retry", exc)
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise

    async def _poll_once(self) -> None:
        """One poll tick.

        We don't read the collected-indices return value — the Lua side
        issues a separate ``PACKET_COLLECTED_INDICES`` push that lands on
        ``_on_switch_push`` (which then dedups + emits ``LocationChecks``).
        Our Lua-exec reply here is just the trigger; the empty-string
        return value is discarded."""
        if self.executor is None:
            return
        # Trigger the collected-indices push. Reply payload is unused
        # — the actual data arrives via the COLLECTED_INDICES push frame
        # the bootstrap Lua sends right after running this.
        await self.executor.run_lua(
            "RL.GetCollectedIndicesAndSend(); return ''"
        )
        # Direct game-state poll. The Switch's PACKET_GAME_STATE push covers
        # this too, but it only fires on scenario transitions; this explicit
        # read covers the case where the player reaches the goal while
        # already in s080_shipyard.
        state_resp = await self.executor.run_lua(
            "return tostring(Init.bBeatenSinceLastReboot)"
        )
        if state_resp.success and state_resp.payload == b"true":
            self.state.update_game_state(beaten_since_reboot=True)
            await self._maybe_report_goal()

    async def _on_switch_push(self, packet_type: PacketType, resp: Response) -> None:
        """Handle Switch-originated push frames.

        * ``COLLECTED_INDICES`` is the meat: parse the bitfield, map each
          set index to an AP location_id, dedup, and send ``LocationChecks``.
        * ``NEW_INVENTORY`` and ``GAME_STATE`` get stashed in BridgeState
          for diagnostics (the authoritative inventory comes from
          ``items_received`` on the AP server side).
        * ``LOG_MESSAGE`` and ``MALFORMED`` are surfaced as logs.
        * ``RECEIVED_PICKUPS`` is debug-only for v0.1 — the position cursor
          we keep on the PC side is what actually drives delivery dedup."""
        if packet_type == PacketType.COLLECTED_INDICES:
            await self._handle_collected_indices(resp)
            return
        if packet_type == PacketType.NEW_INVENTORY:
            self._handle_new_inventory(resp)
            return
        if packet_type == PacketType.GAME_STATE:
            await self._handle_game_state(resp)
            return
        if packet_type == PacketType.LOG_MESSAGE:
            if resp.payload:
                self.state.add_log(resp.payload.decode("utf-8", errors="replace"))
            return
        if packet_type == PacketType.MALFORMED:
            log.warning("Switch reported MALFORMED for our request (payload=%r)", resp.payload)
            return
        # RECEIVED_PICKUPS or unknown: log and skip.
        log.debug("push %s payload (%d bytes): %r",
                  packet_type.name, len(resp.payload), resp.payload[:80])

    async def _handle_collected_indices(self, resp: Response) -> None:
        """Parse a ``PACKET_COLLECTED_INDICES`` push and emit ``LocationChecks``.

        Payload shape (per upstream MercuryConnector.new_collected_locations_received):

            b"locations:" + bitfield_bytes

        where bit ``i`` of byte ``b`` (0-indexed) being set means
        ``pickup_index == b*8 + i`` has been collected. The bootstrap Lua
        dumps the FULL set on every poll tick (and every reconnect), so we
        dedupe against ``self.state`` and only forward genuinely-new
        locations to the AP server."""
        payload = resp.payload
        prefix = b"locations:"
        if not payload.startswith(prefix):
            log.warning("COLLECTED_INDICES payload lacks 'locations:' prefix: %r",
                        payload[:32])
            return
        bitfield = payload[len(prefix):]
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

    def _handle_new_inventory(self, resp: Response) -> None:
        """Parse PACKET_NEW_INVENTORY (JSON ``{"index":int,"inventory":[float...]}``).

        The semantics of `inventory` are positional — the array index
        corresponds to a particular Dread inventory slot — so without a
        slot↔name map this is diagnostic only. We stash the raw count
        keyed by stringified position so the state mirror has *something*.
        v0.2 will add the proper name lookup."""
        try:
            import json
            blob = json.loads(resp.payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            log.warning("NEW_INVENTORY JSON decode failed: %s payload=%r",
                        exc, resp.payload[:80])
            return
        inv_list = blob.get("inventory") or []
        stashed = {f"slot{i}": int(round(v)) for i, v in enumerate(inv_list)}
        self.state.set_inventory(stashed)

    async def _handle_game_state(self, resp: Response) -> None:
        """Parse PACKET_GAME_STATE (``<state>[;<beaten_bool>]``)."""
        try:
            text = resp.payload.decode("utf-8")
        except UnicodeDecodeError:
            return
        parts = text.split(";")
        scenario_id = parts[0] if parts else ""
        beaten = (len(parts) > 1 and parts[1] == "true")
        self.state.update_game_state(scenario_id=scenario_id,
                                     beaten_since_reboot=beaten)
        if beaten:
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
        if self.executor is None:
            log.warning("no Switch connection; /poke ignored")
            return
        resp = await self.executor.run_lua(source)
        log.info("poke reply: success=%s payload=%r", resp.success, resp.payload[:200])
