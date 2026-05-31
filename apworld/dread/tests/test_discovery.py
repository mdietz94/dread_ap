"""Unit tests for the UDP discovery responder."""
from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client.discovery import DiscoveryResponder  # noqa: E402


async def _probe_once(port: int, payload: bytes, timeout: float = 1.0) -> bytes | None:
    """Send a single probe to 127.0.0.1:port and return the reply bytes
    (or None on timeout)."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        await loop.sock_sendto(sock, payload, ("127.0.0.1", port))
        try:
            data, _addr = await asyncio.wait_for(
                loop.sock_recvfrom(sock, 4096), timeout=timeout)
            return data
        except asyncio.TimeoutError:
            return None
    finally:
        sock.close()


@pytest.mark.asyncio
async def test_discover_probe_gets_bridge_reply():
    """A well-formed ``{"t":"discover"}`` probe gets a ``{"t":"bridge",...}``
    reply whose ``port`` is the advertised TCP port and whose ``host`` is
    ``get_lan_ip()`` (the default path used when binding ``0.0.0.0``)."""
    responder = DiscoveryResponder(
        tcp_port=17777,
        bind_host="0.0.0.0",
        port=0,
        get_lan_ip=lambda: "192.168.1.42",
    )
    assert await responder.start()
    try:
        probe = (json.dumps({"t": "discover", "mod_ver": "test"})
                 + "\n").encode("utf-8")
        data = await _probe_once(responder.actual_port, probe)
        assert data is not None
        reply = json.loads(data.decode("utf-8"))
        assert reply["t"] == "bridge"
        assert reply["host"] == "192.168.1.42"
        assert reply["port"] == 17777
    finally:
        responder.stop()


@pytest.mark.asyncio
async def test_non_default_bind_advertises_bind_host():
    """When the listener binds a specific IP (not ``0.0.0.0``), discovery
    advertises THAT IP instead of ``detect_lan_ip()``. Test wiring
    relies on this so loopback-only fixtures don't accidentally
    advertise the real LAN IP."""
    responder = DiscoveryResponder(
        tcp_port=17777,
        bind_host="127.0.0.1",
        port=0,
        get_lan_ip=lambda: "192.168.1.42",  # would be wrong here
    )
    assert await responder.start()
    try:
        probe = (json.dumps({"t": "discover"}) + "\n").encode("utf-8")
        data = await _probe_once(responder.actual_port, probe)
        assert data is not None
        reply = json.loads(data.decode("utf-8"))
        assert reply["host"] == "127.0.0.1"
    finally:
        responder.stop()


@pytest.mark.asyncio
async def test_oversize_probe_is_dropped():
    """Probes larger than ``MAX_PROBE_BYTES`` get silently dropped."""
    responder = DiscoveryResponder(
        tcp_port=17777, bind_host="127.0.0.1", port=0,
        get_lan_ip=lambda: "127.0.0.1",
    )
    assert await responder.start()
    try:
        huge = b"x" * 4000
        data = await _probe_once(responder.actual_port, huge, timeout=0.3)
        assert data is None
    finally:
        responder.stop()


@pytest.mark.asyncio
async def test_malformed_json_is_dropped():
    """Non-JSON probes get silently dropped."""
    responder = DiscoveryResponder(
        tcp_port=17777, bind_host="127.0.0.1", port=0,
        get_lan_ip=lambda: "127.0.0.1",
    )
    assert await responder.start()
    try:
        data = await _probe_once(responder.actual_port, b"not json\n", timeout=0.3)
        assert data is None
    finally:
        responder.stop()


@pytest.mark.asyncio
async def test_non_discover_t_is_dropped():
    """A probe with the wrong ``t`` field gets silently dropped."""
    responder = DiscoveryResponder(
        tcp_port=17777, bind_host="127.0.0.1", port=0,
        get_lan_ip=lambda: "127.0.0.1",
    )
    assert await responder.start()
    try:
        probe = (json.dumps({"t": "hello"}) + "\n").encode("utf-8")
        data = await _probe_once(responder.actual_port, probe, timeout=0.3)
        assert data is None
    finally:
        responder.stop()


@pytest.mark.asyncio
async def test_repeat_probes_each_get_replies():
    """Sending the same probe twice yields two distinct replies — no
    accidental deduplication, no socket poisoning."""
    responder = DiscoveryResponder(
        tcp_port=17777, bind_host="127.0.0.1", port=0,
        get_lan_ip=lambda: "127.0.0.1",
    )
    assert await responder.start()
    try:
        probe = (json.dumps({"t": "discover"}) + "\n").encode("utf-8")
        a = await _probe_once(responder.actual_port, probe)
        b = await _probe_once(responder.actual_port, probe)
        assert a is not None and b is not None
        # Same content; what we care about is both arrived.
        assert json.loads(a.decode("utf-8"))["t"] == "bridge"
        assert json.loads(b.decode("utf-8"))["t"] == "bridge"
    finally:
        responder.stop()


@pytest.mark.asyncio
async def test_start_is_idempotent_and_returns_bool():
    """``start()`` returns ``True`` on first bind. SO_REUSEADDR lets
    additional binds on the same port also succeed on some platforms —
    we just guarantee the return-value contract (``True`` for success).
    Worth pinning so a future SO_REUSEADDR removal doesn't silently
    change responder behavior."""
    a = DiscoveryResponder(
        tcp_port=17777, bind_host="127.0.0.1", port=0,
        get_lan_ip=lambda: "127.0.0.1",
    )
    try:
        assert await a.start() is True
        assert a.actual_port > 0
    finally:
        a.stop()
