"""Async wire-layer wrapper for an accepted exlaunch Lua-eval socket.

Replaces ``DreadExecutor``'s former role. The PC no longer dials the
Switch — under the SMO-style inverted topology, the Switch dials the PC
via UDP-discovery + TCP-connect. ``SwitchServer`` accepts the inbound
connection and hands ``(reader, writer)`` to a context callback, which
wraps them in a :class:`DreadConnection`.

Lifecycle:

    conn = DreadConnection(reader, writer, on_push=ctx._on_switch_push)
    api  = await conn.setup()            # handshake + RL.Version probe
    # caller runs bootstrap + poll-loop wiring
    await conn.run_until_disconnect()    # blocks until socket dies
    await conn.close()

Push messages from the Switch (NEW_INVENTORY, COLLECTED_INDICES, …)
dispatch through the ``on_push`` callback; replies to ``run_lua``
fulfil the in-flight future. Reply matching is positional — an asyncio
Lock around ``run_lua`` guarantees at most one in-flight request.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Optional

from . import lua_packets as lp

logger = logging.getLogger(__name__)

DRAIN_TIMEOUT = 30.0
READ_TIMEOUT = 15.0
KEEP_ALIVE_INTERVAL = 2.0

API_VERSION_QUERY = (
    "return string.format('%d,%d,%s,%s,%s', RL.Version, RL.BufferSize,"
    "tostring(RL.Bootstrap), Init.sLayoutUUID, GameVersion)"
)


@dataclass
class ApiInfo:
    api_version: int
    buffer_size: int
    bootstrap: bool
    layout_uuid: str
    game_version: str

    @classmethod
    def parse(cls, payload: bytes) -> "ApiInfo":
        api_version, buffer_size, bootstrap, layout_uuid, game_version = (
            payload.decode("ascii").split(",", 4)
        )
        return cls(
            api_version=int(api_version),
            buffer_size=int(buffer_size),
            bootstrap=(bootstrap.lower() == "true"),
            layout_uuid=layout_uuid,
            game_version=game_version,
        )


PushHandler = Callable[[lp.PacketType, lp.Response], Awaitable[None]]


@dataclass
class DreadConnection:
    """One accepted exlaunch TCP connection.

    Constructed by ``SwitchServer`` per-accept and handed to context for
    setup + lifecycle. The reader/writer come pre-connected — this class
    never dials.
    """
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    on_push: Optional[PushHandler] = None

    _exec_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _pending_reply: Optional[asyncio.Future[lp.Response]] = field(default=None, init=False)
    _read_task: Optional[asyncio.Task[None]] = field(default=None, init=False)
    _keep_alive_task: Optional[asyncio.Task[None]] = field(default=None, init=False)
    api: Optional[ApiInfo] = field(default=None, init=False)
    _peer: str = field(default="?", init=False)

    def __post_init__(self) -> None:
        peer = self.writer.get_extra_info("peername")
        if peer:
            self._peer = f"{peer[0]}:{peer[1]}" if len(peer) >= 2 else str(peer)

    @property
    def peer(self) -> str:
        return self._peer

    async def setup(self) -> ApiInfo:
        """Run the handshake + API-version probe inline (no read loop yet).

        Mirrors the legacy ``DreadExecutor.connect()`` post-dial sequence:
        the Switch's exlaunch ROM ships only ``RL.*`` stubs, so the API
        probe runs against a stub Lua state and just returns ``nil`` for
        ``RL.Bootstrap`` etc. The caller follows up with
        :meth:`_send_bootstrap` to install the real RL namespace.
        """
        logger.info("Switch connected from %s", self._peer)

        # PACKET_HANDSHAKE — exlaunch responds with [0x01][req_num].
        self.writer.write(lp.encode_handshake(lp.ClientInterest.MULTIWORLD))
        await asyncio.wait_for(self.writer.drain(), timeout=DRAIN_TIMEOUT)
        h_type, h_resp = await self._read_frame()
        if h_type != lp.PacketType.HANDSHAKE:
            raise RuntimeError(
                f"expected HANDSHAKE ack from Switch, got "
                f"{h_type.name} (0x{h_type.value:02x})"
            )
        logger.debug("handshake ack received from %s", self._peer)

        # API version probe — reply is REMOTE_LUA_EXEC.
        self.writer.write(lp.encode_lua_exec(API_VERSION_QUERY))
        await asyncio.wait_for(self.writer.drain(), timeout=DRAIN_TIMEOUT)
        a_type, a_resp = await self._read_frame()
        if a_type != lp.PacketType.REMOTE_LUA_EXEC:
            raise RuntimeError(
                f"expected REMOTE_LUA_EXEC reply for API probe, got {a_type.name}"
            )
        if not a_resp.success:
            raise RuntimeError(f"API version probe failed: {a_resp.payload!r}")
        self.api = ApiInfo.parse(a_resp.payload)
        logger.info(
            "Switch %s api=%d buf=%d bootstrap=%s game=%s layout=%s",
            self._peer,
            self.api.api_version, self.api.buffer_size, self.api.bootstrap,
            self.api.game_version, self.api.layout_uuid,
        )

        # Start background tasks — from here on, all reads happen on the
        # read loop, and replies fulfil ``self._pending_reply``.
        self._read_task = asyncio.create_task(
            self._read_loop(), name=f"dread-conn-read[{self._peer}]")
        self._keep_alive_task = asyncio.create_task(
            self._keep_alive_loop(), name=f"dread-conn-keepalive[{self._peer}]")
        return self.api

    async def run_until_disconnect(self) -> None:
        """Block until the read loop exits (socket closed or torn).

        The keep-alive loop is allowed to outlive the read loop briefly —
        ``close()`` cancels both. We only wait on the read task because a
        keep-alive failure is downstream of a read failure (a half-open
        peer surfaces on the next write).
        """
        if self._read_task is None:
            return
        try:
            await self._read_task
        except asyncio.CancelledError:
            raise

    async def run_lua(self, source: str) -> lp.Response:
        """Send a Lua-exec request and await the immediate reply."""
        if self.writer.is_closing():
            raise RuntimeError("connection is closing")
        async with self._exec_lock:
            loop = asyncio.get_running_loop()
            self._pending_reply = loop.create_future()
            self.writer.write(lp.encode_lua_exec(source))
            await asyncio.wait_for(self.writer.drain(), timeout=DRAIN_TIMEOUT)
            try:
                return await asyncio.wait_for(self._pending_reply, timeout=READ_TIMEOUT)
            finally:
                self._pending_reply = None

    async def close(self) -> None:
        if self._read_task:
            self._read_task.cancel()
        if self._keep_alive_task:
            self._keep_alive_task.cancel()
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except OSError:
            pass
        self._read_task = self._keep_alive_task = None

    # ---- internals -----------------------------------------------------

    async def _read_frame(self) -> tuple[lp.PacketType, lp.Response]:
        """Read one whole Switch→PC frame off the wire.

        Returns ``(packet_type, response)``. Dispatches on the leading
        type byte: reply types yield a populated Response; push types
        also yield a Response with synthesized ``success=True``."""
        type_byte = await asyncio.wait_for(
            self.reader.readexactly(1), timeout=READ_TIMEOUT,
        )
        try:
            ptype = lp.PacketType(type_byte[0])
        except ValueError:
            raise OSError(f"unknown PacketType byte 0x{type_byte[0]:02x} from Switch")

        if ptype == lp.PacketType.HANDSHAKE:
            await asyncio.wait_for(
                self.reader.readexactly(lp.HANDSHAKE_REPLY_TAIL),
                timeout=READ_TIMEOUT,
            )
            return ptype, lp.Response(success=True, payload=b"")

        if ptype == lp.PacketType.REMOTE_LUA_EXEC:
            header = await asyncio.wait_for(
                self.reader.readexactly(lp.LUA_EXEC_REPLY_HEADER),
                timeout=READ_TIMEOUT,
            )
            success, length = lp.parse_lua_exec_reply_header(header)
            payload = b""
            if length:
                payload = await asyncio.wait_for(
                    self.reader.readexactly(length), timeout=READ_TIMEOUT
                )
            return ptype, lp.Response(success=success, payload=payload)

        if ptype in lp.PUSH_TYPES:
            len_field = await asyncio.wait_for(
                self.reader.readexactly(lp.PUSH_LENGTH_PREFIX),
                timeout=READ_TIMEOUT,
            )
            length = lp.parse_push_length(len_field)
            payload = b""
            if length:
                payload = await asyncio.wait_for(
                    self.reader.readexactly(length), timeout=READ_TIMEOUT
                )
            return ptype, lp.Response(success=True, payload=payload)

        if ptype == lp.PacketType.MALFORMED:
            body = await asyncio.wait_for(
                self.reader.readexactly(lp.MALFORMED_BODY),
                timeout=READ_TIMEOUT,
            )
            failing_type, received, should = lp.parse_malformed_body(body)
            logger.warning(
                "Switch reported MALFORMED: failing_type=0x%02x received=%d should=%d",
                failing_type, received, should,
            )
            return ptype, lp.Response(success=False, payload=body)

        # KEEP_ALIVE is PC→Switch only; we should never receive it.
        raise OSError(
            f"unexpected packet type from Switch: {ptype.name} (0x{ptype.value:02x})"
        )

    async def _read_loop(self) -> None:
        try:
            while True:
                ptype, resp = await self._read_frame()
                if ptype in lp.REPLY_TYPES:
                    pending = self._pending_reply
                    if pending is not None and not pending.done():
                        pending.set_result(resp)
                    else:
                        logger.warning(
                            "received %s with no waiter; ignoring", ptype.name
                        )
                    continue
                if self.on_push is not None:
                    try:
                        await self.on_push(ptype, resp)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "on_push handler raised for %s: %s", ptype.name, exc
                        )
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError,
                asyncio.TimeoutError, OSError) as exc:
            logger.info("read loop %s exiting: %s", self._peer, exc)
            pending = self._pending_reply
            if pending is not None and not pending.done():
                pending.set_exception(exc)

    async def _keep_alive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(KEEP_ALIVE_INTERVAL)
                if self.writer.is_closing():
                    return
                async with self._exec_lock:
                    self.writer.write(lp.encode_keep_alive())
                    await asyncio.wait_for(self.writer.drain(), timeout=DRAIN_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, asyncio.TimeoutError, OSError) as exc:
            logger.info("keep-alive loop %s exiting: %s", self._peer, exc)
