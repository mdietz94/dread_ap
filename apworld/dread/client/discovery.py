"""UDP bridge-discovery responder.

The Switch sysmodule sends a small JSON probe to one of:
  * ``127.0.0.1:17779`` (Ryujinx-on-same-host, tried first)
  * each ``.1..254`` of its own ``/24`` (sweep — tried next)

We bind a UDP socket on ``0.0.0.0:17779``, accept either delivery path,
and unicast a reply telling the Switch where the TCP ``SwitchServer`` is
listening. The reply's ``host`` field comes from the shared
``detect_lan_ip()`` helper so the Switch always gets a routable address
(even when the probe arrived on loopback).

Wire format (newline-terminated UTF-8 JSON):

    probe:  {"t":"discover","mod_ver":"<x>"}\\n
    reply:  {"t":"bridge","host":"<ipv4>","port":17777}\\n

Bind failure (e.g. port already in use because another DreadClient is
running) is logged at WARN and the responder no-ops. There is no manual
IP fallback — the rest of the wire only works through this responder.

Direct port of ``smo_archipelago/apworld/smo_archipelago/client/discovery.py``;
see that file for the WSAECONNRESET ioctl design rationale.
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

log = logging.getLogger(__name__)

DEFAULT_DISCOVERY_PORT = 17779
MAX_PROBE_BYTES = 512  # probes are tiny; cap defensively


def _disable_udp_connreset_win32(sock: socket.socket) -> None:
    """Disable WSAECONNRESET on a UDP socket via WSAIoctl(SIO_UDP_CONNRESET).

    Python's ``socket.ioctl()`` has a Win32 IOCTL whitelist that doesn't
    include SIO_UDP_CONNRESET, so we call WSAIoctl directly via ctypes.
    Caller catches OSError if the call fails — we don't want a ctypes
    edge case to take down the whole responder.
    """
    SIO_UDP_CONNRESET = 0x9800000C
    ws2 = ctypes.WinDLL("ws2_32")
    LPDWORD = ctypes.POINTER(ctypes.c_ulong)
    ws2.WSAIoctl.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32,
        LPDWORD,
        ctypes.c_void_p, ctypes.c_void_p,
    ]
    ws2.WSAIoctl.restype = ctypes.c_int
    enable = ctypes.c_uint32(0)
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


class _ResponderProtocol(asyncio.DatagramProtocol):
    def __init__(
        self,
        tcp_port: int,
        get_lan_ip: Callable[[], str] = detect_lan_ip,
        advertise_host: str | None = None,
    ) -> None:
        self._tcp_port = tcp_port
        self._get_lan_ip = get_lan_ip
        self._transport: asyncio.DatagramTransport | None = None
        # If an explicit advertise host is given (e.g. the test harness
        # bound the listener to ``127.0.0.1``), respect it. Otherwise
        # cache ``detect_lan_ip()`` for the lifetime of the responder.
        self._lan_ip = advertise_host or self._get_lan_ip()

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
        bind_host: str = "0.0.0.0",
        port: int = DEFAULT_DISCOVERY_PORT,
        get_lan_ip: Callable[[], str] = detect_lan_ip,
        advertise_host: str | None = None,
    ) -> None:
        self._tcp_port = tcp_port
        self._bind_host = bind_host
        self._port = port
        self._get_lan_ip = get_lan_ip
        # When the listener is bound to a specific non-``0.0.0.0`` host,
        # advertise THAT host — it's the only address the listener will
        # accept connections on. ``detect_lan_ip()`` is only the right
        # answer when binding all interfaces.
        if advertise_host is None and bind_host not in ("0.0.0.0", ""):
            advertise_host = bind_host
        self._advertise_host = advertise_host
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _ResponderProtocol | None = None

    async def start(self) -> bool:
        """Bind the UDP socket and start listening. Returns True on success.

        On bind failure (port in use, etc.) logs a WARN and returns False —
        the DreadClient keeps running but the Switch can never find it. The
        usual cause is a second DreadClient instance hogging the port.

        We create + bind the raw socket ourselves (rather than passing
        ``local_addr=`` to ``create_datagram_endpoint``) so we can set
        SO_REUSEADDR and run the Windows-only SIO_UDP_CONNRESET ioctl
        before asyncio wraps the fd.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self._bind_host, self._port))
        except OSError as e:
            sock.close()
            log.warning(
                "discovery: failed to bind UDP %s:%d (%s) — auto-discovery "
                "disabled this session; the Switch sysmodule will not be "
                "able to find this client.",
                self._bind_host, self._port, e,
            )
            return False

        # Windows-only: suppress ICMP "port unreachable" -> WSAECONNRESET
        # poisoning on this UDP socket. Without this, when our reply
        # sendto() hits an already-closed ephemeral port (Ryujinx tearing
        # down a probe socket between send and reply lands here routinely),
        # Windows returns WSAECONNRESET on the next recv, asyncio's
        # DatagramTransport surfaces it via error_received, and subsequent
        # inbound datagrams are silently dropped. SIO_UDP_CONNRESET makes
        # Windows ignore the ICMP error. Must run on the raw socket BEFORE
        # asyncio wraps it — TransportSocket doesn't expose ioctl.
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
                    self._tcp_port,
                    self._get_lan_ip,
                    advertise_host=self._advertise_host,
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
        # Resolve the actual bound port — matters when port=0 (ephemeral).
        sockname = transport.get_extra_info("sockname")
        if sockname and len(sockname) >= 2:
            self._port = int(sockname[1])
        log.info(
            "discovery: listening on UDP %s:%d (replies advertise TCP %s:%d)",
            self._bind_host, self._port,
            getattr(self._protocol, "_lan_ip", "?"), self._tcp_port,
        )
        return True

    @property
    def actual_port(self) -> int:
        """Bound UDP port (resolves :0 to a real ephemeral port post-start)."""
        return self._port

    def stop(self) -> None:
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None
        self._protocol = None
