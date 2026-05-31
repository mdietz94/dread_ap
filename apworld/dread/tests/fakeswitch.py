"""A stateful fake Metroid Dread game that speaks the exlaunch wire protocol.

The integration-test fixture between unit tests and a real Switch.

Under the SMO-style inverted topology, the Switch dials the PC. So this
fake is a TCP **client**: on :meth:`dial`, it sends a UDP discovery probe
to the responder, parses the ``{"t":"bridge","host":...,"port":...}``
reply, opens a TCP socket to the advertised address, and runs the
protocol loop on that connection.

It models the *real* delivery protocol read from upstream Lua
(randovania ``bootstrap_part_2.lua`` + open-dread-rando ``randomizer_powerup.lua``):

  * Two counters — ``ReceivedPickups`` (confirmed AP deliveries) and
    ``InventoryIndex`` (every pickup, local or remote). Both reported via the
    RECEIVED_PICKUPS / NEW_INVENTORY pushes.
  * ``RL.ReceivePickup(msg, cls, prog, receivedPickupIndex, inventoryIndex)``
    grants ONLY when no pickup is pending AND both indices match the live
    counters; it then defers through cutscenes and, on confirm, applies the
    resources (``OnPickedUp`` → ``InventoryIndex += 1``) and ``ReceivedPickups += 1``.
  * Collecting a world pickup locally also runs ``OnPickedUp`` → bumps
    ``InventoryIndex`` and sets the location bit.

``in_cutscene`` models the cinematic window where
``Scenario.IsUserInteractionEnabled`` is false: a received pickup is held
pending until :meth:`end_cutscene`.
"""
from __future__ import annotations

import asyncio
import json
import re
import socket
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

from dread.client import lua_packets as lp  # noqa: E402

# Resources inside the progression literal render as {item_id="ITEM_X", quantity=N}.
_RESOURCE_RE = re.compile(r'item_id\s*=\s*"([^"]+)"\s*,\s*quantity\s*=\s*(-?\d+)')

# RL.ReceivePickup("msg", Cls, "prog", receivedIndex, inventoryIndex)
_RECEIVE_RE = re.compile(
    r'RL\.ReceivePickup\(\s*'
    r'("(?:[^"\\]|\\.)*")\s*,\s*'   # 1: msg
    r'(\w+)\s*,\s*'                 # 2: cls
    r'("(?:[^"\\]|\\.)*")\s*,\s*'   # 3: progression source
    r'(-?\d+)\s*,\s*'              # 4: receivedPickupIndex
    r'(-?\d+)\s*\)'                # 5: inventoryIndex
)


def _unescape_lua_string(literal: str) -> str:
    inner = literal[1:-1]
    return inner.replace('\\"', '"').replace("\\\\", "\\")


def _lua_exec_reply(req_num: int, success: bool, payload: bytes) -> bytes:
    return (
        bytes([lp.PacketType.REMOTE_LUA_EXEC, req_num & 0xFF, 1 if success else 0])
        + len(payload).to_bytes(3, "little")
        + payload
    )


def _push_frame(packet_type: lp.PacketType, payload: bytes) -> bytes:
    return bytes([packet_type]) + len(payload).to_bytes(4, "little") + payload


async def discover_bridge(
    discovery_port: int,
    probe_host: str = "127.0.0.1",
    timeout: float = 1.0,
) -> tuple[str, int]:
    """UDP-probe the bridge and return ``(host, port)`` from the reply.

    Tests run both sides on loopback, so the probe target is fixed at
    ``127.0.0.1`` — production parity (loopback first) without
    duplicating the SMO ``/24`` sweep logic in the test harness. Raises
    ``asyncio.TimeoutError`` on no reply.
    """
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        probe = (json.dumps({"t": "discover", "mod_ver": "fake-dread"})
                 + "\n").encode("utf-8")
        await loop.sock_sendto(sock, probe, (probe_host, discovery_port))
        data, _addr = await asyncio.wait_for(
            loop.sock_recvfrom(sock, 4096), timeout=timeout)
    finally:
        sock.close()
    reply = json.loads(data.decode("utf-8"))
    if reply.get("t") != "bridge":
        raise RuntimeError(f"unexpected discovery reply: {reply!r}")
    return str(reply["host"]), int(reply["port"])


