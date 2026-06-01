"""Tests for BridgeServer: HELLO/HELLO_ACK, lua_exec roundtrip, multi-Switch
active/inactive promotion.
"""
from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client import wire as W  # noqa: E402
from dread.client.bridge_server import BridgeServer, ActiveConnInfo  # noqa: E402
from dread.tests.fakeswitch import FakeDreadGame  # noqa: E402


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.asyncio
async def test_hello_ack_handshake():
    port = _free_port()
    active_events: list[ActiveConnInfo] = []

    async def on_active(info: ActiveConnInfo):
        active_events.append(info)

    bridge = BridgeServer(slot="Samus", seed="abc123", port=port,
                          on_active_connected=on_active)
    await bridge.start()
    fake = FakeDreadGame(mod_ver="v1", layout_uuid="layout-xyz", device_id="sw-A")
    try:
        await fake.connect(host="127.0.0.1", port=port)
        ack = await fake.wait_for_hello_ack(5.0)
        assert ack.ok is True
        assert ack.slot == "Samus"
        assert ack.seed == "abc123"
        # active callback fired
        assert len(active_events) == 1
        assert active_events[0].device_id == "sw-A"
        assert active_events[0].layout_uuid == "layout-xyz"
    finally:
        await fake.stop()
        await bridge.close()


@pytest.mark.asyncio
async def test_hello_ack_rejects_version_mismatch():
    port = _free_port()
    bridge = BridgeServer(slot="Samus", seed="x", port=port,
                          expected_mod_ver="v1.expected")
    await bridge.start()
    fake = FakeDreadGame(mod_ver="v0.something_else", device_id="sw-A")
    try:
        await fake.connect(host="127.0.0.1", port=port)
        ack = await fake.wait_for_hello_ack(5.0)
        assert ack.ok is False
        assert ack.err and "mod_ver" in ack.err
    finally:
        await fake.stop()
        await bridge.close()


@pytest.mark.asyncio
async def test_lua_exec_roundtrip():
    port = _free_port()
    active_events: list[ActiveConnInfo] = []

    async def on_active(info):
        active_events.append(info)

    bridge = BridgeServer(slot="x", seed="", port=port,
                          on_active_connected=on_active)
    await bridge.start()
    fake = FakeDreadGame(device_id="sw-A")
    try:
        await fake.connect("127.0.0.1", port)
        await fake.wait_for_hello_ack(5.0)
        # wait until active callback has fired
        for _ in range(100):
            if active_events:
                break
            await asyncio.sleep(0.01)
        assert active_events
        # Issue a lua_exec; fake should reply with ok=True
        resp = await bridge.run_lua("return tostring(Init.bBeatenSinceLastReboot)")
        assert resp.success
        assert resp.payload == b"false"
    finally:
        await fake.stop()
        await bridge.close()


@pytest.mark.asyncio
async def test_multi_switch_promotion():
    """Switch A actives. Switch B dials → kicked inactive. A disconnects →
    B is auto-promoted, on_active_connected fires again."""
    port = _free_port()
    active_events: list[ActiveConnInfo] = []

    async def on_active(info):
        active_events.append(info)

    bridge = BridgeServer(slot="x", seed="", port=port,
                          on_active_connected=on_active)
    await bridge.start()
    fake_a = FakeDreadGame(device_id="sw-A")
    fake_b = FakeDreadGame(device_id="sw-B")
    try:
        await fake_a.connect("127.0.0.1", port)
        await fake_a.wait_for_hello_ack(5.0)
        for _ in range(100):
            if active_events:
                break
            await asyncio.sleep(0.01)
        assert active_events[-1].device_id == "sw-A"

        await fake_b.connect("127.0.0.1", port)
        await fake_b.wait_for_hello_ack(5.0)
        # B should receive a Kick(reason="inactive")
        for _ in range(100):
            if fake_b.kicks_received:
                break
            await asyncio.sleep(0.01)
        assert "inactive" in fake_b.kicks_received

        # A disconnects → B auto-promotes
        await fake_a.stop()
        # Wait for the new on_active_connected for sw-B
        for _ in range(200):
            if any(e.device_id == "sw-B" for e in active_events):
                break
            await asyncio.sleep(0.01)
        assert any(e.device_id == "sw-B" for e in active_events)
        assert bridge.active_device_id() == "sw-B"
    finally:
        await fake_b.stop()
        await bridge.close()


@pytest.mark.asyncio
async def test_list_switches_shows_all_registered():
    port = _free_port()
    bridge = BridgeServer(slot="x", seed="", port=port)
    await bridge.start()
    fake_a = FakeDreadGame(device_id="sw-A")
    fake_b = FakeDreadGame(device_id="sw-B")
    try:
        await fake_a.connect("127.0.0.1", port)
        await fake_a.wait_for_hello_ack(5.0)
        await fake_b.connect("127.0.0.1", port)
        await fake_b.wait_for_hello_ack(5.0)
        for _ in range(100):
            if len(bridge.list_switches()) == 2:
                break
            await asyncio.sleep(0.01)
        switches = bridge.list_switches()
        ids = {s["device_id"] for s in switches}
        assert ids == {"sw-A", "sw-B"}
        active = [s for s in switches if s["active"]]
        assert len(active) == 1
        assert active[0]["device_id"] == "sw-A"
    finally:
        await fake_a.stop()
        await fake_b.stop()
        await bridge.close()


@pytest.mark.asyncio
async def test_manual_promote_swaps_active():
    port = _free_port()
    bridge = BridgeServer(slot="x", seed="", port=port)
    await bridge.start()
    fake_a = FakeDreadGame(device_id="sw-A")
    fake_b = FakeDreadGame(device_id="sw-B")
    try:
        await fake_a.connect("127.0.0.1", port)
        await fake_a.wait_for_hello_ack(5.0)
        await fake_b.connect("127.0.0.1", port)
        await fake_b.wait_for_hello_ack(5.0)
        for _ in range(100):
            if len(bridge.list_switches()) == 2:
                break
            await asyncio.sleep(0.01)
        assert bridge.active_device_id() == "sw-A"
        ok = await bridge.manual_promote("sw-B")
        assert ok
        assert bridge.active_device_id() == "sw-B"
    finally:
        await fake_a.stop()
        await fake_b.stop()
        await bridge.close()
