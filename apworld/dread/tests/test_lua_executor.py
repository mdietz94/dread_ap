"""Async unit tests for DreadConnection against an in-process fake Switch.

Under the inverted topology, the PC listens and the Switch dials in. So
the test fixture binds a small TCP server on the PC side, the fake
Switch dials in via ``asyncio.open_connection``, and the PC-side
accepted ``(reader, writer)`` get wrapped in :class:`DreadConnection`.
The fake responds to handshake + Lua-exec on its side of the pipe,
mimicking exlaunch byte-for-byte.

Wire format (confirmed from vendor/open-dread-rando-exlaunch/source/program/):

    HANDSHAKE reply       [0x01][req_num:1]                                       (2 bytes)
    REMOTE_LUA_EXEC reply [0x03][req_num:1][success:1][len:3 LE u24][payload]
    Push frames           [type:1][len:4 LE u32][payload]
    MALFORMED             [0x09][failing_type:1][rcv:4 LE u32][should:4 LE u32]

Run with:  python -m pytest apworld/dread/tests/test_lua_executor.py -v
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client import lua_executor as le  # noqa: E402
from dread.client import lua_packets as lp  # noqa: E402


# ---- Frame builders mirroring the exlaunch C++ sender ---------------------

def _handshake_reply(req_num: int) -> bytes:
    return bytes([lp.PacketType.HANDSHAKE, req_num & 0xFF])


def _lua_exec_reply(req_num: int, success: bool, payload: bytes) -> bytes:
    return (
        bytes([lp.PacketType.REMOTE_LUA_EXEC, req_num & 0xFF, 1 if success else 0])
        + len(payload).to_bytes(3, "little")
        + payload
    )


def _push_frame(packet_type: lp.PacketType, payload: bytes) -> bytes:
    return bytes([packet_type]) + len(payload).to_bytes(4, "little") + payload


class FakeSwitchDialer:
    """Test fixture: TCP-dials an in-process bridge and replays exlaunch
    wire frames in response to PC-issued requests.

    Lifecycle:
        bridge = await LoopbackBridge.start()
        fake   = FakeSwitchDialer()
        fake.lua_responses = [(True, b"42"), ...]
        await fake.dial(bridge.port)
        # bridge.accepted_pair is now (reader, writer) for DreadConnection
        conn = le.DreadConnection(*bridge.accepted_pair)
        await conn.setup()
        ...
    """

    def __init__(self):
        self.received: list[bytes] = []
        self.api_response = b"1,4096,true,abcd-uuid,v2.1.0"
        self.lua_responses: list[tuple[bool, bytes]] = []
        self.pushes_after_next_exec: list[bytes] = []
        self.pushes_before_next_exec: list[bytes] = []
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        self._req_num: int = 0

    def _bump(self) -> int:
        n = self._req_num
        self._req_num = (self._req_num + 1) % 256
        return n

    async def dial(self, port: int) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self._writer = writer
        self._task = asyncio.create_task(
            self._handle(reader, writer), name="fakeswitch-dialer")

    async def stop(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()
            self._task = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            # Handshake request: [0x01][interest_byte]
            handshake = await reader.readexactly(2)
            self.received.append(bytes(handshake))
            assert handshake[0] == lp.PacketType.HANDSHAKE
            writer.write(_handshake_reply(self._bump()))
            await writer.drain()

            # First Lua-exec is the API version probe.
            header = await reader.readexactly(5)
            assert header[0] == lp.PacketType.REMOTE_LUA_EXEC
            length = int.from_bytes(header[1:5], "little")
            src = await reader.readexactly(length)
            self.received.append(bytes(src))
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
                src = await reader.readexactly(length)
                self.received.append(bytes(src))

                for frame in self.pushes_before_next_exec:
                    writer.write(frame)
                self.pushes_before_next_exec = []

                if not self.lua_responses:
                    writer.write(_lua_exec_reply(self._bump(), True, b""))
                else:
                    success, payload = self.lua_responses.pop(0)
                    writer.write(_lua_exec_reply(self._bump(), success, payload))

                for frame in self.pushes_after_next_exec:
                    writer.write(frame)
                self.pushes_after_next_exec = []

                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


class LoopbackBridge:
    """Tiny TCP server that captures one accepted (reader, writer) pair."""

    def __init__(self):
        self._server: asyncio.base_events.Server | None = None
        self.port: int = 0
        self._accept_future: asyncio.Future[
            tuple[asyncio.StreamReader, asyncio.StreamWriter]
        ] | None = None
        self.accepted_pair: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None

    @classmethod
    async def start(cls) -> "LoopbackBridge":
        b = cls()
        loop = asyncio.get_running_loop()
        b._accept_future = loop.create_future()
        b._server = await asyncio.start_server(b._accept, host="127.0.0.1", port=0)
        b.port = b._server.sockets[0].getsockname()[1]
        return b

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._accept_future is not None and not self._accept_future.done():
            self._accept_future.set_result((reader, writer))
        # Keep the coroutine alive so writer doesn't get garbage-collected.
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass

    async def wait_accept(self, timeout: float = 2.0) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        assert self._accept_future is not None
        self.accepted_pair = await asyncio.wait_for(self._accept_future, timeout=timeout)
        return self.accepted_pair

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=1.0)
            except (asyncio.TimeoutError, Exception):
                pass


async def _setup_conn(
    fake: FakeSwitchDialer,
) -> tuple[le.DreadConnection, LoopbackBridge]:
    bridge = await LoopbackBridge.start()
    await fake.dial(bridge.port)
    reader, writer = await bridge.wait_accept()
    conn = le.DreadConnection(reader, writer)
    return conn, bridge


# ---- Tests ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_setup_handshake_and_api_parse():
    fake = FakeSwitchDialer()
    conn, bridge = await _setup_conn(fake)
    try:
        api = await conn.setup()
        assert api.api_version == 1
        assert api.buffer_size == 4096
        assert api.bootstrap is True
        assert api.layout_uuid == "abcd-uuid"
        assert api.game_version == "v2.1.0"
        await conn.close()
        # handshake byte + multiworld interest
        assert fake.received[0] == b"\x01\x02"
        # the API version probe Lua source
        assert fake.received[1].startswith(b"return string.format")
    finally:
        await fake.stop()
        await bridge.stop()


@pytest.mark.asyncio
async def test_run_lua_returns_response():
    fake = FakeSwitchDialer()
    fake.lua_responses = [(True, b"42"), (True, b"hello")]
    conn, bridge = await _setup_conn(fake)
    try:
        await conn.setup()
        r1 = await conn.run_lua("return 42")
        r2 = await conn.run_lua("return 'hello'")
        assert r1.payload == b"42"
        assert r2.payload == b"hello"
        await conn.close()
    finally:
        await fake.stop()
        await bridge.stop()


@pytest.mark.asyncio
async def test_concurrent_run_lua_serializes_via_lock():
    fake = FakeSwitchDialer()
    fake.lua_responses = [(True, b"first"), (True, b"second")]
    conn, bridge = await _setup_conn(fake)
    try:
        await conn.setup()
        results = await asyncio.gather(
            conn.run_lua("return 1"),
            conn.run_lua("return 2"),
        )
        payloads = sorted(r.payload for r in results)
        assert payloads == [b"first", b"second"]
        await conn.close()
    finally:
        await fake.stop()
        await bridge.stop()


@pytest.mark.asyncio
async def test_push_frame_routes_to_on_push():
    fake = FakeSwitchDialer()
    payload = b"locations:" + bytes([0x05])
    fake.lua_responses = [(True, b"ok")]
    fake.pushes_after_next_exec = [
        _push_frame(lp.PacketType.COLLECTED_INDICES, payload),
    ]
    bridge = await LoopbackBridge.start()
    await fake.dial(bridge.port)
    reader, writer = await bridge.wait_accept()

    pushes: list[tuple[lp.PacketType, lp.Response]] = []
    push_event = asyncio.Event()

    async def on_push(ptype: lp.PacketType, resp: lp.Response) -> None:
        pushes.append((ptype, resp))
        push_event.set()

    conn = le.DreadConnection(reader, writer, on_push=on_push)
    try:
        await conn.setup()
        reply = await conn.run_lua("RL.GetCollectedIndicesAndSend(); return ''")
        assert reply.payload == b"ok"
        await asyncio.wait_for(push_event.wait(), timeout=1.0)
        assert len(pushes) == 1
        ptype, resp = pushes[0]
        assert ptype == lp.PacketType.COLLECTED_INDICES
        assert resp.success is True
        assert resp.payload == payload
        await conn.close()
    finally:
        await fake.stop()
        await bridge.stop()


@pytest.mark.asyncio
async def test_lua_exec_reply_still_routes_to_pending_future():
    """A Lua-exec reply (no surrounding pushes) routes to the awaiter,
    not to on_push, even when on_push is set."""
    fake = FakeSwitchDialer()
    fake.lua_responses = [(True, b"the-reply")]
    bridge = await LoopbackBridge.start()
    await fake.dial(bridge.port)
    reader, writer = await bridge.wait_accept()

    pushes: list = []

    async def on_push(ptype: lp.PacketType, resp: lp.Response) -> None:
        pushes.append((ptype, resp))

    conn = le.DreadConnection(reader, writer, on_push=on_push)
    try:
        await conn.setup()
        reply = await conn.run_lua("return 'the-reply'")
        assert reply.payload == b"the-reply"
        await asyncio.sleep(0.05)
        assert pushes == []
        await conn.close()
    finally:
        await fake.stop()
        await bridge.stop()


@pytest.mark.asyncio
async def test_interleaved_push_and_reply():
    fake = FakeSwitchDialer()
    fake.lua_responses = [(True, b"reply-payload")]
    fake.pushes_before_next_exec = [
        _push_frame(lp.PacketType.NEW_INVENTORY, b'{"index":7,"inventory":[1,2,3]}'),
    ]
    bridge = await LoopbackBridge.start()
    await fake.dial(bridge.port)
    reader, writer = await bridge.wait_accept()

    pushes: list[tuple[lp.PacketType, lp.Response]] = []
    push_event = asyncio.Event()

    async def on_push(ptype: lp.PacketType, resp: lp.Response) -> None:
        pushes.append((ptype, resp))
        push_event.set()

    conn = le.DreadConnection(reader, writer, on_push=on_push)
    try:
        await conn.setup()
        reply = await conn.run_lua("return 1")
        assert reply.payload == b"reply-payload"
        await asyncio.wait_for(push_event.wait(), timeout=1.0)
        assert len(pushes) == 1
        ptype, resp = pushes[0]
        assert ptype == lp.PacketType.NEW_INVENTORY
        assert resp.payload == b'{"index":7,"inventory":[1,2,3]}'
        await conn.close()
    finally:
        await fake.stop()
        await bridge.stop()


@pytest.mark.asyncio
async def test_unknown_packet_type_exits_read_loop_cleanly():
    """If the Switch emits a byte we don't know, the read loop exits
    cleanly without corrupting later frames."""
    fake = FakeSwitchDialer()
    fake.lua_responses = [(True, b"ok")]
    fake.pushes_after_next_exec = [bytes([0xEE]) + b"extra-junk"]
    bridge = await LoopbackBridge.start()
    await fake.dial(bridge.port)
    reader, writer = await bridge.wait_accept()

    pushes: list = []

    async def on_push(ptype: lp.PacketType, resp: lp.Response) -> None:
        pushes.append((ptype, resp))

    conn = le.DreadConnection(reader, writer, on_push=on_push)
    try:
        await conn.setup()
        reply = await conn.run_lua("return 1")
        assert reply.payload == b"ok"
        # The read loop hits the bad byte after the reply and exits.
        await asyncio.sleep(0.1)
        await conn.close()
    finally:
        await fake.stop()
        await bridge.stop()


@pytest.mark.asyncio
async def test_run_until_disconnect_returns_when_socket_drops():
    """Closing the Switch side of the pipe makes the read loop exit;
    ``run_until_disconnect`` returns. Previously this responsibility lived
    on a context-side ``on_disconnect`` callback; under the inverted
    topology the per-connection accept handler simply awaits this method
    and unwinds on return."""
    fake = FakeSwitchDialer()
    bridge = await LoopbackBridge.start()
    await fake.dial(bridge.port)
    reader, writer = await bridge.wait_accept()
    conn = le.DreadConnection(reader, writer)
    try:
        await conn.setup()
        # Switch side drops the socket.
        await fake.stop()
        await asyncio.wait_for(conn.run_until_disconnect(), timeout=2.0)
        await conn.close()
    finally:
        await fake.stop()
        await bridge.stop()