class FakeDreadGame:
    """In-process Dread game model. TCP-dials a DreadClient bridge."""

    def __init__(self) -> None:
        self.api_response = b"1,4096,true,fake-layout-uuid,2.1.0"

        # ---- game state ----
        self.collected_pickup_indices: set[int] = set()
        self.inventory: dict[str, int] = {}        # ITEM_* -> amount (additive)
        self.received_pickups: int = 0             # Blackboard.ReceivedPickups
        self.inventory_index: int = 0              # Blackboard.InventoryIndex
        self.beaten: bool = False
        self.in_cutscene: bool = False             # IsUserInteractionEnabled == false

        # ---- delivery internals (RL.PendingPickup) ----
        self._pending: Optional[list[tuple[str, int]]] = None

        # ---- observability ----
        self.onpickedup_calls: list[list[tuple[str, int]]] = []
        self.lua_log: list[str] = []
        self.bootstrap_chunks: list[str] = []
        self.bootstrapped: bool = False

        # ---- transport ----
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        self._req_num: int = 0

    # ---- lifecycle ----------------------------------------------------

    async def dial(self, discovery_port: int, probe_host: str = "127.0.0.1") -> None:
        """Run the SMO-style discover-then-dial sequence against a bridge.

        Probes UDP at ``probe_host:discovery_port``, then TCP-connects to
        the advertised ``(host, port)``. Starts the protocol loop as a
        background task; tests can interleave :meth:`collect` /
        :meth:`end_cutscene` etc. while the loop runs.
        """
        host, port = await discover_bridge(discovery_port, probe_host)
        reader, writer = await asyncio.open_connection(host, port)
        self._writer = writer
        self._task = asyncio.create_task(
            self._handle(reader, writer), name="fakeswitch-handle")

    async def disconnect(self) -> None:
        """Close the writer and wait for the protocol task to unwind.

        Use to simulate the Switch losing its socket mid-session — the
        DreadContext's accept handler sees EOF and returns to listening.
        """
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()
            self._task = None

    async def stop(self) -> None:
        """Alias of :meth:`disconnect` for test-fixture symmetry with
        the prior server-mode fake."""
        await self.disconnect()

    # ---- test-control helpers ----------------------------------------

    def collect(self, *pickup_indices: int) -> None:
        """Simulate the player collecting pickups in-world. Each newly-collected
        pickup runs OnPickedUp → bumps InventoryIndex (as the real game does)."""
        for idx in pickup_indices:
            if idx not in self.collected_pickup_indices:
                self.collected_pickup_indices.add(idx)
                self.inventory_index += 1

    def end_cutscene(self) -> None:
        """Leave the cinematic; grant any pending received pickup."""
        self.in_cutscene = False
        self._try_grant_pending()

    def inventory_of(self, item_id: str) -> int:
        return self.inventory.get(item_id, 0)

    @property
    def has_pending(self) -> bool:
        return self._pending is not None

    # ---- delivery model ----------------------------------------------

    def _try_grant_pending(self) -> None:
        if self._pending is None or self.in_cutscene:
            return
        resources = self._pending
        self._pending = None
        self.onpickedup_calls.append(resources)
        for item_id, qty in resources:
            self.inventory[item_id] = self.inventory.get(item_id, 0) + qty
        self.inventory_index += 1
        self.received_pickups += 1

    def _receive_pickup(self, src: str) -> None:
        m = _RECEIVE_RE.search(src)
        if m is None:
            return
        msg = _unescape_lua_string(m.group(1))
        prog = _unescape_lua_string(m.group(3))
        recv_index = int(m.group(4))
        inv_index = int(m.group(5))
        self.lua_log.append(msg)
        if self._pending is not None:
            return
        if recv_index != self.received_pickups or inv_index != self.inventory_index:
            return
        self._pending = [(item_id, int(qty)) for item_id, qty in _RESOURCE_RE.findall(prog)]
        self._try_grant_pending()

    # ---- wire handling ------------------------------------------------

    def _bump(self) -> int:
        n = self._req_num
        self._req_num = (self._req_num + 1) % 256
        return n

    def _collected_bitfield(self) -> bytes:
        if not self.collected_pickup_indices:
            return b"locations:"
        max_bit = max(self.collected_pickup_indices)
        buf = bytearray(max_bit // 8 + 1)
        for idx in self.collected_pickup_indices:
            buf[idx // 8] |= 1 << (idx % 8)
        return b"locations:" + bytes(buf)

    def _respond(self, src: str) -> list[bytes]:
        if ("function " in src or "Game.DoFile" in src
                or "RL.Pickups[i]=" in src or "RL.Bootstrap=true" in src):
            self.bootstrap_chunks.append(src)
            if "RL.Bootstrap=true" in src:
                self.bootstrapped = True
            return [_lua_exec_reply(self._bump(), True, b"")]

        if "RL.ReceivePickup(" in src:
            self._receive_pickup(src)
            return [_lua_exec_reply(self._bump(), True, b"")]

        if "RL.GetCollectedIndicesAndSend" in src:
            return [
                _lua_exec_reply(self._bump(), True, b""),
                _push_frame(lp.PacketType.COLLECTED_INDICES, self._collected_bitfield()),
            ]

        if "RL.GetReceivedPickupsAndSend" in src:
            return [
                _lua_exec_reply(self._bump(), True, b""),
                _push_frame(lp.PacketType.RECEIVED_PICKUPS,
                            str(self.received_pickups).encode("utf-8")),
            ]

        if "RL.GetInventoryAndSend" in src:
            blob = json.dumps({"index": self.inventory_index, "inventory": []})
            return [
                _lua_exec_reply(self._bump(), True, b""),
                _push_frame(lp.PacketType.NEW_INVENTORY, blob.encode("utf-8")),
            ]

        if "Init.bBeatenSinceLastReboot" in src:
            return [_lua_exec_reply(self._bump(), True,
                                    b"true" if self.beaten else b"false")]

        if "RL.Version" in src or "string.format('%d,%d" in src:
            return [_lua_exec_reply(self._bump(), True, self.api_response)]

        self.bootstrap_chunks.append(src)
        if "RL.Bootstrap=true" in src:
            self.bootstrapped = True
        return [_lua_exec_reply(self._bump(), True, b"")]

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            # Handshake request: [0x01][interest_byte] — sent by the bridge.
            handshake = await reader.readexactly(2)
            assert handshake[0] == lp.PacketType.HANDSHAKE
            writer.write(bytes([lp.PacketType.HANDSHAKE, self._bump()]))
            await writer.drain()

            # First Lua-exec is always the API version probe.
            header = await reader.readexactly(5)
            assert header[0] == lp.PacketType.REMOTE_LUA_EXEC
            length = int.from_bytes(header[1:5], "little")
            await reader.readexactly(length)
            writer.write(_lua_exec_reply(self._bump(), True, self.api_response))
            await writer.drain()

            # Request loop.
            while True:
                first = await reader.readexactly(1)
                if first[0] == lp.PacketType.KEEP_ALIVE:
                    continue
                if first[0] != lp.PacketType.REMOTE_LUA_EXEC:
                    return
                length = int.from_bytes(await reader.readexactly(4), "little")
                src = (await reader.readexactly(length)).decode("utf-8")
                for frame in self._respond(src):
                    writer.write(frame)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
