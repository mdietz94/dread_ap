"""Tests for the UDP discovery responder.

Exercises the live loopback path: bind the responder on an ephemeral UDP
port, send a JSON probe from a stand-in client, assert that the reply
points back at the right TCP port.
"""
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


def _find_free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ProbeClientProto(asyncio.DatagramProtocol):
    def __init__(self, fut: asyncio.Future):
        self._fut = fut

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        if not self._fut.done():
            self._fut.set_result((data, addr))


@pytest.mark.asyncio
async def test_loopback_discovery_returns_bridge_advertisement():
    port = _find_free_udp_port()
    tcp_port = 17777
    responder = DiscoveryResponder(
        tcp_port=tcp_port,
        get_seed=lambda: "test-seed",
        bind_host="127.0.0.1",
        port=port,
        get_lan_ip=lambda: "10.0.0.42",
    )
    ok = await responder.start()
    assert ok, "responder failed to bind"
    try:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        client_transport, _ = await loop.create_datagram_endpoint(
            lambda: _ProbeClientProto(fut),
            local_addr=("127.0.0.1", 0),
        )
        try:
            probe = json.dumps({"t": "discover", "mod_ver": "test"}).encode() + b"\n"
            client_transport.sendto(probe, ("127.0.0.1", port))
            data, addr = await asyncio.wait_for(fut, timeout=2.0)
        finally:
            client_transport.close()

        reply = json.loads(data.decode("utf-8"))
        assert reply["t"] == "bridge"
        assert reply["host"] == "10.0.0.42"
        assert reply["port"] == tcp_port
        assert reply["seed"] == "test-seed"
        assert addr == ("127.0.0.1", port)
    finally:
        responder.stop()


@pytest.mark.asyncio
async def test_non_discover_probe_is_ignored():
    port = _find_free_udp_port()
    responder = DiscoveryResponder(
        tcp_port=17777,
        get_seed=lambda: "",
        port=port,
        get_lan_ip=lambda: "127.0.0.1",
    )
    ok = await responder.start()
    assert ok
    try:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        client_transport, _ = await loop.create_datagram_endpoint(
            lambda: _ProbeClientProto(fut),
            local_addr=("127.0.0.1", 0),
        )
        try:
            probe = b'{"t":"hello","mod_ver":"x"}\n'
            client_transport.sendto(probe, ("127.0.0.1", port))
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(fut, timeout=0.5)
        finally:
            client_transport.close()
    finally:
        responder.stop()


@pytest.mark.asyncio
async def test_bind_failure_returns_false(monkeypatch):
    """When bind() raises OSError, start() returns False (rather than crashing).

    We can't easily reproduce port-collision portably (Windows SO_REUSEADDR
    allows double-bind), so we monkey-patch ``socket.socket.bind`` to raise."""
    import socket

    real_bind = socket.socket.bind

    def boom_bind(self, addr):
        if addr[1] != 0:  # only trip on the responder's bind, not the helper
            raise OSError(98, "simulated address in use")
        return real_bind(self, addr)

    monkeypatch.setattr(socket.socket, "bind", boom_bind)
    r = DiscoveryResponder(tcp_port=17777, get_seed=lambda: "",
                           port=12345, get_lan_ip=lambda: "127.0.0.1")
    ok = await r.start()
    assert ok is False
