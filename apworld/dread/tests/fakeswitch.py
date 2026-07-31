"""A stateful fake Metroid Dread game that speaks the line-delimited JSON wire.

The integration-test fixture between unit tests and a real Switch. Unlike a
wire-only fake, this models **game semantics** so the real :class:`DreadContext`
can be driven through a whole session against it over a loopback socket.

Topology-inverted vs the old binary-wire fake: the fake is now a TCP **client**
that dials in to a running :class:`BridgeServer` on ``localhost:17777``,
sends a JSON HELLO, and reacts to ``lua_exec`` requests by sending back the
appropriate JSON pushes (``collected``, ``received_pickups``, ``inventory``,
``game_state``) plus a ``lua_exec_reply``.

It models the *real* delivery protocol read from upstream Lua
(randovania ``bootstrap_part_2.lua`` + open-dread-rando ``randomizer_powerup.lua``):

  * Two counters — ``ReceivedPickups`` (confirmed AP deliveries) and
    ``InventoryIndex`` (every pickup, local or remote).
  * ``RL.ReceivePickup(msg, cls, prog, receivedPickupIndex, inventoryIndex)``
    grants ONLY when no pickup is pending AND both indices match the live
    counters; defers through cutscenes; on confirm applies resources
    (``OnPickedUp`` → ``InventoryIndex += 1``) and ``ReceivedPickups += 1``.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from dread.client import wire as W  # noqa: E402

_RESOURCE_RE = re.compile(r'item_id\s*=\s*"([^"]+)"\s*,\s*quantity\s*=\s*(-?\d+)')

_RECEIVE_RE = re.compile(
    r'RL\.ReceivePickup\(\s*'
    r'("(?:[^"\\]|\\.)*")\s*,\s*'   # 1: msg
    r'(\w+)\s*,\s*'                 # 2: cls
    r'("(?:[^"\\]|\\.)*")\s*,\s*'   # 3: progression source (quoted)
    r'(-?\d+)\s*,\s*'              # 4: receivedPickupIndex
    r'(-?\d+)\s*'                 # 5: inventoryIndex
    r'(?:,[^)]*)?'                # optional popup/reschedule overrides (burst)
    r'\)'
)


def _unescape_lua_string(literal: str) -> str:
    inner = literal[1:-1]
    return inner.replace('\\"', '"').replace("\\\\", "\\")


def _split_top_level_braces(s: str) -> list[str]:
    """Return each top-level ``{...}`` group inside a Lua table body."""
    groups: list[str] = []
    depth = 0
    start: Optional[int] = None
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                groups.append(s[start:i + 1])
                start = None
    return groups


def _parse_progression_stages(prog: str) -> list[list[tuple[str, int]]]:
    """Parse a rendered Lua progression table into stages.

    ``prog`` looks like ``{{{item_id="ITEM_A", quantity=1}}, {{item_id="ITEM_B",
    quantity=1}}}`` — an outer list of stages, each a list of resource dicts.
    Returns ``[[(item_id, qty), ...], ...]`` so the fake can model the game's
    ``HandlePickupResources`` (grant only the first stage the player lacks)."""
    prog = prog.strip()
    inner = prog[1:-1] if prog.startswith("{") and prog.endswith("}") else prog
    stages = [
        [(item_id, int(qty)) for item_id, qty in _RESOURCE_RE.findall(stage_src)]
        for stage_src in _split_top_level_braces(inner)
    ]
    if not stages:  # no nested braces — treat as one flat stage
        stages = [[(item_id, int(qty))
                   for item_id, qty in _RESOURCE_RE.findall(prog)]]
    return stages


class FakeDreadGame:
    """In-process Dread game model + line-delimited JSON TCP client."""

    def __init__(
        self,
        *,
        mod_ver: str = "fake-1.0",
        dread_ver: str = "2.1.0",
        layout_uuid: str = "fake-layout-uuid",
        device_id: str = "sw-fake",
    ) -> None:
        self.mod_ver = mod_ver
        self.dread_ver = dread_ver
        self.layout_uuid = layout_uuid
        self.device_id = device_id

        # ---- game state ----
        self.collected_pickup_indices: set[int] = set()
        self.inventory: dict[str, int] = {}
        self.received_pickups: int = 0
        self.inventory_index: int = 0
        self.beaten: bool = False
        self.in_cutscene: bool = False
        # Models RL.IsInBossArena() — true while Samus stands in a boss arena.
        # When set, the /warp primitive's in-Lua guard returns "in_boss" before
        # Game.LoadScenario, so no revert/restore happens.
        self.in_boss_arena: bool = False
        # Models RL.IsInNavRoom() — true while Samus stands in a Navigation
        # (Adam) room. When set, the /warp guard returns "in_nav" before
        # Game.LoadScenario.
        self.in_nav_room: bool = False
        # Models RL.IsInSaveRoom() — true while Samus stands at a save station.
        # When set, the /warp guard returns "in_save" before Game.LoadScenario,
        # and RL.KillPlayer() drops an incoming DeathLink ("in_safe_room").
        self.in_save_room: bool = False
        # Models RL.IsInMapRoom() — true while Samus stands at a map station.
        # Like a save/nav room, RL.KillPlayer() drops a DeathLink here.
        self.in_map_room: bool = False
        # Last committed save. Game.LoadScenario (the /warp primitive) reloads
        # Samus from this, reverting anything collected/delivered since save().
        self.saved_inventory: dict[str, int] = {}
        self.saved_received_pickups: int = 0
        self.saved_inventory_index: int = 0
        self.saved_collected: set[int] = set()
        # ProgressStat_PlayerDeaths — the Blackboard prop the client polls for
        # DeathLink. Bumped by die() (natural death) or by RL.KillPlayer().
        self.death_count: int = 0
        # Whether a save file is loaded. The GAME blackboard is populated from
        # the file, so with no save loaded (title screen / fresh boot) the death
        # prop reads NIL — the client's read Lua returns "" there, which is not
        # the same as a real "0 deaths". Default True so ordinary tests run
        # in-world; reboot_to_title() / load_save() drive the restart case.
        self.save_loaded: bool = True
        # Game.GetCurrentGameModeID(): 'INGAME' when the player is in the game
        # world. Default INGAME so delivery tests run; set to a menu value
        # (e.g. 'MENU') to exercise the client's pre-game delivery gate.
        self.game_mode: str = "INGAME"

        # ---- delivery internals (RL.PendingPickup) ----
        # A list of progression stages, each a list of (item_id, qty) — the
        # game grants the first stage whose first item the player lacks.
        self._pending: Optional[list[list[tuple[str, int]]]] = None

        # ---- observability ----
        self.onpickedup_calls: list[list[tuple[str, int]]] = []
        self.lua_log: list[str] = []
        # Full RL.ReceivePickup(...) source strings, in arrival order — lets
        # tests assert the burst popup/reschedule overrides the client sends.
        self.receive_srcs: list[str] = []
        self.bootstrap_chunks: list[str] = []
        self.bootstrapped: bool = False
        self.hello_ack: Optional[W.HelloAck] = None
        self.kicks_received: list[str] = []

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._read_task: Optional[asyncio.Task] = None
        self._read_buf = bytearray()

    # ---- lifecycle ----------------------------------------------------

    async def connect(self, host: str = "127.0.0.1", port: int = 17777) -> None:
        self._reader, self._writer = await asyncio.open_connection(host, port)
        await self._send(W.Hello(
            mod_ver=self.mod_ver,
            dread_ver=self.dread_ver,
            layout_uuid=self.layout_uuid,
            device_id=self.device_id,
        ))
        self._read_task = asyncio.create_task(self._read_loop(),
                                              name="fakeswitch-read")

    async def stop(self) -> None:
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):
                pass
            self._read_task = None
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except OSError:
                pass
            self._writer = None
        self._reader = None

    async def wait_for_hello_ack(self, timeout: float = 5.0) -> W.HelloAck:
        """Block until the BridgeServer's HELLO_ACK arrives."""
        deadline = asyncio.get_event_loop().time() + timeout
        while self.hello_ack is None:
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError("HELLO_ACK never arrived")
            await asyncio.sleep(0.01)
        return self.hello_ack

    # ---- test-control helpers ----------------------------------------

    def collect(self, *pickup_indices: int,
                grants: Optional[dict[str, int]] = None) -> None:
        """Simulate the player collecting pickups in-world.

        ``grants`` models the SEED-BAKED local item grant (the game's own
        OnPickedUp running for the player's own item at that node). It bumps the
        live inventory but NOT ``received_pickups`` — and it is NOT saved until
        :meth:`save`, so a warp-reload reverts it. This is exactly the path the
        missing-Energy-Tank bug rides."""
        newly = [idx for idx in pickup_indices
                 if idx not in self.collected_pickup_indices]
        if not newly:
            # Already collected → the actor doesn't respawn, so no re-grant. This
            # is exactly what re-asserting Location_Collected_* buys us post-warp.
            return
        for idx in newly:
            self.collected_pickup_indices.add(idx)
            self.inventory_index += 1
        for item_id, qty in (grants or {}).items():
            self.inventory[item_id] = self.inventory.get(item_id, 0) + qty

    def save(self) -> None:
        """Commit current state to the save file (what a save station does)."""
        self.saved_inventory = dict(self.inventory)
        self.saved_received_pickups = self.received_pickups
        self.saved_inventory_index = self.inventory_index
        self.saved_collected = set(self.collected_pickup_indices)

    def _load_scenario_revert(self) -> None:
        """Model Game.LoadScenario: reload Samus from the last save."""
        self.inventory = dict(self.saved_inventory)
        self.received_pickups = self.saved_received_pickups
        self.inventory_index = self.saved_inventory_index
        self.collected_pickup_indices = set(self.saved_collected)
        self._pending = None

    def end_cutscene(self) -> None:
        """Leave the cinematic; grant any pending received pickup."""
        self.in_cutscene = False
        self._try_grant_pending()

    def die(self) -> None:
        """Simulate the player dying in-world (the game bumps the death stat).
        Models both a natural death and the result of RL.KillPlayer()."""
        self.death_count += 1

    def reboot_to_title(self) -> None:
        """Model an emulator/console restart (or a quit to the main menu): no
        save is loaded, so the game mode is a menu and the death stat prop
        reads nil. The file's death count is kept so load_save() can restore
        it, exactly as the real save file does."""
        self.game_mode = "MENU"
        self.save_loaded = False

    def load_save(self, death_count: Optional[int] = None) -> None:
        """Model loading a save file: the GAME blackboard is populated from the
        file (restoring its death count) and the game enters the world."""
        if death_count is not None:
            self.death_count = death_count
        self.save_loaded = True
        self.game_mode = "INGAME"

    def inventory_of(self, item_id: str) -> int:
        return self.inventory.get(item_id, 0)

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    async def push(self, msg) -> None:
        """Manually push a wire message to the bridge (for tests that need
        to drive an event between polls)."""
        await self._send(msg)

    # ---- delivery model ----------------------------------------------

    def _grant_progression(
        self, stages: list[list[tuple[str, int]]]
    ) -> list[tuple[str, int]]:
        """Model ``RandomizerPowerup.HandlePickupResources``: grant the FIRST
        stage whose first item the player lacks; a single-stage progression is
        always granted (additive). Returns the granted stage (``[]`` if every
        tier is already owned)."""
        always = len(stages) == 1
        for stage in stages:
            if not stage:
                continue
            first_id, first_qty = stage[0]
            if always or self.inventory.get(first_id, 0) < first_qty:
                for item_id, qty in stage:
                    self.inventory[item_id] = self.inventory.get(item_id, 0) + qty
                return stage
        return []

    def _try_grant_pending(self) -> None:
        if self._pending is None or self.in_cutscene:
            return
        stages = self._pending
        self._pending = None
        granted = self._grant_progression(stages)
        self.onpickedup_calls.append(granted)
        # The game bumps both counters unconditionally on confirm, even when no
        # tier is granted (all already owned) — mirror that.
        self.inventory_index += 1
        self.received_pickups += 1

    def _receive_pickup(self, src: str) -> bool:
        """Apply one ``RL.ReceivePickup`` call. Returns True iff it granted
        (so the caller can mirror the bootstrap's post-grant counter pushes)."""
        self.receive_srcs.append(src)
        m = _RECEIVE_RE.search(src)
        if m is None:
            return False
        msg = _unescape_lua_string(m.group(1))
        prog = _unescape_lua_string(m.group(3))
        recv_index = int(m.group(4))
        inv_index = int(m.group(5))
        self.lua_log.append(msg)
        if self._pending is not None:
            return False
        if recv_index != self.received_pickups or inv_index != self.inventory_index:
            return False
        before = self.received_pickups
        self._pending = _parse_progression_stages(prog)
        self._try_grant_pending()
        return self.received_pickups > before

    # ---- wire / dispatch ----------------------------------------------

    async def _send(self, msg) -> None:
        if self._writer is None:
            raise RuntimeError("not connected")
        self._writer.write(W.encode(msg))
        await self._writer.drain()

    def _collected_hex(self) -> str:
        if not self.collected_pickup_indices:
            return ""
        max_bit = max(self.collected_pickup_indices)
        buf = bytearray(max_bit // 8 + 1)
        for idx in self.collected_pickup_indices:
            buf[idx // 8] |= 1 << (idx % 8)
        return buf.hex()

    async def _respond_to_lua_exec(self, seq: int, src: str) -> None:
        """Map one ``lua_exec`` request to the appropriate reply + pushes.

        Mirrors the old binary fake's ``_respond``. The reply ``ok=True``
        is the default; pushes are emitted *before* the reply so the
        BridgeServer fan-out matches the order a real Switch would emit.
        Actually the order doesn't matter — pushes go to ``on_push`` and
        replies match by ``seq``, so they're decoupled — but mirroring
        upstream order keeps the test pleasant to read.
        """
        # Bootstrap chunks: anything containing function defs / DoFile /
        # Pickups-init / Bootstrap=true. Record + ack.
        if ("function " in src or "Game.DoFile" in src
                or "RL.Pickups[i]=" in src or "RL.Bootstrap=true" in src):
            self.bootstrap_chunks.append(src)
            if "RL.Bootstrap=true" in src:
                self.bootstrapped = True
            await self._send(W.LuaExecReply(seq=seq, ok=True, result=""))
            return

        if "RL.ReceivePickup(" in src:
            granted = self._receive_pickup(src)
            await self._send(W.LuaExecReply(seq=seq, ok=True, result=""))
            if granted:
                # Mirror the bootstrap's post-grant reschedule
                # (RL.GivePendingPickup): it re-sends InventoryIndex then
                # ReceivedPickups, which is what clocks the next delivery.
                # Inventory first so the client's next ReceivePickup carries the
                # post-grant index. (In the real mod this fires after the
                # reschedule delay; here it is immediate, which is fine — the
                # client drives delivery off these pushes, not off wall-clock.)
                await self._send(W.Inventory(index=self.inventory_index, inventory=[]))
                await self._send(W.ReceivedPickups(count=self.received_pickups))
            return

        if "Game.LoadScenario(" in src:
            # The /warp primitive. Model the in-Lua gates, then reload from save.
            # Gate order mirrors the warp src: INGAME, then boss arena, then
            # Nav room, then save station, then cutscene/user-interaction.
            if self.game_mode != "INGAME":
                result = "not_ingame"
            elif self.in_boss_arena and "RL.IsInBossArena()" in src:
                result = "in_boss"
            elif self.in_nav_room and "RL.IsInNavRoom()" in src:
                result = "in_nav"
            elif self.in_save_room and "RL.IsInSaveRoom()" in src:
                result = "in_save"
            elif self.in_cutscene:
                result = "no_interaction"
            else:
                self._load_scenario_revert()
                result = "ok"
            await self._send(W.LuaExecReply(seq=seq, ok=True, result=result))
            return

        if "RL_READ_COLLECTED" in src:
            body = ",".join(str(i) for i in sorted(self.collected_pickup_indices))
            await self._send(W.LuaExecReply(seq=seq, ok=True, result=body))
            return

        if "RL_MARK_COLLECTED" in src:
            m = re.search(r"ipairs\(\{([\d,\s]*)\}\)", src)
            if m and m.group(1).strip():
                for tok in m.group(1).split(","):
                    tok = tok.strip()
                    if tok:
                        self.collected_pickup_indices.add(int(tok))
            await self._send(W.LuaExecReply(seq=seq, ok=True, result=""))
            return

        if "RandomizerPowerup.GetItemAmount" in src:
            # build_read_inventory_amounts_lua: self-describing ITEM=amt;... .
            body = ";".join(f"{k}={v}" for k, v in self.inventory.items())
            await self._send(W.LuaExecReply(seq=seq, ok=True, result=body))
            return

        if "return tostring(RL.ReceivedPickups())" in src:
            await self._send(W.LuaExecReply(
                seq=seq, ok=True, result=str(self.received_pickups)))
            return

        if 'WriteToPlayerBlackboard("ReceivedPickups"' in src:
            # build_set_received_pickups_lua: rewind the delivery cursor.
            m = re.search(r'"ReceivedPickups"\s*,\s*"f"\s*,\s*(-?\d+)', src)
            if m is not None:
                self.received_pickups = int(m.group(1))
            await self._send(W.LuaExecReply(seq=seq, ok=True, result=""))
            return

        if ".OnPickedUp(" in src:
            # build_restore_grant_lua: a direct additive grant (warp rewind).
            resources = [(item_id, int(qty))
                         for item_id, qty in _RESOURCE_RE.findall(src)]
            self.onpickedup_calls.append(resources)
            for item_id, qty in resources:
                self.inventory[item_id] = self.inventory.get(item_id, 0) + qty
            self.inventory_index += 1
            await self._send(W.LuaExecReply(seq=seq, ok=True, result=""))
            return

        if "RL.GetCollectedIndicesAndSend" in src:
            await self._send(W.LuaExecReply(seq=seq, ok=True, result=""))
            await self._send(W.Collected(hex=self._collected_hex()))
            return

        if "RL.GetReceivedPickupsAndSend" in src:
            await self._send(W.LuaExecReply(seq=seq, ok=True, result=""))
            await self._send(W.ReceivedPickups(count=self.received_pickups))
            return

        if "RL.GetInventoryAndSend" in src:
            await self._send(W.LuaExecReply(seq=seq, ok=True, result=""))
            await self._send(W.Inventory(
                index=self.inventory_index,
                inventory=[],
            ))
            return

        if "Game.GetCurrentGameModeID" in src:
            await self._send(W.LuaExecReply(
                seq=seq, ok=True, result=self.game_mode))
            return

        if "Init.bBeatenSinceLastReboot" in src:
            await self._send(W.LuaExecReply(
                seq=seq, ok=True,
                result="true" if self.beaten else "false",
            ))
            return

        if "RL.KillPlayer(" in src:
            # Models RL.KillPlayer's guards + death pipeline. A Save/Nav/Map
            # station DROPS the kill ("in_safe_room"); a menu no-ops
            # ("not_ingame"); otherwise the death pipeline runs (ForceDead →
            # OnPlayerDead → stat bump) and returns "killed".
            if self.game_mode != "INGAME":
                result = "not_ingame"
            elif self.in_save_room or self.in_nav_room or self.in_map_room:
                result = "in_safe_room"
            else:
                self.die()
                result = "killed"
            await self._send(W.LuaExecReply(seq=seq, ok=True, result=result))
            return

        if "ProgressStat_PlayerDeaths" in src:
            # No save loaded ⇒ the prop is nil ⇒ the read Lua returns "".
            await self._send(W.LuaExecReply(
                seq=seq, ok=True,
                result=str(self.death_count) if self.save_loaded else ""))
            return

        # Anything else (Game.AddSF arming, pokes, etc.) → ack.
        self.bootstrap_chunks.append(src)
        if "RL.Bootstrap=true" in src:
            self.bootstrapped = True
        await self._send(W.LuaExecReply(seq=seq, ok=True, result=""))

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                chunk = await self._reader.read(4096)
                if not chunk:
                    return
                self._read_buf.extend(chunk)
                for line in W.iter_lines(self._read_buf):
                    await self._dispatch(line)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            return

    async def _dispatch(self, line: bytes) -> None:
        try:
            d = W.decode(line)
        except Exception:
            return
        parsed = W.from_dict(d)
        if parsed is None:
            return
        if isinstance(parsed, W.HelloAck):
            self.hello_ack = parsed
            return
        if isinstance(parsed, W.LuaExec):
            await self._respond_to_lua_exec(parsed.seq, parsed.src)
            return
        if isinstance(parsed, W.Ping):
            await self._send(W.Pong(ts_ms=parsed.ts_ms))
            return
        if isinstance(parsed, W.Kick):
            self.kicks_received.append(parsed.reason)
            return
