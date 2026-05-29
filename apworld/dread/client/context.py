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

from .commands import parse_command, parse_switch_target
from .datapackage import DataPackage
from .lua_executor import DreadExecutor
from .lua_packets import PacketType, Response, parse_received_pickups_count
from .protocol import (
    DreadItem,
    ReceivedItemEvent,
    CollectedLocationEvent,
    build_receive_pickup_lua,
)
from .scout_cache import ScoutCache, request_scout
from .state import BridgeState

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
    Archipelago launcher restarts so users don't have to repaste
    /patch_python every session.

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

# Switch auto-connect backoff bounds (seconds). The supervisor doubles from
# START up to MAX between failed dials; MAX also caps the idle re-check cadence
# while connected.
_SWITCH_BACKOFF_START = 1.0
_SWITCH_BACKOFF_MAX = 30.0


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

    def _cmd_dread_connect(self, host: str = "") -> bool:
        """``/dread_connect [ip[:port]]`` — (re)dial the Switch.

        The initial dial sometimes loses the race with Dreadvania finishing
        its own startup, so this is the recovery hatch. With no argument it
        just re-dials the current target; pass ``ip`` (optionally ``ip:port``)
        to re-point first — e.g. ``/dread_connect 192.168.1.42`` or
        ``/dread_connect localhost:6969``.
        """
        ctx = self.ctx
        if host:
            try:
                new_host, new_port = parse_switch_target(host)
            except ValueError as exc:
                self.output(f"err: {exc}")
                self.output("usage: /dread_connect [ip[:port]]")
                return True
            ctx.switch_host = new_host
            if new_port is not None:
                ctx.switch_port = new_port
        # reconnect_switch wakes the backoff supervisor (request_redial) once
        # it has re-dialed, so no explicit signal is needed here.
        asyncio.ensure_future(ctx.reconnect_switch())
        self.output(f"reconnecting to Switch at {ctx.switch_host}:{ctx.switch_port} …")
        return True

    def _cmd_switch_reconnect(self) -> bool:
        """Drop the current Switch connection and re-dial. Alias of
        ``/dread_connect`` (kept for muscle memory)."""
        return self._cmd_dread_connect()

    def _cmd_poke(self, *lua_words: str) -> bool:
        """``/poke <lua-source>`` — run arbitrary Lua. Debug only."""
        if not lua_words:
            self.output("usage: /poke <lua-source>")
            return True
        source = " ".join(lua_words)
        asyncio.ensure_future(self.ctx._poke_lua(source))
        return True

    def _cmd_patch_python(self, path: str = "") -> bool:
        """``/patch_python [<path-to-python.exe>]`` — show or set which
        Python the ``/patch`` subprocess invokes.

        With no argument, prints the current effective Python and whether
        ``open_dread_rando`` is importable from it. This is the diagnostic
        for the "open-dread-rando is not installed" error from the frozen
        Archipelago launcher: that launcher's ``sys.executable`` is
        ``ArchipelagoLauncher.exe``, which has its own bundled
        site-packages and will never see ``pip install``ed packages.

        With a path, sets a per-session override. Point it at the Python
        in the venv where you installed ``open-dread-rando``.
        """
        from ..patcher_pipeline import check_dependencies, describe_python

        ctx = self.ctx
        if path:
            expanded = _expand(path)
            if not Path(expanded).is_file():
                self.output(f"err: not a file: {expanded}")
                return True
            ctx.dreadvania_python = expanded
            cfg = _load_user_config()
            cfg["dreadvania_python"] = expanded
            _save_user_config(cfg)
            self.output(f"dreadvania_python set to {expanded!r}")
            self.output(f"  persisted to {_user_config_path()}")
        self.output(f"patcher Python: {describe_python(ctx.dreadvania_python)}")
        dep_err = check_dependencies(ctx.dreadvania_python)
        if dep_err is None:
            self.output("  open_dread_rando + mercury_engine_data_structures: OK")
        else:
            for line in dep_err.splitlines():
                self.output(f"  {line}")
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
        ctx = self.ctx
        if not ctx.slot_data or "placements" not in ctx.slot_data:
            self.output(
                "err: slot_data has no placements. Are you connected? Was this seed "
                "generated with a recent DreadWorld build (one that bundles "
                "placements in fill_slot_data)?"
            )
            return True
        if dreadvania_dir and vanilla_romfs_dir:
            # Explicit paths given (scripts / power users) — run directly.
            asyncio.ensure_future(ctx._run_patch(dreadvania_dir, vanilla_romfs_dir))
            return True
        # No (or partial) args: pop native folder pickers, pre-filled with the
        # folders used last time. Runs the orchestration in a thread so the
        # asyncio loop isn't blocked by the patcher's ~3s subprocess call.
        asyncio.ensure_future(ctx._patch_interactive())
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
        # Set by the backoff supervisor / manual reconnect to interrupt the
        # exponential-backoff sleep and dial the Switch immediately. See
        # _switch_supervisor.
        self._redial_event = asyncio.Event()
        self._goal_reported = False
        # Whether the RL.* bootstrap has been sent to the Switch this connection.
        # Until it has, RL.GetCollectedIndicesAndSend / RL.ReceivePickup / etc.
        # don't exist on the Switch side, so polling + delivery must wait.
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
        # Override for the Python the patcher subprocess invokes. Default
        # (None) means use sys.executable, which is correct from a real
        # Python but WRONG inside the frozen Archipelago launcher — that
        # binary's bundled site-packages never sees `pip install`. Set via
        # the /patch_python command; persisted in _user_config_path() so
        # the setting survives Archipelago launcher restarts.
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
        if self.executor:
            await self.executor.close()
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

    async def connect_switch(self) -> None:
        if self.executor is not None:
            return
        self.executor = DreadExecutor(
            host=self.switch_host,
            port=self.switch_port,
            on_push=self._on_switch_push,
        )
        try:
            self._bootstrapped = False
            api = await self.executor.connect()
            self.state.update_game_state(layout_uuid=api.layout_uuid)
            # The exlaunch ROM only ships RL.* stubs; the real query/delivery
            # functions are Lua we must send every connect (matching
            # randovania's dread_executor.bootstrap). Until this lands, nothing
            # else on the wire works.
            await self._send_bootstrap(api.buffer_size)
            self._bootstrapped = True
            self.state.set_switch_conn("connected")
            # Remember the host that actually worked so the next session dials
            # it without the user re-typing (default is Ryujinx loopback).
            self._remember_switch_target()
            log.info("Switch connected + bootstrapped: api=%s game=%s layout=%s",
                     api.api_version, api.game_version, api.layout_uuid)
            self._poll_task = asyncio.create_task(self._poll_loop(), name="dread-poll")
        except Exception as exc:
            log.warning("Switch dial/bootstrap failed: %s", exc)
            self.state.set_switch_conn(f"error: {exc}")
            if self.executor is not None:
                await self.executor.close()
            self.executor = None

    async def _send_bootstrap(self, buffer_size: int) -> None:
        """Send the vendored RL.* bootstrap, chunked to the negotiated buffer
        size. Raises if the Switch reports a Lua error for any chunk — a failed
        bootstrap means the rest of the protocol can't run, so surfacing it is
        better than limping on with half the namespace defined."""
        assert self.executor is not None
        from .bootstrap import load_bootstrap_code, chunk_lua_blocks

        blocks = load_bootstrap_code()
        chunks = chunk_lua_blocks(blocks, buffer_size)
        log.info("Sending RL bootstrap: %d blocks in %d chunk(s)", len(blocks), len(chunks))
        for i, chunk in enumerate(chunks):
            resp = await self.executor.run_lua(chunk)
            if not resp.success:
                raise RuntimeError(
                    f"bootstrap chunk {i + 1}/{len(chunks)} failed: "
                    f"{resp.payload.decode('utf-8', 'replace')[:200]}"
                )

    async def reconnect_switch(self) -> None:
        async with self._reconnect_lock:
            if self._poll_task:
                self._poll_task.cancel()
                self._poll_task = None
            if self.executor:
                await self.executor.close()
                self.executor = None
            await self.connect_switch()
        # Wake the supervisor so it resets its backoff and resumes watching
        # (also covers the case where this manual dial failed).
        self.request_redial()

    def request_redial(self) -> None:
        """Signal the backoff supervisor to dial the Switch immediately.

        Both ``/dread_connect`` and the GUI reconnect popup go through
        ``reconnect_switch``, which calls this so a manual retry never has to
        wait out the current backoff sleep."""
        self._redial_event.set()

    def _remember_switch_target(self) -> None:
        """Persist the current Switch host/port so the next session reuses it."""
        cfg = _load_user_config()
        cfg["switch_host"] = self.switch_host
        cfg["switch_port"] = self.switch_port
        _save_user_config(cfg)

    async def _switch_supervisor(self) -> None:
        """Keep a Switch connection up, retrying with exponential backoff.

        The initial dial often loses the race with Dreadvania finishing its own
        boot; rather than make the user hit /dread_connect, we retry 1, 2, 4 …
        up to 30s. A manual reconnect (``request_redial``) interrupts the sleep
        and resets the backoff. While connected we idle until the wire drops
        (``executor`` cleared by a failed reconnect or close), then resume."""
        backoff = _SWITCH_BACKOFF_START
        while not self.exit_event.is_set():
            if self.executor is None:
                # connect_switch is a no-op if already connected and is
                # serialized against manual reconnects via _reconnect_lock.
                await self.connect_switch()
                if self.executor is not None:
                    backoff = _SWITCH_BACKOFF_START
                    continue
                wait = backoff
                backoff = min(backoff * 2, _SWITCH_BACKOFF_MAX)
            else:
                wait = _SWITCH_BACKOFF_MAX
            self._redial_event.clear()
            try:
                await asyncio.wait_for(self._redial_event.wait(), timeout=wait)
                # Woken by a manual redial — dial now with a fresh backoff.
                backoff = _SWITCH_BACKOFF_START
            except asyncio.TimeoutError:
                pass

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
        if self.executor is None or not self._bootstrapped:
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
            await self.executor.run_lua(lua)
        except (ConnectionError, asyncio.TimeoutError) as exc:
            log.warning("ReceivePickup send failed for %s: %s; will retry",
                        dread_item.ap_item_name, exc)

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
        """One poll tick. Each ``RL.Get*AndSend`` triggers a push that lands on
        ``_on_switch_push``; the reply payloads here are just triggers.

        We fetch inventory FIRST (so ``InventoryIndex`` is fresh before the
        received-pickups push drives a delivery), then collected indices, then
        received pickups. All three feed delivery/checks; none is optional."""
        if self.executor is None or not self._bootstrapped:
            return
        await self.executor.run_lua("RL.GetInventoryAndSend(); return ''")
        await self.executor.run_lua("RL.GetCollectedIndicesAndSend(); return ''")
        await self.executor.run_lua("RL.GetReceivedPickupsAndSend(); return ''")
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
        if self.executor is None:
            log.warning("no Switch connection; /poke ignored")
            return
        resp = await self.executor.run_lua(source)
        log.info("poke reply: success=%s payload=%r", resp.success, resp.payload[:200])
