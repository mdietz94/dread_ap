"""Async TCP client for the exlaunch Lua-eval socket on the Switch.

Replaces SMO's ``switch_server.py`` (which was a TCP *server* on the PC
that the Switch dialed into). For Dread the topology is inverted: the
Switch listens, the PC dials.

Lifecycle:

    executor = DreadExecutor(host="192.168.1.42")
    await executor.connect()  # opens TCP, handshakes, runs API version probe
    inv_json = await executor.run_lua("RL.GetInventoryAndSend(); return 1")
    await executor.close()

Push messages from the Switch (NEW_INVENTORY, COLLECTED_INDICES, etc.) are
dispatched through ``on_push`` callbacks; reply messages from Lua-exec
requests are returned by ``run_lua``.

Every Switch→PC frame begins with a 1-byte ``PacketType``. We dispatch on
that byte: reply types (HANDSHAKE, REMOTE_LUA_EXEC) fulfill the pending
future, push types route to ``on_push``. The protocol does not embed
request IDs in user-visible terms (the exlaunch side does echo a
``request_number``, which we read and discard — see lua_packets), so
reply matching is positional and we serialize Lua-exec requests through
an asyncio Lock.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Optional

from . import lua_packets as lp

logger = logging.getLogger(__name__)

DEFAULT_PORT = 6969
DRAIN_TIMEOUT = 30.0
READ_TIMEOUT = 15.0
CONNECT_TIMEOUT = 10.0
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
class DreadExecutor:
    host: str
    port: int = DEFAULT_PORT
    on_push: Optional[PushHandler] = None

    _reader: Optional[asyncio.StreamReader] = field(default=None, init=False)
    _writer: Optional[asyncio.StreamWriter] = field(default=None, init=False)
    _exec_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _pending_reply: Optional[asyncio.Future[lp.Response]] = field(default=None, init=False)
    _read_task: Optional[asyncio.Task[None]] = field(default=None, init=False)
    _keep_alive_task: Optional[asyncio.Task[None]] = field(default=None, init=False)
    api: Optional[ApiInfo] = field(default=None, init=False)

    async def connect(self) -> ApiInfo:
        logger.info("Dialing %s:%d", self.host, self.port)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=CONNECT_TIMEOUT
        )

        # Handshake (no read loop running yet, so read inline).
        self._writer.write(lp.encode_handshake(lp.ClientInterest.MULTIWORLD))
        await asyncio.wait_for(self._writer.drain(), timeout=DRAIN_TIMEOUT)
        h_type, h_resp = await self._read_frame()
        if h_type != lp.PacketType.HANDSHAKE:
            raise RuntimeError(
                f"expected HANDSHAKE ack from Switch, got {h_type.name} (0x{h_type.value:02x})"
            )
        logger.debug("handshake ack received")

        # API version probe — also inline; read loop starts after this.
        self._writer.write(lp.encode_lua_exec(API_VERSION_QUERY))
        await asyncio.wait_for(self._writer.drain(), timeout=DRAIN_TIMEOUT)
        a_type, a_resp = await self._read_frame()
        if a_type != lp.PacketType.REMOTE_LUA_EXEC:
            raise RuntimeError(
                f"expected REMOTE_LUA_EXEC reply for API probe, got {a_type.name}"
            )
        if not a_resp.success:
            raise RuntimeError(f"API version probe failed: {a_resp.payload!r}")
        self.api = ApiInfo.parse(a_resp.payload)
        logger.info("connected: api=%d buf=%d bootstrap=%s game=%s layout=%s",
                    self.api.api_version, self.api.buffer_size, self.api.bootstrap,
                    self.api.game_version, self.api.layout_uuid)

        # Start background tasks
        self._read_task = asyncio.create_task(self._read_loop(), name="dread-exec-read")
        self._keep_alive_task = asyncio.create_task(self._keep_alive_loop(), name="dread-exec-keepalive")
        return self.api

    async def run_lua(self, source: str) -> lp.Response:
        """Send a Lua-exec request and await the immediate reply."""
        if self._writer is None:
            raise RuntimeError("not connected")
        async with self._exec_lock:
            loop = asyncio.get_running_loop()
            self._pending_reply = loop.create_future()
            self._writer.write(lp.encode_lua_exec(source))
            await asyncio.wait_for(self._writer.drain(), timeout=DRAIN_TIMEOUT)
            try:
                return await asyncio.wait_for(self._pending_reply, timeout=READ_TIMEOUT)
            finally:
                self._pending_reply = None

    async def close(self) -> None:
        if self._read_task:
            self._read_task.cancel()
        if self._keep_alive_task:
            self._keep_alive_task.cancel()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except OSError:
                pass
        self._reader = self._writer = None
        self._read_task = self._keep_alive_task = None

    # ---- internals -----------------------------------------------------

    async def _read_frame(self) -> tuple[lp.PacketType, lp.Response]:
        """Read one whole Switch→PC frame off the wire.

        Returns ``(packet_type, response)``. Dispatches on the leading
        type byte: reply types yield a populated Response; push types
        also yield a Response with synthesized ``success=True``."""
        assert self._reader is not None
        type_byte = await asyncio.wait_for(self._reader.readexactly(1),
                                           timeout=READ_TIMEOUT)
        try:
            ptype = lp.PacketType(type_byte[0])
        except ValueError:
            raise OSError(f"unknown PacketType byte 0x{type_byte[0]:02x} from Switch")

        if ptype == lp.PacketType.HANDSHAKE:
            # [0x01][req_num] — body is just one request_number byte
            await asyncio.wait_for(
                self._reader.readexactly(lp.HANDSHAKE_REPLY_TAIL),
                timeout=READ_TIMEOUT,
            )
            return ptype, lp.Response(success=True, payload=b"")

        if ptype == lp.PacketType.REMOTE_LUA_EXEC:
            # [0x03][req_num][success][len:3 LE u24][payload]
            header = await asyncio.wait_for(
                self._reader.readexactly(lp.LUA_EXEC_REPLY_HEADER),
                timeout=READ_TIMEOUT,
            )
            success, length = lp.parse_lua_exec_reply_header(header)
            payload = b""
            if length:
                payload = await asyncio.wait_for(
                    self._reader.readexactly(length), timeout=READ_TIMEOUT
                )
            return ptype, lp.Response(success=success, payload=payload)

        if ptype in lp.PUSH_TYPES:
            # [type][len:4 LE u32][payload]
            len_field = await asyncio.wait_for(
                self._reader.readexactly(lp.PUSH_LENGTH_PREFIX),
                timeout=READ_TIMEOUT,
            )
            length = lp.parse_push_length(len_field)
            payload = b""
            if length:
                payload = await asyncio.wait_for(
                    self._reader.readexactly(length), timeout=READ_TIMEOUT
                )
            return ptype, lp.Response(success=True, payload=payload)

        if ptype == lp.PacketType.MALFORMED:
            # [0x09][failing_type][rcv:4][should:4]
            body = await asyncio.wait_for(
                self._reader.readexactly(lp.MALFORMED_BODY),
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
                    # Replies fulfil the in-flight future. The exec lock
                    # guarantees at most one is pending at a time.
                    pending = self._pending_reply
                    if pending is not None and not pending.done():
                        pending.set_result(resp)
                    else:
                        logger.warning(
                            "received %s with no waiter; ignoring", ptype.name
                        )
                    continue
                # Push (or MALFORMED) — route to handler if registered.
                if self.on_push is not None:
                    try:
                        await self.on_push(ptype, resp)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "on_push handler raised for %s: %s", ptype.name, exc
                        )
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.TimeoutError, OSError) as exc:
            logger.warning("read loop exiting: %s", exc)
            pending = self._pending_reply
            if pending is not None and not pending.done():
                pending.set_exception(exc)

    async def _keep_alive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(KEEP_ALIVE_INTERVAL)
                if self._writer is None:
                    return
                # Take the exec lock so we don't interleave a keep-alive
                # mid-Lua-request (which would confuse the FIFO reader).
                async with self._exec_lock:
                    self._writer.write(lp.encode_keep_alive())
                    await asyncio.wait_for(self._writer.drain(), timeout=DRAIN_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, asyncio.TimeoutError) as exc:
            logger.warning("keep-alive loop exiting: %s", exc)
