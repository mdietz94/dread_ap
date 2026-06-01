"""Async TCP server that accepts Switch dial-ins.

Replaces the old ``lua_executor.py`` (which dialed *out* to the Switch).
The Switch is now the dialer; we bind ``0.0.0.0:17777`` and accept
multiple concurrent connections, designating one as **active** (drives
the AP slot) and ``kick``ing the rest as **inactive** (registered but
silenced; auto-promoted if the active drops).

Wire is line-delimited JSON; envelope dataclasses live in :mod:`.wire`.

Public surface (mirrors the old ``DreadExecutor`` so :mod:`.context`
edits stay minimal):

    server = BridgeServer(slot=ctx.username, seed=ctx.state.seed,
                          on_push=ctx._on_switch_push,
                          on_active_connected=ctx._on_switch_ready,
                          on_active_disconnected=ctx._on_switch_gone)
    await server.start()
    resp = await server.run_lua("RL.GetCollectedIndicesAndSend()")
    await server.close()

Per-Switch state lives in :class:`_SwitchConn`. The active Switch's
writer is used by ``run_lua`` to ship ``LuaExec`` lines and by ``on_push``
to fan out unsolicited pushes (``inventory``, ``collected``, etc).
``LuaExecReply`` is matched to the awaiting future by ``seq``.

Lifted structurally from
``smo_archipelago/apworld/smo_archipelago/client/switch_server.py`` —
the multi-Switch promotion logic, ``_resolve_device_id`` fallback, and
post-HELLO replay pattern translate directly.
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Optional

from . import wire as W

logger = logging.getLogger(__name__)

DEFAULT_PORT = 17777
READ_TIMEOUT = 15.0
DRAIN_TIMEOUT = 30.0
# Scenario loads on the Switch can stall the game thread (and therefore our
# game-thread Lua tick) for >15 s during initial cave load. The first poll
# after bootstrap routinely tripped a 15 s timeout, and when the reply
# finally arrived the future was already cleaned up. 45 s comfortably
# covers a cold s010_cave load without changing the steady-state behavior.
LUA_EXEC_TIMEOUT = 45.0


# Callbacks the owning context can set. All optional.
PushHandler = Callable[[Any], Awaitable[None]]
ActiveConnectedHandler = Callable[["ActiveConnInfo"], Awaitable[None]]
ActiveDisconnectedHandler = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class ActiveConnInfo:
    """Snapshot handed to ``on_active_connected`` callbacks."""
    device_id: str
    peer_ip: str
    mod_ver: str
    dread_ver: str
    layout_uuid: str


@dataclass
class _SwitchConn:
    device_id: str
    peer_ip: str
    writer: asyncio.StreamWriter
    hello: W.Hello
    is_active: bool
    last_seen_ms: float
    writer_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Per-connection in-flight lua-exec serialization. Even though seq lets
    # the wire carry multiple in-flight, the Switch's game thread processes
    # them one at a time. Holding this lock around run_lua keeps the wire
    # FIFO and gives the timeout a clean boundary.
    exec_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BridgeServer:
    """Multi-Switch TCP server for the Dread bridge."""

    def __init__(
        self,
        *,
        slot: str = "",
        seed: str = "",
        expected_mod_ver: str = "",
        host: str = "0.0.0.0",
        port: int = DEFAULT_PORT,
        on_push: Optional[PushHandler] = None,
        on_active_connected: Optional[ActiveConnectedHandler] = None,
        on_active_disconnected: Optional[ActiveDisconnectedHandler] = None,
    ) -> None:
        self._slot = slot
        self._seed = seed
        self._expected_mod_ver = expected_mod_ver
        self._host = host
        self._port = port
        self.on_push = on_push
        self.on_active_connected = on_active_connected
        self.on_active_disconnected = on_active_disconnected

        self._connections: dict[str, _SwitchConn] = {}
        self._active_device_id: Optional[str] = None
        self._pending_replies: dict[int, asyncio.Future[W.LuaExecReply]] = {}
        self._seq_counter = itertools.count(1)
        self._server: Optional[asyncio.AbstractServer] = None
        self._registry_lock = asyncio.Lock()

    # ---- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        """Bind and start accepting. Raises OSError on bind failure — the
        port-collision case is reported loudly per the plan; the caller
        should surface this to the user."""
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._port,
        )
        logger.info("BridgeServer listening on %s:%d (slot=%r seed=%r)",
                    self._host, self._port, self._slot, self._seed)

    async def close(self) -> None:
        """Tear down all connections and the listener."""
        # Snapshot conns under the lock; close outside.
        async with self._registry_lock:
            conns = list(self._connections.values())
            self._connections.clear()
            self._active_device_id = None
        for c in conns:
            try:
                c.writer.close()
                await c.writer.wait_closed()
            except OSError:
                pass
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        # Resolve any pending replies with a cancellation so callers don't
        # hang forever after close().
        for fut in list(self._pending_replies.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("BridgeServer closed"))
        self._pending_replies.clear()

    # ---- public API ----------------------------------------------------

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    def is_connected(self) -> bool:
        return self._active_device_id is not None

    def active_device_id(self) -> Optional[str]:
        return self._active_device_id

    def list_switches(self) -> list[dict[str, Any]]:
        """For GUI / status-command rendering. Returns a snapshot, not a live view."""
        now = time.monotonic() * 1000
        out: list[dict[str, Any]] = []
        for c in self._connections.values():
            out.append({
                "device_id": c.device_id,
                "peer_ip": c.peer_ip,
                "active": c.is_active,
                "last_seen_ms": c.last_seen_ms,
                "stale_ms": now - c.last_seen_ms,
                "mod_ver": c.hello.mod_ver,
                "dread_ver": c.hello.dread_ver,
                "layout_uuid": c.hello.layout_uuid,
            })
        out.sort(key=lambda d: (not d["active"], d["device_id"]))
        return out

    async def run_lua(self, source: str) -> W.Response:
        """Send a ``LuaExec`` to the active Switch and await the reply.

        Raises ``RuntimeError`` if no active Switch is connected — callers
        should treat this as "wait for the next on_active_connected" rather
        than retry-with-backoff."""
        conn = self._active_conn()
        if conn is None:
            raise RuntimeError("no active Switch connected")
        async with conn.exec_lock:
            seq = next(self._seq_counter)
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[W.LuaExecReply] = loop.create_future()
            self._pending_replies[seq] = fut
            try:
                await self._send_to(conn, W.LuaExec(seq=seq, src=source))
                reply = await asyncio.wait_for(fut, timeout=LUA_EXEC_TIMEOUT)
            finally:
                self._pending_replies.pop(seq, None)
        return W.response_from_reply(reply)

    async def manual_promote(self, device_id: str) -> bool:
        """User-driven promotion: make ``device_id`` the active Switch.

        No-op if already active. Returns False if ``device_id`` isn't a
        registered conn."""
        async with self._registry_lock:
            if device_id not in self._connections:
                return False
            if self._active_device_id == device_id:
                return True
            await self._demote_active_locked(reason="manual_promote")
            await self._activate_locked(device_id, kicked_before=True)
        return True

    # ---- internals -----------------------------------------------------

    def _active_conn(self) -> Optional[_SwitchConn]:
        did = self._active_device_id
        if did is None:
            return None
        return self._connections.get(did)

    async def _send_to(self, conn: _SwitchConn, msg: Any) -> None:
        async with conn.writer_lock:
            if conn.writer.is_closing():
                raise ConnectionError(f"writer for {conn.device_id} is closing")
            try:
                conn.writer.write(W.encode(msg))
                await asyncio.wait_for(conn.writer.drain(),
                                       timeout=DRAIN_TIMEOUT)
            except (ConnectionError, asyncio.TimeoutError, OSError) as exc:
                logger.warning("send to %s failed: %s", conn.device_id, exc)
                raise

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        peer_ip = "?"
        try:
            peer = writer.get_extra_info("peername")
            if peer:
                peer_ip = str(peer[0])
        except Exception:
            pass
        logger.info("new TCP from %s", peer_ip)

        buffer = bytearray()
        # Phase 1: read until HELLO arrives (or EOF/timeout).
        hello_msg: Optional[W.Hello] = None
        try:
            while hello_msg is None:
                chunk = await asyncio.wait_for(reader.read(4096),
                                               timeout=READ_TIMEOUT)
                if not chunk:
                    raise ConnectionError("peer closed before HELLO")
                buffer.extend(chunk)
                for line in W.iter_lines(buffer):
                    try:
                        d = W.decode(line)
                    except Exception:
                        logger.warning("pre-HELLO malformed line from %s: %r",
                                       peer_ip, line[:120])
                        continue
                    parsed = W.from_dict(d)
                    if isinstance(parsed, W.Hello):
                        hello_msg = parsed
                        break
                    else:
                        logger.warning(
                            "pre-HELLO non-Hello message from %s: t=%r; ignoring",
                            peer_ip, d.get("t"))
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError) as exc:
            logger.info("dropping %s before HELLO: %s", peer_ip, exc)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return

        # Phase 2: validate, register, decide active/inactive.
        device_id = self._resolve_device_id(hello_msg.device_id, peer_ip)
        is_active = False
        conn = _SwitchConn(
            device_id=device_id,
            peer_ip=peer_ip,
            writer=writer,
            hello=hello_msg,
            is_active=False,
            last_seen_ms=time.monotonic() * 1000,
        )

        version_ok = self._mod_ver_ok(hello_msg.mod_ver)
        if not version_ok:
            ack = W.HelloAck(
                ok=False,
                slot=self._slot,
                seed=self._seed,
                err=(f"mod_ver mismatch: got {hello_msg.mod_ver!r}, "
                     f"expected {self._expected_mod_ver!r}"),
            )
            await self._send_to(conn, ack)
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            logger.warning("rejected %s (device=%s) on mod_ver mismatch: %r",
                           peer_ip, device_id, hello_msg.mod_ver)
            return

        async with self._registry_lock:
            # Replace stale registration with same device_id (e.g. reconnect)
            existing = self._connections.get(device_id)
            if existing is not None:
                logger.info("replacing stale conn for device=%s", device_id)
                try:
                    existing.writer.close()
                except OSError:
                    pass
            self._connections[device_id] = conn
            if self._active_device_id is None:
                conn.is_active = True
                self._active_device_id = device_id
                is_active = True
            else:
                conn.is_active = False
                is_active = False

        ack = W.HelloAck(ok=True, slot=self._slot, seed=self._seed)
        try:
            await self._send_to(conn, ack)
        except Exception:
            await self._unregister(device_id)
            return

        if is_active:
            logger.info("Switch %s (peer=%s) connected -> ACTIVE", device_id, peer_ip)
            if self.on_active_connected is not None:
                info = ActiveConnInfo(
                    device_id=device_id,
                    peer_ip=peer_ip,
                    mod_ver=hello_msg.mod_ver,
                    dread_ver=hello_msg.dread_ver,
                    layout_uuid=hello_msg.layout_uuid,
                )
                # CRITICAL: don't await on_active_connected on the read loop.
                # The callback (DreadContext._on_switch_ready) sends bootstrap
                # chunks via run_lua, which awaits LuaExecReply on THIS read
                # loop — deadlocking if we await here. Fire-and-forget.
                asyncio.create_task(
                    self._safe_active_connected(info),
                    name=f"on-active-connected-{device_id}",
                )
        else:
            logger.info("Switch %s (peer=%s) connected -> INACTIVE (kicking)",
                        device_id, peer_ip)
            try:
                await self._send_to(conn, W.Kick(reason="inactive"))
            except Exception:
                pass  # Kick is best-effort; we keep the conn registered
                # for visibility in the GUI even if it bounces.

        # Phase 3: dispatch loop.
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                conn.last_seen_ms = time.monotonic() * 1000
                buffer.extend(chunk)
                for line in W.iter_lines(buffer):
                    await self._dispatch(conn, line)
        except (ConnectionError, asyncio.IncompleteReadError, OSError) as exc:
            logger.info("Switch %s read loop ended: %s", device_id, exc)
        finally:
            await self._unregister(device_id)

    async def _dispatch(self, conn: _SwitchConn, line: bytes) -> None:
        try:
            d = W.decode(line)
        except Exception:
            logger.warning("malformed line from %s: %r", conn.device_id, line[:120])
            return
        parsed = W.from_dict(d)
        if parsed is None:
            logger.warning("unknown wire type from %s: t=%r", conn.device_id, d.get("t"))
            return

        if isinstance(parsed, W.LuaExecReply):
            # Only honor replies from the active conn — inactive conns
            # shouldn't be receiving lua_exec at all (their writer is
            # kicked) but defend against it.
            if not conn.is_active:
                logger.warning("lua_exec_reply from inactive %s; ignoring",
                               conn.device_id)
                return
            fut = self._pending_replies.get(parsed.seq)
            if fut is None or fut.done():
                logger.warning("lua_exec_reply seq=%d has no waiter; dropping",
                               parsed.seq)
                return
            fut.set_result(parsed)
            return

        if isinstance(parsed, W.Ping):
            try:
                await self._send_to(conn, W.Pong(ts_ms=parsed.ts_ms))
            except Exception:
                pass
            return

        if isinstance(parsed, W.LayoutUuid) and conn.is_active:
            # Update HELLO snapshot so GUI status reflects the real UUID.
            conn.hello.layout_uuid = parsed.value

        # Everything else (Log, Inventory, Collected, ReceivedPickups,
        # GameState, LayoutUuid) is a push from the active Switch. Fan
        # out to on_push.
        if not conn.is_active:
            logger.debug("ignoring push %r from inactive %s",
                         type(parsed).__name__, conn.device_id)
            return
        if self.on_push is None:
            return
        try:
            await self.on_push(parsed)
        except Exception:
            logger.exception("on_push handler raised for %r", type(parsed).__name__)

    async def _safe_active_connected(self, info: ActiveConnInfo) -> None:
        """Wrapper that swallows exceptions from on_active_connected so a
        bug in the callback never tears down the read loop / the whole
        bridge."""
        if self.on_active_connected is None:
            return
        try:
            await self.on_active_connected(info)
        except Exception:
            logger.exception("on_active_connected raised for %s", info.device_id)

    async def _unregister(self, device_id: str) -> None:
        was_active = False
        async with self._registry_lock:
            conn = self._connections.pop(device_id, None)
            if conn is None:
                return
            try:
                conn.writer.close()
            except OSError:
                pass
            if self._active_device_id == device_id:
                was_active = True
                self._active_device_id = None
            # Pick a promotion candidate while we hold the lock.
            next_did = None
            if was_active and self._connections:
                # Most-recent first.
                next_did = max(self._connections,
                               key=lambda d: self._connections[d].last_seen_ms)
                self._connections[next_did].is_active = True
                self._active_device_id = next_did
        try:
            await conn.writer.wait_closed()
        except (OSError, Exception):
            pass
        if was_active:
            # Fail any in-flight lua_exec on the old active.
            for seq, fut in list(self._pending_replies.items()):
                if not fut.done():
                    fut.set_exception(ConnectionError("active Switch disconnected"))
            self._pending_replies.clear()
            if next_did is None:
                if self.on_active_disconnected is not None:
                    try:
                        await self.on_active_disconnected()
                    except Exception:
                        logger.exception("on_active_disconnected raised")
                logger.info("active Switch %s gone; no inactives to promote",
                            device_id)
            else:
                promoted = self._connections.get(next_did)
                if promoted is not None and self.on_active_connected is not None:
                    info = ActiveConnInfo(
                        device_id=promoted.device_id,
                        peer_ip=promoted.peer_ip,
                        mod_ver=promoted.hello.mod_ver,
                        dread_ver=promoted.hello.dread_ver,
                        layout_uuid=promoted.hello.layout_uuid,
                    )
                    asyncio.create_task(
                        self._safe_active_connected(info),
                        name=f"on-active-connected-{next_did}",
                    )
                logger.info("active Switch %s gone; auto-promoted %s",
                            device_id, next_did)

    async def _demote_active_locked(self, *, reason: str) -> None:
        """Internal: called with ``_registry_lock`` held."""
        if self._active_device_id is None:
            return
        conn = self._connections.get(self._active_device_id)
        if conn is not None:
            conn.is_active = False
            try:
                await self._send_to(conn, W.Kick(reason=reason))
            except Exception:
                pass
        # Fail in-flight lua_exec.
        for fut in list(self._pending_replies.values()):
            if not fut.done():
                fut.set_exception(ConnectionError("demoted"))
        self._pending_replies.clear()
        old_active = self._active_device_id
        self._active_device_id = None
        if self.on_active_disconnected is not None:
            try:
                await self.on_active_disconnected()
            except Exception:
                logger.exception("on_active_disconnected raised on demote of %s",
                                 old_active)

    async def _activate_locked(self, device_id: str, *, kicked_before: bool) -> None:
        """Internal: called with ``_registry_lock`` held."""
        conn = self._connections.get(device_id)
        if conn is None:
            return
        conn.is_active = True
        self._active_device_id = device_id
        if self.on_active_connected is not None:
            info = ActiveConnInfo(
                device_id=conn.device_id,
                peer_ip=conn.peer_ip,
                mod_ver=conn.hello.mod_ver,
                dread_ver=conn.hello.dread_ver,
                layout_uuid=conn.hello.layout_uuid,
            )
            asyncio.create_task(
                self._safe_active_connected(info),
                name=f"on-active-connected-{device_id}",
            )

    def _resolve_device_id(self, hello_id: str, peer_ip: str) -> str:
        """If HELLO carries a device_id, use it. Else synthesize ``sw-<ip>``.

        The Switch's first HELLO often has an empty layout_uuid (read from
        Lua, not available pre-bootstrap), so its device_id may also be
        ``sw-<ip-suffix>``. The Switch can correct this later by pushing
        ``LayoutUuid``; we update conn.hello.layout_uuid then but keep the
        original device_id stable so the AP slot mapping doesn't churn.
        """
        if hello_id:
            return hello_id
        suffix = peer_ip.replace(".", "-")
        return f"sw-{suffix}"

    def _mod_ver_ok(self, got: str) -> bool:
        """If we have no expected version (e.g. development), accept anything.
        Otherwise require an exact match — the Switch must rebuild on apworld
        version bumps so the bootstrap Lua matches the Lua API the PC will
        drive."""
        if not self._expected_mod_ver:
            return True
        return got == self._expected_mod_ver
