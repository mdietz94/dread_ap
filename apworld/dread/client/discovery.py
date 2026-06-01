"""UDP bridge-discovery responder.

The Switch sysmodule sends a small JSON probe to one of:
  * ``127.0.0.1:17776`` (Ryujinx-on-same-host, tried first, 250ms cap)
  * Every host in the ``BRIDGE_HOST_STRING /24`` (unicast subnet sweep, 2s
    collect window) — see ``vendor/open-dread-rando-exlaunch/source/program/
    discovery.cpp``.

We bind UDP on ``0.0.0.0:17776``, accept either delivery path, and unicast a
reply pointing the Switch at our TCP BridgeServer. The reply's ``host`` comes
from ``detect_lan_ip()`` so the Switch always gets a routable address (even
when the probe arrived on loopback).

Wire format — newline-terminated UTF-8 JSON, matching the TCP envelope so the
Switch's JSON encoder is reusable:

    probe:  {"t":"discover","mod_ver":"<x>"}\\n
    reply:  {"t":"bridge","host":"<ipv4>","port":17777,"seed":"<seed>"}\\n

Bind failure (e.g. port already in use because a second DreadClient is
running) is logged at WARN and the responder no-ops. The Switch will retry
its discovery sweep on each backoff cycle, so a transient port collision
self-heals once the offending process exits.

Lifted from smo_archipelago/apworld/smo_archipelago/client/discovery.py;
the protocol semantics (loopback first, then unicast sweep) are identical.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import socket
import sys
from typing import Callable

from .net_util import detect_lan_ip


def _disable_udp_connreset_win32(sock: socket.socket) -> None:
    """Disable WSAECONNRESET on a UDP socket via WSAIoctl(SIO_UDP_CONNRESET).

    Python's ``socket.ioctl()`` whitelist doesn't include SIO_UDP_CONNRESET,
    so we call WSAIoctl directly via ctypes. Caller catches OSError so a
    ctypes edge case can't take down the responder.
    """
    SIO_UDP_CONNRESET = 0x9800000C
    ws2 = ctypes.WinDLL("ws2_32")
    LPDWORD = ctypes.POINTER(ctypes.c_ulong)
    ws2.WSAIoctl.argtypes = [
        ctypes.c_void_p,                      # SOCKET s
        ctypes.c_uint32,                      # DWORD dwIoControlCode
        ctypes.c_void_p, ctypes.c_uint32,     # in buf + size
        ctypes.c_void_p, ctypes.c_uint32,     # out buf + size
        LPDWORD,                              # bytes returned
        ctypes.c_void_p, ctypes.c_void_p,     # overlapped + completion
    ]
    ws2.WSAIoctl.restype = ctypes.c_int
    enable = ctypes.c_uint32(0)  # FALSE = suppress ECONNRESET on UDP
    out_size = ctypes.c_ulong(0)
    rc = ws2.WSAIoctl(
        sock.fileno(),
        SIO_UDP_CONNRESET,
        ctypes.byref(enable), 4,
        None, 0,
        ctypes.byref(out_size),
        None, None,
    )
    if rc != 0:
        ws2.WSAGetLastError.restype = ctypes.c_int
        wsa_err = ws2.WSAGetLastError()
        raise OSError(wsa_err, f"WSAIoctl(SIO_UDP_CONNRESET) failed: WSA error {wsa_err}")


log = logging.getLogger(__name__)

DEFAULT_DISCOVERY_PORT = 17776
MAX_PROBE_BYTES = 512


SeedProvider = Callable[[], str]


class _ResponderProtocol(asyncio.DatagramProtocol):
    def __init__(
        self,
        tcp_port: int,
        get_seed: SeedProvider,
        get_lan_ip: Callable[[], str] = detect_lan_ip,
    ) -> None:
        self._tcp_port = tcp_port
        self._get_seed = get_seed
        self._get_lan_ip = get_lan_ip
        self._transport: asyncio.DatagramTransport | None = None
        # Cache the LAN IP for the responder's lifetime. detect_lan_ip()
        # opens a UDP socket per call; doing it per-probe is wasteful and
        # the LAN IP doesn't change without a DreadClient restart anyway.
        self._lan_ip = self._get_lan_ip()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        log.info("discovery: probe received from %s (%d bytes)",
                 addr, len(data))
        if len(data) > MAX_PROBE_BYTES:
            log.debug("discovery: oversized probe (%d bytes) from %s; ignoring",
                      len(data), addr)
            return
        try:
            msg = json.loads(data.decode("utf-8", errors="replace"))
        except Exception:
            log.warning("discovery: malformed JSON from %s: %r",
                        addr, data[:80])
            return
        if not isinstance(msg, dict) or msg.get("t") != "discover":
            log.debug("discovery: probe from %s wasn't t=discover: %r",
                      addr, msg)
            return
        reply = {
            "t": "bridge",
            "host": self._lan_ip,
            "port": self._tcp_port,
            "seed": self._get_seed() or "",
        }
        payload = (json.dumps(reply, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            assert self._transport is not None
            self._transport.sendto(payload, addr)
            log.info("discovery: replied to %s (%d bytes -> host=%s port=%d)",
                     addr, len(payload), self._lan_ip, self._tcp_port)
        except Exception:
            log.exception("discovery: sendto failed (addr=%s)", addr)

    def error_received(self, exc: Exception) -> None:
        log.debug("discovery: datagram error: %r", exc)


class DiscoveryResponder:
    """UDP bridge-discovery responder. One per DreadClient process."""

    def __init__(
        self,
        tcp_port: int,
        get_seed: SeedProvider,
        bind_host: str = "0.0.0.0",
        port: int = DEFAULT_DISCOVERY_PORT,
        get_lan_ip: Callable[[], str] = detect_lan_ip,
    ) -> None:
        self._tcp_port = tcp_port
        self._get_seed = get_seed
        self._bind_host = bind_host
        self._port = port
        self._get_lan_ip = get_lan_ip
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _ResponderProtocol | None = None

    async def start(self) -> bool:
        """Bind the UDP socket and start listening. Returns True on success.

        On bind failure logs WARN and returns False — DreadClient keeps
        running. The Switch's discovery sweep retries on every backoff
        cycle so a transient collision self-heals once the offending
        process exits.

        We create + bind the raw socket ourselves (rather than passing
        ``local_addr=`` to ``create_datagram_endpoint``) for two reasons:
          1. The Win32 WSAECONNRESET ioctl needs a raw ``socket.socket``
             — asyncio's TransportSocket doesn't expose ``ioctl``.
          2. ``SO_REUSEADDR`` (Windows lets a previous crashed-process
             release the port without TIME_WAIT) must be set pre-bind.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self._bind_host, self._port))
        except OSError as e:
            sock.close()
            log.warning(
                "discovery: failed to bind UDP %s:%d (%s) — auto-discovery "
                "disabled this session; rebuild the sysmodule with a "
                "BRIDGE_HOST that resolves directly to this PC.",
                self._bind_host, self._port, e,
            )
            return False

        # Windows-only: suppress ICMP "port unreachable" → WSAECONNRESET
        # poisoning. Ryujinx alongside a real Switch trips this on every
        # reconnect cycle; one poisoned transport silently drops all
        # subsequent probes for the bridge's lifetime. Must run on the raw
        # socket BEFORE asyncio wraps it.
        if sys.platform == "win32":
            try:
                _disable_udp_connreset_win32(sock)
            except OSError as e:
                log.warning(
                    "discovery: failed to disable WSAECONNRESET (%s) — "
                    "the UDP socket may stop accepting probes after the "
                    "first ICMP-unreachable bounce.", e,
                )
            except Exception as e:
                log.warning(
                    "discovery: WSAIoctl ctypes call raised (%r) — "
                    "the WSAECONNRESET poisoning hazard is not "
                    "suppressed this session.", e,
                )

        loop = asyncio.get_running_loop()
        try:
            transport, protocol = await loop.create_datagram_endpoint(
                lambda: _ResponderProtocol(
                    self._tcp_port, self._get_seed, self._get_lan_ip,
                ),
                sock=sock,
            )
        except Exception as e:
            sock.close()
            log.warning(
                "discovery: create_datagram_endpoint failed: %r", e,
            )
            return False
        self._transport = transport
        self._protocol = protocol  # type: ignore[assignment]
        log.info(
            "discovery: listening on UDP %s:%d (replies advertise TCP %s:%d)",
            self._bind_host, self._port,
            getattr(self._protocol, "_lan_ip", "?"), self._tcp_port,
        )
        return True

    def stop(self) -> None:
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None
        self._protocol = None
