"""Asyncio TCP server that accepts inbound exlaunch connections.

Counterpart to the dial-side ``DreadExecutor`` that used to live in
``lua_executor.py``. With the SMO-style inverted topology, the Switch
discovers the PC via UDP and TCP-dials it. The PC just listens.

Single-active-connection model:
  * Every accept becomes the active connection.
  * If a previous connection is still attached when a new one arrives,
    the previous writer is force-closed; its accept handler unwinds and
    fires ``on_disconnect``.
  * The active connection's handler callback runs the entire session —
    handshake, RL bootstrap, poll loop, and the per-frame demux — and
    only returns when the wire dies. ``SwitchServer`` keeps accepting
    in the meantime.

Aggressive TCP keepalive (10 s idle / 2 s interval / 5 probes) surfaces
a dead Wi-Fi link in ~20 s instead of hanging on the OS-default ~2 h.

Lifted in shape from ``smo_archipelago/apworld/smo_archipelago/client/
switch_server.py``; trimmed for dread (no HELLO/replay protocol, no
multi-Switch routing, no version policing — those concerns either don't
apply or live in :class:`DreadConnection`).
"""
from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from typing import Optional

log = logging.getLogger(__name__)


def _enable_tcp_keepalive(writer: asyncio.StreamWriter) -> None:
    """Force aggressive TCP keepalive on an accepted Switch socket.

    Windows' default keepalive is 2h idle, which is far too slow for the
    bridge to detect a half-open writer. When a Wi-Fi blip leaves the
    PC's side of a connection alive while the Switch's side is gone,
    ``is_closing()`` stays False until the OS finally times out, and
    every new Switch connection takes over from the previous live one
    in a self-sustaining storm. Shorten to idle=10s, interval=2s,
    probes=5 so dead writers surface in ~20s.

    Uses the TCP_KEEP{IDLE,INTVL,CNT} setsockopt surface — supported on
    Linux, macOS (idle via TCP_KEEPALIVE), and Windows 10 1709+ with
    Python 3.12+.
    """
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    for attr, value in (
        ("TCP_KEEPIDLE", 10),
        ("TCP_KEEPINTVL", 2),
        ("TCP_KEEPCNT", 5),
        ("TCP_KEEPALIVE", 10),  # macOS spelling for idle
    ):
        opt = getattr(socket, attr, None)
        if opt is None:
            continue
        try:
            sock.setsockopt(socket.IPPROTO_TCP, opt, value)
        except OSError:
            pass


# (reader, writer, peer) -> Awaitable that runs the full session and
# returns when the connection dies. SwitchServer awaits it; on return the
# accept slot is free for the next inbound connect.
ConnectionHandler = Callable[
    [asyncio.StreamReader, asyncio.StreamWriter, str],
    Awaitable[None],
]


class SwitchServer:
    """TCP listener for inbound Switch connections."""

    def __init__(
        self,
        host: str,
        port: int,
        on_connection: ConnectionHandler,
    ) -> None:
        self._host = host
        self._port = port
        self._on_connection = on_connection
        self._server: Optional[asyncio.AbstractServer] = None
        # The currently-active writer. New connect closes this if set.
        self._active_writer: Optional[asyncio.StreamWriter] = None
        # The currently-active handler task; tracked so stop() can wait for it.
        self._active_task: Optional[asyncio.Task[None]] = None

    @property
    def actual_port(self) -> int:
        """Bound port (resolves :0 to a real ephemeral port post-start)."""
        if self._server is None or not self._server.sockets:
            return self._port
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._port,
        )
        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        log.info("switch server listening on %s", addrs)

    async def stop(self) -> None:
        """Tear down the listener + any in-flight connection."""
        old_writer = self._active_writer
        self._active_writer = None
        if old_writer is not None:
            try:
                old_writer.close()
            except Exception:
                pass
        if self._active_task is not None and not self._active_task.done():
            try:
                await asyncio.wait_for(self._active_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._active_task.cancel()
            except Exception:
                pass
        self._active_task = None
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=2.0)
            except (asyncio.TimeoutError, Exception):
                pass
            self._server = None

    async def disconnect_active(self) -> None:
        """Force-close the active connection so the Switch redials.

        Wired to ``/dread_connect`` and the GUI's "Disconnect" button.
        No-op if there is no active connection.
        """
        w = self._active_writer
        if w is None:
            return
        log.info("forcing active Switch disconnect (Switch will redial)")
        try:
            w.close()
        except Exception:
            pass

    def is_connected(self) -> bool:
        w = self._active_writer
        return w is not None and not w.is_closing()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer_info = writer.get_extra_info("peername")
        peer = (
            f"{peer_info[0]}:{peer_info[1]}"
            if peer_info and len(peer_info) >= 2
            else "?"
        )
        _enable_tcp_keepalive(writer)
        log.info("switch connecting from %s", peer)

        # Supersede any prior active connection. The previous handler's
        # read loop will see EOF and unwind in its own finally block.
        prev_writer = self._active_writer
        prev_task = self._active_task
        if prev_writer is not None:
            log.info("superseding previous Switch connection")
            try:
                prev_writer.close()
            except Exception:
                pass
            if prev_task is not None and not prev_task.done():
                try:
                    await asyncio.wait_for(prev_task, timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass

        self._active_writer = writer
        # Track the handler task so stop() / supersede can wait for it.
        # We can't reference our own coroutine task from here, so we
        # capture it as soon as it's scheduled. The handler IS this
        # coroutine; reach it via current_task().
        self._active_task = asyncio.current_task()

        try:
            await self._on_connection(reader, writer, peer)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("on_connection handler raised for %s", peer)
        finally:
            # Only clear `self._active_*` if we're STILL the active
            # connection — a faster supersede may have replaced us.
            if self._active_writer is writer:
                self._active_writer = None
                self._active_task = None
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            log.info("switch %s disconnected", peer)
