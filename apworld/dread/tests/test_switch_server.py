"""Unit tests for the TCP SwitchServer (PC-side passive listener)."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client.switch_server import SwitchServer  # noqa: E402


class _Recorder:
    """Captures every on_connection invocation and tracks lifetime."""

    def __init__(self):
        self.calls: list[tuple[str, asyncio.Event]] = []
        self.live: int = 0
        self.max_live: int = 0
        # If set, the handler awaits this event before returning. Lets
        # tests gate the connection's lifecycle deterministically.
        self.release: asyncio.Event = asyncio.Event()

    async def handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: str,
    ) -> None:
        my_event = asyncio.Event()
        self.calls.append((peer, my_event))
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        try:
            # Block until either the test releases us OR the socket dies.
            done = asyncio.create_task(self.release.wait())
            read = asyncio.create_task(reader.read(1))
            await asyncio.wait(
                [done, read],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in (done, read):
                if not t.done():
                    t.cancel()
        finally:
            self.live -= 1
            my_event.set()


@pytest.mark.asyncio
async def test_listener_accepts_one_connection():
    rec = _Recorder()
    server = SwitchServer(host="127.0.0.1", port=0, on_connection=rec.handle)
    await server.start()
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", server.actual_port)
        try:
            # Give the accept handler a beat to register.
            for _ in range(20):
                await asyncio.sleep(0.01)
                if rec.calls:
                    break
            assert len(rec.calls) == 1
            assert server.is_connected()
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_second_connection_supersedes_the_first():
    rec = _Recorder()
    server = SwitchServer(host="127.0.0.1", port=0, on_connection=rec.handle)
    await server.start()
    try:
        # First connection.
        r1, w1 = await asyncio.open_connection("127.0.0.1", server.actual_port)
        for _ in range(20):
            await asyncio.sleep(0.01)
            if len(rec.calls) >= 1:
                break
        assert len(rec.calls) == 1

        # Second connection — should supersede the first.
        r2, w2 = await asyncio.open_connection("127.0.0.1", server.actual_port)
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(rec.calls) >= 2 and rec.live <= 1:
                break
        assert len(rec.calls) == 2
        # The supersede closes the first writer; the recorder's `live`
        # counter drops back to 1.
        assert rec.live == 1

        for w in (w1, w2):
            try:
                w.close()
                await w.wait_closed()
            except Exception:
                pass
        del r1, r2
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_disconnect_active_force_closes_writer():
    """``disconnect_active()`` closes the writer of the currently-active
    connection so the (real) Switch sysmodule sees EOF and redials."""
    rec = _Recorder()
    server = SwitchServer(host="127.0.0.1", port=0, on_connection=rec.handle)
    await server.start()
    try:
        r, w = await asyncio.open_connection("127.0.0.1", server.actual_port)
        for _ in range(20):
            await asyncio.sleep(0.01)
            if rec.calls:
                break
        assert server.is_connected()

        await server.disconnect_active()
        # Client side sees EOF.
        data = await asyncio.wait_for(r.read(1), timeout=1.0)
        assert data == b""

        try:
            w.close()
            await w.wait_closed()
        except Exception:
            pass
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_disconnect_active_is_noop_when_no_connection():
    rec = _Recorder()
    server = SwitchServer(host="127.0.0.1", port=0, on_connection=rec.handle)
    await server.start()
    try:
        # Never accepted anything yet — must not raise.
        await server.disconnect_active()
        assert not server.is_connected()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_stop_closes_active_connection_and_listener():
    rec = _Recorder()
    server = SwitchServer(host="127.0.0.1", port=0, on_connection=rec.handle)
    await server.start()
    r, w = await asyncio.open_connection("127.0.0.1", server.actual_port)
    for _ in range(20):
        await asyncio.sleep(0.01)
        if rec.calls:
            break
    assert rec.live == 1

    await server.stop()
    # Client sees EOF after stop, and a new connect attempt fails.
    data = await asyncio.wait_for(r.read(1), timeout=1.0)
    assert data == b""
    try:
        w.close()
        await w.wait_closed()
    except Exception:
        pass
    with pytest.raises((ConnectionRefusedError, OSError)):
        await asyncio.open_connection("127.0.0.1", server.actual_port)
