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
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from CommonClient import CommonContext, ClientCommandProcessor
from NetUtils import ClientStatus

from .commands import parse_command
from .datapackage import DataPackage
from .discovery import DEFAULT_DISCOVERY_PORT, DiscoveryResponder
from .lua_executor import DreadConnection
from .lua_packets import PacketType, Response, parse_received_pickups_count
from .protocol import (
    DreadItem,
    ReceivedItemEvent,
    CollectedLocationEvent,
    build_receive_pickup_lua,
)
from .scout_cache import ScoutCache, request_scout
from .state import BridgeState
from .switch_server import SwitchServer
from .._setup import setup_state_path
from .._setup.deploy import DREAD_TITLE_ID, RYU_MOD_NAME

DEFAULT_LISTEN_PORT = 17777

log = logging.getLogger(__name__)

# Parent package logger ("<pkg>.client") that the GUI log pane tails; the
# per-module loggers (context, lua_executor, …) propagate into it. Derived
# from __name__ so it's right whether installed as worlds.dread.* (AP folder
# install) or apworld.dread.* (tests / dev). The GUI computes the same name.
_CLIENT_LOGGER = __name__.rpartition(".")[0]
# Switch-forwarded device logs land here (a child of _CLIENT_LOGGER) so they
# appear in the GUI log pane, tagged distinct from PC-side diagnostics.
_switch_log = logging.getLogger(f"{_CLIENT_LOGGER}.switch")
# The "Client" logger is what the GUI maps to the **Archipelago** tab
# (DreadManager.logging_pairs = [("Client", "Archipelago")]). User-facing
# setup guidance (e.g. the exact `pip install` command) goes here so it lands
# in the tab AP users watch by default.
_ap_log = logging.getLogger("Client")


GAME_NAME = "Metroid Dread"


def _expand(path: str) -> str:
    """Expand ~ and %ENV%/$ENV references — users paste paths from their
    shell, which already does this for them. We want /patch to behave the
    same whether the user typed the env reference verbatim or shell-
    expanded it."""
    return os.path.expandvars(os.path.expanduser(path))


