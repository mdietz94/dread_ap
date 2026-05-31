"""Async unit tests for DreadExecutor against an in-process fake server.

Doesn't touch a real Switch. Spins up an asyncio TCP server in the same
event loop that mimics the exlaunch wire protocol byte-for-byte.

Wire format (confirmed from https://github.com/randovania/open-dread-rando-exlaunch/tree/main/source/program):

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
    """[0x01][req_num] — what the Switch emits after PACKET_HANDSHAKE."""
    return bytes([lp.PacketType.HANDSHAKE, req_num & 0xFF])


def _lua_exec_reply(req_num: int, success: bool, payload: bytes) -> bytes:
    """[0x03][req_num][success][len:3 LE u24][payload]."""
    return (
        bytes([lp.PacketType.REMOTE_LUA_EXEC, req_num & 0xFF, 1 if success else 0])
        + len(payload).to_bytes(3, "little")
        + payload
    )


def _push_frame(packet_type: lp.PacketType, payload: bytes) -> bytes:
    """[type][len:4 LE u32][payload] — push shape used by 0x02/0x05/0x06/0x07/0x08."""
    return bytes([packet_type]) + len(payload).to_bytes(4, "little") + payload


class FakeSwitch:
    """In-process server that mimics exlaunch byte-for-byte."""

    def __init__(self):
        self.received: list[bytes] = []
        self.api_response = b"1,4096,true,abcd-uuid,v2.1.0"
        self.lua_responses: list[tuple[bool, bytes]] = []
        # Frames the server should push as soon as a Lua-exec request is
        # received (after sending the matching reply). Each entry is a
        # raw bytes object built via _push_frame() or similar.
        self.pushes_after_next_exec: list[bytes] = []
        # Frames to push BEFORE replying to the next exec — useful for
        # interleaving tests.
        self.pushes_before_next_exec: list[bytes] = []
        self._server: asyncio.base_events.Server | None = None
        self.port: int = 0
        self._req_num: int = 0
        # Track active connections so stop() can close them — asyncio's
        # Server.close() only stops accepting new conns; it leaves existing
        # ones alive, which makes "simulate mid-session drop" tests hang
        # until the client's READ_TIMEOUT (15s).
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, host="127.0.0.1", port=0)
        sock = self._server.sockets[0]
        self.port = sock.getsockname()[1]

    async def stop(self) -> None:
        # Close any active client writers first — kicks each client's
        # next readexactly into IncompleteReadError immediately.
        for w in list(self._writers):
            try:
                w.close()
            except OSError:
                pass
        self._writers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _bump(self) -> int:
        n = self._req_num
        self._req_num = (self._req_num + 1) % 256
        return n

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        try:
            # --- Handshake request: [0x01][interest_byte] ---
            handshake = await reader.readexactly(2)
            self.received.append(handshake)
            assert handshake[0] == lp.PacketType.HANDSHAKE
            writer.write(_handshake_reply(self._bump()))
            await writer.drain()

            # --- API version Lua-exec: [0x03][len:4 LE u32][payload] ---
            header = await reader.readexactly(5)
            assert header[0] == lp.PacketType.REMOTE_LUA_EXEC
            length = int.from_bytes(header[1:5], "little")
            body = await reader.readexactly(length)
            self.received.append(body)
            writer.write(_lua_exec_reply(self._bump(), True, self.api_response))
            await writer.drain()

            # --- Subsequent requests ---
            while True:
                first = await reader.readexactly(1)
                if first == bytes([lp.PacketType.KEEP_ALIVE]):
                    # Client→server ping; no reply
                    continue
                if first[0] != lp.PacketType.REMOTE_LUA_EXEC:
                    return
                length_bytes = await reader.readexactly(4)
                length = int.from_bytes(length_bytes, "little")
                body = await reader.readexactly(length)
                self.received.append(body)

                # Any "before-reply" pushes go now
                for frame in self.pushes_before_next_exec:
                    writer.write(frame)
                self.pushes_before_next_exec = []

                # Reply
                if not self.lua_responses:
                    writer.write(_lua_exec_reply(self._bump(), True, b""))
                else:
                    success, payload = self.lua_responses.pop(0)
                    writer.write(_lua_exec_reply(self._bump(), success, payload))

                # Any "after-reply" pushes go now
                for frame in self.pushes_after_next_exec:
                    writer.write(frame)
                self.pushes_after_next_exec = []

                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


# ---- Existing tests -------------------------------------------------------

@pytest.mark.asyncio
async def test_connect_handshake_and_api_parse():
    fake = FakeSwitch()
    await fake.start()
    try:
        exec_ = le.DreadExecutor(host="127.0.0.1", port=fake.port)
        api = await exec_.connect()
        assert api.api_version == 1
        assert api.buffer_size == 4096
        assert api.bootstrap is True
        assert api.layout_uuid == "abcd-uuid"
        assert api.game_version == "v2.1.0"
        await exec_.close()
        assert fake.received[0] == b"\x01\x02"  # handshake byte + multiworld
        assert fake.received[1].startswith(b"return string.format")
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_run_lua_returns_response():
    fake = FakeSwitch()
    fake.lua_responses = [(True, b"42"), (True, b"hello")]
    await fake.start()
    try:
        exec_ = le.DreadExecutor(host="127.0.0.1", port=fake.port)
        await exec_.connect()
        r1 = await exec_.run_lua("return 42")
        r2 = await exec_.run_lua("return 'hello'")
        assert r1.payload == b"42"
        assert r2.payload == b"hello"
        await exec_.close()
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_concurrent_run_lua_serializes_via_lock():
    """Two concurrent run_lua calls should both succeed via the exec lock."""
    fake = FakeSwitch()
    fake.lua_responses = [(True, b"first"), (True, b"second")]
    await fake.start()
    try:
        exec_ = le.DreadExecutor(host="127.0.0.1", port=fake.port)
        await exec_.connect()
        results = await asyncio.gather(
            exec_.run_lua("return 1"),
            exec_.run_lua("return 2"),
        )
        payloads = sorted(r.payload for r in results)
        assert payloads == [b"first", b"second"]
        await exec_.close()
    finally:
        await fake.stop()


# ---- Push-demux tests (Gate A1) -------------------------------------------

@pytest.mark.asyncio
async def test_push_frame_routes_to_on_push():
    """A push frame emitted alongside a Lua-exec reply must reach on_push
    with the correct PacketType and payload."""
    fake = FakeSwitch()
    payload = b"locations:" + bytes([0x05])  # bit 0 and bit 2 set in byte 0
    fake.lua_responses = [(True, b"ok")]
    fake.pushes_after_next_exec = [_push_frame(lp.PacketType.COLLECTED_INDICES, payload)]
    await fake.start()

    pushes: list[tuple[lp.PacketType, lp.Response]] = []
    push_event = asyncio.Event()

    async def on_push(ptype: lp.PacketType, resp: lp.Response) -> None:
        pushes.append((ptype, resp))
        push_event.set()

    try:
        exec_ = le.DreadExecutor(host="127.0.0.1", port=fake.port, on_push=on_push)
        await exec_.connect()
        reply = await exec_.run_lua("RL.GetCollectedIndicesAndSend(); return ''")
        # The reply should be the empty string from the Lua exec.
        assert reply.payload == b"ok"
        # Wait for the push to land
        await asyncio.wait_for(push_event.wait(), timeout=1.0)
        assert len(pushes) == 1
        ptype, resp = pushes[0]
        assert ptype == lp.PacketType.COLLECTED_INDICES
        assert resp.success is True
        assert resp.payload == payload
        await exec_.close()
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_lua_exec_reply_still_routes_to_pending_future():
    """A Lua-exec reply (no surrounding pushes) routes to the awaiter,
    not to on_push, even when on_push is set."""
    fake = FakeSwitch()
    fake.lua_responses = [(True, b"the-reply")]
    await fake.start()

    pushes: list = []

    async def on_push(ptype: lp.PacketType, resp: lp.Response) -> None:
        pushes.append((ptype, resp))

    try:
        exec_ = le.DreadExecutor(host="127.0.0.1", port=fake.port, on_push=on_push)
        await exec_.connect()
        reply = await exec_.run_lua("return 'the-reply'")
        assert reply.payload == b"the-reply"
        # Give the read loop a tick to deliver anything stray.
        await asyncio.sleep(0.05)
        assert pushes == []
        await exec_.close()
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_interleaved_push_and_reply():
    """A push arriving BEFORE the reply (still within the same exec turn)
    must go to on_push; the reply must still resolve the future."""
    fake = FakeSwitch()
    fake.lua_responses = [(True, b"reply-payload")]
    fake.pushes_before_next_exec = [
        _push_frame(lp.PacketType.NEW_INVENTORY, b'{"index":7,"inventory":[1,2,3]}'),
    ]
    await fake.start()

    pushes: list[tuple[lp.PacketType, lp.Response]] = []
    push_event = asyncio.Event()

    async def on_push(ptype: lp.PacketType, resp: lp.Response) -> None:
        pushes.append((ptype, resp))
        push_event.set()

    try:
        exec_ = le.DreadExecutor(host="127.0.0.1", port=fake.port, on_push=on_push)
        await exec_.connect()
        reply = await exec_.run_lua("return 1")
        assert reply.payload == b"reply-payload"
        await asyncio.wait_for(push_event.wait(), timeout=1.0)
        assert len(pushes) == 1
        ptype, resp = pushes[0]
        assert ptype == lp.PacketType.NEW_INVENTORY
        assert resp.payload == b'{"index":7,"inventory":[1,2,3]}'
        await exec_.close()
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_unknown_packet_type_raises():
    """If the Switch emits a byte we don't know, the read loop must surface
    it (not silently corrupt later frames)."""
    fake = FakeSwitch()
    fake.lua_responses = [(True, b"ok")]
    fake.pushes_after_next_exec = [bytes([0xEE]) + b"extra-junk"]
    await fake.start()

    pushes: list = []

    async def on_push(ptype: lp.PacketType, resp: lp.Response) -> None:
        pushes.append((ptype, resp))

    try:
        exec_ = le.DreadExecutor(host="127.0.0.1", port=fake.port, on_push=on_push)
        await exec_.connect()
        # The reply itself should land cleanly.
        reply = await exec_.run_lua("return 1")
        assert reply.payload == b"ok"
        # The read loop hits the bad byte after the reply and exits cleanly.
        # Give it a beat to log + tear down.
        await asyncio.sleep(0.1)
        await exec_.close()
    finally:
        await fake.stop()


@pytest.mark.asyncio
async def test_on_disconnect_fires_when_socket_drops():
    """The bug this guards: when the Switch closes the TCP socket mid-
    session (Ryujinx-side drop after a stall), the executor's read loop
    exits but the owning context never learned. The supervisor stayed
    asleep at max-backoff with a dead ``executor`` reference and the
    poll loop spammed ``Switch poll failed`` forever — users had to
    manually ``/dread_connect`` to recover.

    Fixed by ``on_disconnect``: the read loop's exception branch fires
    it exactly once before unwinding, so the context can drop its
    executor reference and wake the supervisor for an immediate redial.
    """
    fake = FakeSwitch()
    await fake.start()

    disconnect_calls: list[float] = []

    async def on_disconnect() -> None:
        disconnect_calls.append(asyncio.get_running_loop().time())

    try:
        exec_ = le.DreadExecutor(
            host="127.0.0.1", port=fake.port, on_disconnect=on_disconnect,
        )
        await exec_.connect()
        # Slam the server side closed mid-session. The read loop's
        # next readexactly() raises IncompleteReadError → on_disconnect.
        await fake.stop()
        # Give the read loop a moment to notice EOF + run the callback.
        for _ in range(20):
            await asyncio.sleep(0.05)
            if disconnect_calls:
                break
        assert len(disconnect_calls) == 1, (
            f"expected on_disconnect to fire once after socket close, "
            f"got {len(disconnect_calls)} call(s)"
        )
        # Latch guarantees no second fire even if both loops trip.
        await asyncio.sleep(0.1)
        assert len(disconnect_calls) == 1
        await exec_.close()
    finally:
        # fake already stopped above; idempotent in case of early exit
        await fake.stop()


@pytest.mark.asyncio
async def test_on_disconnect_does_not_fire_on_normal_close():
    """A user-initiated ``close()`` must not look like a disconnect —
    that path cancels the read loop with CancelledError, which is a
    legitimate shutdown signal, not a socket drop. Firing on_disconnect
    here would race the supervisor's own teardown and re-spawn a redial
    we don't want.
    """
    fake = FakeSwitch()
    await fake.start()

    disconnect_calls: list[int] = []

    async def on_disconnect() -> None:
        disconnect_calls.append(1)

    try:
        exec_ = le.DreadExecutor(
            host="127.0.0.1", port=fake.port, on_disconnect=on_disconnect,
        )
        await exec_.connect()
        await exec_.close()  # the path under test
        await asyncio.sleep(0.1)
        assert disconnect_calls == []
    finally:
        await fake.stop()