def _user_config_path() -> Path:
    """Per-user config location for the Dread client. Survives across
    Archipelago launcher restarts so the auto-detected patcher Python
    (recorded by ``_ensure_patcher_python``) is reused across sessions
    without re-probing every connect.

    Windows: ``%APPDATA%\\dread_ap\\config.json``.
    Other:   ``~/.config/dread_ap/config.json`` (XDG-ish; this client is
    Windows-targeted in practice but the AP launcher is cross-platform)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "dread_ap" / "config.json"


def _load_user_config() -> dict:
    """Best-effort load. Missing/corrupt file → empty dict; we never want
    a bad config to block client startup."""
    path = _user_config_path()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable %s: %s", path, exc)
        return {}


def _save_user_config(cfg: dict) -> None:
    """Best-effort save. Logs and swallows OS errors — persistence is a
    convenience, never a correctness requirement."""
    path = _user_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("could not persist %s: %s", path, exc)


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

    def _cmd_dread_connect(self) -> bool:
        """``/dread_connect`` — force-close the active Switch connection so
        the sysmodule re-dials.

        Under the inverted topology the Switch dials in via UDP discovery
        + TCP connect; the PC never picks an IP. This is the recovery
        hatch for a half-open / stale connection — close the writer, the
        Switch redials within its reconnect cadence.
        """
        ctx = self.ctx
        asyncio.ensure_future(ctx.disconnect_active_switch())
        self.output("dropped active Switch connection; sysmodule will redial …")
        return True

    def _cmd_switch_reconnect(self) -> bool:
        """Alias of ``/dread_connect`` (kept for muscle memory)."""
        return self._cmd_dread_connect()

    def _cmd_poke(self, *lua_words: str) -> bool:
        """``/poke <lua-source>`` — run arbitrary Lua. Debug only."""
        if not lua_words:
            self.output("usage: /poke <lua-source>")
            return True
        source = " ".join(lua_words)
        asyncio.ensure_future(self.ctx._poke_lua(source))
        return True

    def _cmd_patch(self, *args) -> bool:
        """``/patch`` is deprecated — the romfs patcher now runs
        automatically on AP-connect.

        Run ``/setup`` once per machine to open the wizard — it walks
        you through prereqs, vanilla romfs picker, sysmodule build, and
        deploy target. Every subsequent seed re-patches on connect — no
        /patch typing needed.
        """
        del args  # accepted-and-ignored so old aliases don't error
        self.output(
            "/patch is deprecated. The romfs patcher now runs "
            "automatically on AP-connect once /setup has recorded your "
            "paths. Run /setup once per machine to open the wizard; "
            "subsequent seeds patch on connect."
        )
        return True

    def _cmd_setup(self, *_args: str) -> bool:
        """``/setup`` — open the Kivy setup wizard in a new window.

        Covers first-time setup and re-runs alike: prereq install
        (devkitPro / Python 3.12 / open-dread-rando), vanilla romfs
        picker, exlaunch sysmodule build, and deploy-target picker
        (Ryujinx / SD card / custom folder). Spawns the wizard as a
        subprocess so DreadClient stays open while it runs — close it
        any time; DreadClient is unaffected.

        Mirrors smo_archipelago's ``/setup`` pattern: the slash command
        delegates entirely to the wizard, which is the single canonical
        UI for build + deploy. Earlier behavior (``/setup [ryujinx|sd]``
        as an inline one-shot build pipeline) is gone — the wizard's
        DeployPage handles target selection with auto-detection. The
        positional arg is accepted-and-ignored so old muscle memory
        doesn't error.
        """
        from worlds.LauncherComponents import launch_subprocess
        # Defer to a module-level callable so the subprocess-pickling
        # path in launch_subprocess can reach it by qualified name.
        # ``_run_setup_wizard_no_dreadap`` is the only sanctioned entry
        # point exported by the apworld root __init__ for the wizard.
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
    (via :class:`DreadExecutor`). Forwards AP items to Lua, AP server
    receives collected-checks from the periodic poll."""

    command_processor = DreadClientCommandProcessor
    game = GAME_NAME
    # Receive ONLY items found in OTHER players' worlds (bit 0). Dread's own
    # items and the starting inventory are baked into the patched ROM by
    # open-dread-rando (real resources per pedestal + starting_items), so the
    # game grants them locally. Setting bit 1 (own-world items) or bit 2
    # (starting inventory) would make the server re-send those too, double-
    # granting them — exactly the "starting Charge Beam re-delivered as a popup"
    # symptom. Only cross-world items flow through RL.ReceivePickup.
    items_handling = 0b001

    def __init__(
        self,
        server_address: Optional[str],
        password: Optional[str],
        *,
        state: BridgeState,
        datapackage: DataPackage,
        listen_host: str = "0.0.0.0",
        listen_port: int = DEFAULT_LISTEN_PORT,
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
    ):
        super().__init__(server_address, password)
        self.state = state
        self.datapackage = datapackage
        self.scout_cache = ScoutCache()
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.discovery_port = discovery_port
        # Currently-active accepted Switch connection (one at a time —
        # newer accepts supersede). None when no Switch is attached.
        self._active_conn: Optional[DreadConnection] = None
        # Informational: source IP:port of the active connection, surfaced
        # in the GUI status pill / reconnect popup. None when disconnected.
        self.connected_peer: Optional[str] = None
        self._switch_server: Optional[SwitchServer] = None
        self._discovery: Optional[DiscoveryResponder] = None
        self._poll_task: Optional[asyncio.Task[None]] = None
        self._goal_reported = False
        # Whether the RL.* bootstrap has been sent to the Switch this
        # connection. Until it has, RL.GetCollectedIndicesAndSend /
        # RL.ReceivePickup / etc. don't exist on the Switch side, so
        # polling + delivery must wait.
        self._bootstrapped = False
        # Full ordered AP items_received list (indexed by AP receive position).
        # Delivery sends the item at position == the game's ReceivedPickups
        # count; we never advance a local cursor on send. See _attempt_delivery.
        self._ap_items: list[Any] = []
        # Per-slot placements payload delivered by the server in the
        # Connected packet (DreadWorld.fill_slot_data bundles it). The
        # /patch command reads from here so users don't need a local seed
        # zip to run the patcher.
        self.slot_data: dict = {}
        # Cached path to the external Python the per-seed romfs patcher
        # subprocess invokes. Auto-detected by ``_ensure_patcher_python``
        # via ``patcher_pipeline.autodetect_patcher_python`` and persisted
        # in ``_user_config_path()`` so the resolved interpreter survives
        # across Archipelago launcher restarts. ``sys.executable`` is
        # explicitly NOT used — inside the frozen Archipelago launcher
        # the bundled site-packages can't ``pip install``, so we always
        # shell out to an external interpreter the user already has set
        # up. The setup wizard's prereqs page surfaces the same probe.
        self.dreadvania_python: Optional[str] = _load_user_config().get(
            "dreadvania_python"
        )
        # Human-readable result of the last patcher-Python autodetect; mirrored
        # to the GUI panel + logged to the Archipelago tab. See
        # _ensure_patcher_python.
        self.patcher_python_status: str = ""

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
        if self._discovery is not None:
            self._discovery.stop()
            self._discovery = None
        if self._switch_server is not None:
            await self._switch_server.stop()
            self._switch_server = None
        await super().shutdown()

    def run_gui(self) -> None:
        """Lazy-import + start the Kivy UI.

        gui.py pulls kvui/Kivy, which crashes on headless generation hosts
        (no display server). The apworld __init__ is imported at generation
        time, so we must never import gui.py at module load — defer it to
        here, which only runs inside ``launch()`` from the Launcher
        subprocess. Mirrors ``SMOContext.run_gui``."""
        from .gui import DreadManager
        self.ui = DreadManager(self)
        self.ui_task = asyncio.create_task(self.ui.async_run(), name="DreadUI")

    # ---- Switch connection lifecycle ----------------------------------

    async def start_switch_listener(self) -> None:
        """Start the UDP discovery responder + TCP listener.

        Called once from ``main.py``. The Switch sysmodule UDP-probes for
        the responder, then TCP-connects to the advertised listener; the
        per-connection lifetime runs inside ``_on_switch_connection``.

        No exponential-backoff loop — the Switch dials in when ready, and
        TCP keepalive on accepted sockets (see ``switch_server``) surfaces
        a dead wire so the Switch redials within ~20 s. Discovery /
        listener failures are logged and the client keeps running so the
        user can re-run /setup or investigate.
        """
        if self._switch_server is not None:
            return
        self.state.set_switch_conn("listening")
        self._switch_server = SwitchServer(
            host=self.listen_host,
            port=self.listen_port,
            on_connection=self._on_switch_connection,
        )
        try:
            await self._switch_server.start()
        except OSError as exc:
            log.error("could not bind TCP listener on %s:%d: %s",
                      self.listen_host, self.listen_port, exc)
            self.state.set_switch_conn(f"listen failed: {exc}")
            self._switch_server = None
            return
        # Advertise the actual bound port (matters when listen_port=0).
        self.listen_port = self._switch_server.actual_port
        self._discovery = DiscoveryResponder(
            tcp_port=self.listen_port,
            bind_host=self.listen_host,
            port=self.discovery_port,
        )
        await self._discovery.start()
        # Mirror the discovery port back to the context so tests / GUI see
        # the actual bound value when discovery_port=0 was requested.
        self.discovery_port = self._discovery.actual_port
        log.info("Switch listener ready: TCP %s:%d, UDP discovery :%d — "
                 "waiting for sysmodule to dial in",
                 self.listen_host, self.listen_port, self.discovery_port)

    async def _on_switch_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: str,
    ) -> None:
        """Handle one accepted Switch connection start-to-finish.

        Runs for the lifetime of the TCP socket: handshake → API probe →
        ``RL.*`` bootstrap → poll loop start → block on the read loop →
        clean up. Returning unwinds in ``SwitchServer._handle_client``.
        """
        conn = DreadConnection(reader, writer, on_push=self._on_switch_push)
        self._active_conn = conn
        self.connected_peer = peer
        self._bootstrapped = False
        self.state.set_switch_conn("connecting")
        try:
            api = await conn.setup()
            self.state.update_game_state(layout_uuid=api.layout_uuid)
            # The exlaunch ROM only ships RL.* stubs; the real query / delivery
            # functions are Lua we install every connect (matching randovania's
            # dread_executor.bootstrap). Until this lands, nothing else on the
            # wire works. The bootstrap RE-RUNS on every accept because the
            # Switch may have reconnected with a fresh Lua state.
            await self._send_bootstrap(conn, api.buffer_size)
            self._bootstrapped = True
            self.state.set_switch_conn("connected")
            log.info(
                "Switch connected + bootstrapped from %s: "
                "api=%s game=%s layout=%s",
                peer, api.api_version, api.game_version, api.layout_uuid,
            )
            self._poll_task = asyncio.create_task(
                self._poll_loop(), name="dread-poll")
            try:
                await conn.run_until_disconnect()
            finally:
                if self._poll_task is not None:
                    self._poll_task.cancel()
                    self._poll_task = None
        except Exception as exc:
            log.warning("Switch session ended with error: %s", exc)
            self.state.set_switch_conn(f"error: {exc}")
        finally:
            await conn.close()
            # Only mutate state if we're still the active connection — a
            # supersede may have already replaced us.
            if self._active_conn is conn:
                self._active_conn = None
                self.connected_peer = None
                self._bootstrapped = False
                self.state.set_switch_conn("listening")

    async def _send_bootstrap(self, conn: DreadConnection, buffer_size: int) -> None:
        """Send the vendored RL.* bootstrap, chunked to the negotiated buffer
        size. Raises if the Switch reports a Lua error for any chunk — a failed
        bootstrap means the rest of the protocol can't run, so surfacing it is
        better than limping on with half the namespace defined."""
        from .bootstrap import load_bootstrap_code, chunk_lua_blocks

        blocks = load_bootstrap_code()
        chunks = chunk_lua_blocks(blocks, buffer_size)
        log.info("Sending RL bootstrap: %d blocks in %d chunk(s)", len(blocks), len(chunks))
        for i, chunk in enumerate(chunks):
            resp = await conn.run_lua(chunk)
            if not resp.success:
                raise RuntimeError(
                    f"bootstrap chunk {i + 1}/{len(chunks)} failed: "
                    f"{resp.payload.decode('utf-8', 'replace')[:200]}"
                )

    async def disconnect_active_switch(self) -> None:
        """Force-close the active Switch connection so the sysmodule redials.

        Wired to ``/dread_connect`` / ``/switch_reconnect`` and the GUI's
        Disconnect button. No-op if nothing is attached.
        """
        if self._switch_server is None:
            return
        await self._switch_server.disconnect_active()

    async def _ensure_patcher_python(self) -> None:
        """Ensure a usable patcher Python is configured and tell the user.

        Keeps a previously-saved interpreter if its deps still import; else
        auto-detects one (persisting it on success). The actionable result —
        an OK line or the exact ``pip install`` command — is logged to the
        Archipelago tab. Dep checks shell out, so they run off the loop."""
        from ..patcher_pipeline import autodetect_patcher_python, check_dependencies

        def _resolve() -> tuple[Optional[str], str]:
            configured = self.dreadvania_python
            if configured and check_dependencies(configured) is None:
                return configured, f"patcher Python OK: {configured}"
            return autodetect_patcher_python()

        path, message = await asyncio.to_thread(_resolve)
        self.patcher_python_status = message
        if path:
            if path != self.dreadvania_python:
                self.dreadvania_python = path
                cfg = _load_user_config()
                cfg["dreadvania_python"] = path
                _save_user_config(cfg)
            self.state.set_patcher_python(f"ready ({Path(path).name})")
            _ap_log.info(message)
        else:
            self.state.set_patcher_python("not installed — see Archipelago tab")
            _ap_log.warning("Patcher setup needed for /patch:")
            for line in message.splitlines():
                _ap_log.warning("  %s", line)

    # ---- AP-driven flows ---------------------------------------------

    async def _on_connected(self, args: dict) -> None:
        self.state.set_ap_conn("connected")
        self.state.slot = self.username or ""
        self.state.seed = args.get("seed_name", self.state.seed)
        self._goal_reported = False
        # Fresh AP connection: AP resends ReceivedItems from index 0, so rebuild
        # the ordered list from scratch. Delivery keys off the game's counter,
        # not this list's length, so a rebuild never re-grants.
        self._ap_items = []
        # Stash slot_data for /patch. fill_slot_data bundles the placements
        # payload (everything seed_to_patcher_overrides used to extract from
        # the seed zip).
        sd = args.get("slot_data")
        if isinstance(sd, dict):
            self.slot_data = sd
        # Auto-run the per-seed romfs patcher if /setup recorded the romfs
        # path and a deploy target. Schedule it rather than await — the
        # ~3s patcher subprocess shouldn't delay the rest of the connect
        # handshake. _maybe_auto_patch logs a clear actionable line if the
        # setup state is missing or stale, so the AP session is never
        # blocked nor crashed by a misconfiguration.
        asyncio.ensure_future(self._maybe_auto_patch())
        # Phase 1.5 — once we have all_location_ids from datapackage, scout
        # them all so we know which AP item lives at each pickup before the
        # player collects it (used to compose in-game popup text).
        loc_ids = self.datapackage.all_location_ids()
        if loc_ids:
            await request_scout(self, loc_ids, cache=self.scout_cache)
        # No dial-on-AP-connect — the Switch sysmodule dials in via UDP
        # discovery whenever it's ready (handled by `start_switch_listener`
        # called once from main).

    async def _on_received_items(self, args: dict) -> None:
        """Absorb a ``ReceivedItems`` package into the ordered AP-items list,
        then attempt delivery. We place items at their absolute positions
        (``index + offset``) so the list mirrors AP's authoritative ordering;
        delivery decides what to send based on the *game's* counter, not this
        list, so re-absorbing the same items (reconnect resend) is harmless."""
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

        The game's ``ReceivedPickups`` count is the cursor: we deliver the AP
        item at that position, tagged with the game's current ``InventoryIndex``.
        ``RL.ReceivePickup`` accepts it only if both indices still match, guards
        against a second in-flight pickup, defers through cutscenes, and bumps
        ``ReceivedPickups`` on confirm. The resulting push re-enters here and
        clocks the next one. So we send exactly one per call and never advance a
        local cursor — making reconnect/restart and mid-cutscene delivery safe
        by construction (CLAUDE.md risk #1)."""
        conn = self._active_conn
        if conn is None or not self._bootstrapped:
            return
        received = self.state.game_received_pickups()
        target = len(self._ap_items)
        if received >= target:
            return
        network_item = self._ap_items[received]
        if network_item is None:
            # Gap in the list (out-of-order absorb) — wait for it to fill.
            return
        dread_item, sender = self._resolve_item(network_item)
        if dread_item is None:
            # Can't deliver AND can't skip — skipping would desync the contiguous
            # received-pickup index the game enforces. Stall loudly instead.
            log.error("no Dread mapping for AP item id %s at received index %d; "
                      "delivery stalled", _field(network_item, "item", 0), received)
            return
        message = f"Received {dread_item.ap_item_name} from {sender}"
        progression = [[{"item_id": dread_item.patcher_item_id,
                         "quantity": dread_item.quantity}]]
        lua = build_receive_pickup_lua(
            message=message,
            progression=progression,
            received_pickup_index=received,
            inventory_index=self.state.game_inventory_index(),
        )
        try:
            await conn.run_lua(lua)
        except (ConnectionError, asyncio.TimeoutError, OSError, RuntimeError) as exc:
            log.warning("ReceivePickup send failed for %s: %s; will retry",
                        dread_item.ap_item_name, exc)

    def _sender_name(self, slot_idx: int) -> str:
        if self.slot_info and slot_idx in self.slot_info:
            return self.slot_info[slot_idx].name
        if slot_idx == self.slot:
            return "yourself"
        return f"Player {slot_idx}"

    # ---- /setup auto-run hookup --------------------------------------

    def _dreadvania_install_dir_from_state(self, state: dict) -> Optional[Path]:
        """Compute the dreadvania mod-install dir (where the patcher writes
        romfs overlays) from the /setup wizard's persisted state.

        Mirrors the layouts in `_setup.deploy`:
          - ryujinx -> ``<ryu>/mods/contents/<title>/DreadRandovania``
          - sd      -> ``<sd>/atmosphere/contents/<title>``
          - custom  -> ``<custom>/atmosphere/contents/<title>``

        Returns ``None`` if ``deploy_target`` is unset/unknown, or the
        corresponding root key is missing/empty in the state. The caller
        logs a clear remediation hint rather than crashing.
        """
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
        """Run the per-seed romfs patcher if /setup has recorded the paths.

        Hand-off contract: when setup_state.json is missing, the romfs path
        is stale, the deploy dir can't be computed, or slot_data has no
        placements, log a clear actionable message on the Archipelago tab
        and return. We never raise — a misconfiguration must not crash or
        even slow the AP connect path.

        When everything lines up, schedule ``_run_patch`` via
        ``asyncio.ensure_future`` so the patcher's ~3s subprocess overlaps
        with the rest of the connect handshake (scout absorb, Switch dial).
        """
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
                "/setup to pick your extracted Dread 2.1.0 romfs folder.",
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
        if not self.slot_data or "placements" not in self.slot_data:
            _ap_log.info(
                "Auto-patch skipped: slot_data has no placements. Older "
                "seeds (generated before fill_slot_data bundled placements) "
                "need the /patch command run manually with the seed zip.",
            )
            return

        _ap_log.info(
            "Auto-patch: writing per-seed romfs overlay to %s (vanilla "
            "romfs: %s)",
            deploy_dir, romfs_path,
        )
        asyncio.ensure_future(
            self._run_patch(str(deploy_dir), str(romfs_path))
        )

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
                python_executable=self.dreadvania_python,
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
        else:
            log.error("/patch: %s", result.message)
            if result.cli_stderr_tail:
                for line in result.cli_stderr_tail.splitlines():
                    log.error("  | %s", line)

    async def _patch_interactive(self) -> None:
        """``/patch`` with no args: pop native folder pickers (pre-filled with
        the last-used folders) for the Dreadvania install dir and the vanilla
        romfs dir, remember the choices, then run the patch.

        Falls back to printing the text-arg usage if no native dialog backend
        is available (e.g. a tkinter-less frozen launcher)."""
        from .filedialog import ask_directory, FileDialogUnavailable

        cfg = _load_user_config()
        dv_init = cfg.get("dreadvania_dir")
        if not dv_init:
            guess = _expand(
                r"%APPDATA%\Ryujinx\mods\contents\010093801237c000\DreadRandovania"
            )
            if Path(guess).is_dir():
                dv_init = guess
        romfs_init = cfg.get("vanilla_romfs_dir")

        try:
            dreadvania_dir = await asyncio.to_thread(
                ask_directory, "Select your Dreadvania mod install folder", dv_init)
            if not dreadvania_dir:
                log.info("/patch: cancelled (no Dreadvania folder chosen)")
                return
            vanilla_romfs_dir = await asyncio.to_thread(
                ask_directory, "Select your extracted Dread 2.1.0 romfs folder",
                romfs_init)
            if not vanilla_romfs_dir:
                log.info("/patch: cancelled (no romfs folder chosen)")
                return
        except FileDialogUnavailable as exc:
            log.warning(
                "/patch: no folder picker available (%s). Pass the paths "
                "directly:\n  /patch <dreadvania-install-dir> <vanilla-romfs-dir>",
                exc)
            return

        cfg["dreadvania_dir"] = dreadvania_dir
        cfg["vanilla_romfs_dir"] = vanilla_romfs_dir
        _save_user_config(cfg)
        await self._run_patch(dreadvania_dir, vanilla_romfs_dir)

    # ---- Switch poll loop --------------------------------------------

    async def _poll_loop(self) -> None:
        """Every POLL_INTERVAL_SECONDS, ask the Switch for collected
        locations + game state, and forward to AP."""
        try:
            while True:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                if self._active_conn is None:
                    return
                try:
                    await self._poll_once()
                except (ConnectionError, asyncio.TimeoutError, OSError, RuntimeError) as exc:
                    log.warning("Switch poll failed: %s; will retry", exc)
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise

    async def _poll_once(self) -> None:
        """One poll tick. Each ``RL.Get*AndSend`` triggers a push that lands on
        ``_on_switch_push``; the reply payloads here are just triggers.

        We fetch inventory FIRST (so ``InventoryIndex`` is fresh before the
        received-pickups push drives a delivery), then collected indices, then
        received pickups. All three feed delivery/checks; none is optional."""
        conn = self._active_conn
        if conn is None or not self._bootstrapped:
            return
        await conn.run_lua("RL.GetInventoryAndSend(); return ''")
        await conn.run_lua("RL.GetCollectedIndicesAndSend(); return ''")
        await conn.run_lua("RL.GetReceivedPickupsAndSend(); return ''")
        # Direct game-state poll. The Switch's PACKET_GAME_STATE push covers
        # this too, but it only fires on scenario transitions; this explicit
        # read covers the case where the player reaches the goal while
        # already in s080_shipyard.
        state_resp = await conn.run_lua(
            "return tostring(Init.bBeatenSinceLastReboot)"
        )
        if state_resp.success and state_resp.payload == b"true":
            self.state.update_game_state(beaten_since_reboot=True)
            await self._maybe_report_goal()
        # Belt-and-suspenders: drive delivery even if the received-pickups push
        # raced the reply ordering this tick.
        await self._attempt_delivery()

    async def _on_switch_push(self, packet_type: PacketType, resp: Response) -> None:
        """Handle Switch-originated push frames.

        * ``COLLECTED_INDICES`` is the meat: parse the bitfield, map each
          set index to an AP location_id, dedup, and send ``LocationChecks``.
        * ``NEW_INVENTORY`` and ``GAME_STATE`` get stashed in BridgeState
          for diagnostics (the authoritative inventory comes from
          ``items_received`` on the AP server side).
        * ``LOG_MESSAGE`` and ``MALFORMED`` are surfaced as logs.
        * ``RECEIVED_PICKUPS`` carries the game's ``ReceivedPickups`` count —
          the delivery cursor. Updating it drives the next ``RL.ReceivePickup``.
        * ``NEW_INVENTORY`` carries ``InventoryIndex`` (the other half of the
          delivery index match) plus a diagnostic item snapshot."""
        if packet_type == PacketType.COLLECTED_INDICES:
            await self._handle_collected_indices(resp)
            return
        if packet_type == PacketType.NEW_INVENTORY:
            self._handle_new_inventory(resp)
            return
        if packet_type == PacketType.RECEIVED_PICKUPS:
            await self._handle_received_pickups(resp)
            return
        if packet_type == PacketType.GAME_STATE:
            await self._handle_game_state(resp)
            return
        if packet_type == PacketType.LOG_MESSAGE:
            if resp.payload:
                text = resp.payload.decode("utf-8", errors="replace")
                self.state.add_log(text)
                # Surface device-side logs in the GUI log pane (which tails
                # the _CLIENT_LOGGER tree). "[switch]" tags them apart from
                # PC-side client diagnostics.
                _switch_log.info("[switch] %s", text)
            return
        if packet_type == PacketType.MALFORMED:
            log.warning("Switch reported MALFORMED for our request (payload=%r)", resp.payload)
            return
        # Unknown push type: log and skip.
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

    async def _handle_received_pickups(self, resp: Response) -> None:
        """Record the game's ``Blackboard.ReceivedPickups`` count (the delivery
        cursor) and log newly-confirmed items into the diagnostics mirror.

        This runs ON the read loop, so it must NOT call ``run_lua`` — doing so
        would await a reply that only the read loop can read, deadlocking until
        timeout. The actual ``RL.ReceivePickup`` send is driven from the poll
        task and the AP-message task (``_attempt_delivery``); the next poll
        picks up this advanced count and clocks the next delivery."""
        count = parse_received_pickups_count(resp.payload)
        if count is None:
            log.debug("RECEIVED_PICKUPS payload not an integer: %r", resp.payload[:32])
            return
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
        if count < previous:
            # Regression — drive a delivery now so we don't wait up to one
            # poll for the inventory-index push + next tick. Lua's index match
            # silently drops anything the player still has.
            await self._attempt_delivery()

    def _resolve_item(self, network_item: Any) -> tuple[Optional[DreadItem], str]:
        """Map an AP NetworkItem to its DreadItem + sender display name.
        Returns ``(None, "")`` if the item id has no Dread mapping."""
        item_id = _field(network_item, "item", 0)
        sender_idx = _field(network_item, "player", 2)
        dread_item = self.datapackage.ap_id_to_dread(int(item_id))
        if dread_item is None:
            return None, ""
        return dread_item, self._sender_name(int(sender_idx))

    def _handle_new_inventory(self, resp: Response) -> None:
        """Parse PACKET_NEW_INVENTORY (JSON ``{"index":int,"inventory":[float...]}``).

        ``index`` is the game's ``InventoryIndex`` — half of the delivery index
        match, so we record it. The ``inventory`` array is positional (no
        slot↔name map yet) and stashed only for diagnostics."""
        try:
            import json
            blob = json.loads(resp.payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            log.warning("NEW_INVENTORY JSON decode failed: %s payload=%r",
                        exc, resp.payload[:80])
            return
        index = blob.get("index")
        if isinstance(index, (int, float)):
            self.state.set_game_inventory_index(int(index))
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
        conn = self._active_conn
        if conn is None:
            log.warning("no Switch connection; /poke ignored")
            return
        resp = await conn.run_lua(source)
        log.info("poke reply: success=%s payload=%r", resp.success, resp.payload[:200])
