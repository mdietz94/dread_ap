"""Tests for the installer-UX additions on DreadContext:
the discovery listener startup and interactive /patch.

The pre-inversion suite also covered ``_remember_switch_target``,
``request_redial``, and the exponential-backoff supervisor; those code
paths were removed when the Switch became the TCP client (no more dial,
no more cached IP, no more supervisor). The listener-startup test below
replaces them.
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

from dread.client.datapackage import DataPackage  # noqa: E402
from dread.client.state import BridgeState  # noqa: E402

DATA = ROOT / "data"


@pytest.fixture
def ctx():
    from dread.client.context import DreadContext  # noqa: E402

    return DreadContext(
        server_address=None,
        password=None,
        state=BridgeState(),
        datapackage=DataPackage(apworld_data_dir=DATA),
        listen_host="127.0.0.1",
        listen_port=0,
        discovery_port=0,
    )


# ---- listener startup ---------------------------------------------------

@pytest.mark.asyncio
async def test_start_switch_listener_binds_and_advertises(ctx):
    """``start_switch_listener`` binds the TCP listener + UDP discovery
    responder, and a UDP probe gets back a ``{"t":"bridge"}`` reply whose
    ``port`` matches the bound TCP listener.

    This is the contract the exlaunch sysmodule depends on — UDP probe →
    JSON reply with the TCP port to dial.
    """
    await ctx.start_switch_listener()
    try:
        assert ctx.listen_port > 0
        assert ctx.discovery_port > 0

        # Probe via the asyncio socket API so the event loop stays
        # responsive (sync recvfrom would block the loop and deadlock
        # the responder).
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        try:
            probe = b'{"t":"discover","mod_ver":"test"}\n'
            await loop.sock_sendto(sock, probe,
                                   ("127.0.0.1", ctx.discovery_port))
            data, _addr = await asyncio.wait_for(
                loop.sock_recvfrom(sock, 4096), timeout=2.0)
        finally:
            sock.close()

        reply = json.loads(data.decode("utf-8"))
        assert reply["t"] == "bridge"
        assert reply["port"] == ctx.listen_port
        assert reply["host"]  # detect_lan_ip should populate this
    finally:
        await ctx.shutdown()


# ---- interactive /patch -------------------------------------------------

@pytest.mark.asyncio
async def test_patch_interactive_persists_and_runs(ctx, tmp_path, monkeypatch):
    cfgfile = tmp_path / "config.json"
    monkeypatch.setattr("dread.client.context._user_config_path", lambda: cfgfile)

    import dread.client.filedialog as fd
    picks = iter(["/dv/dir", "/romfs/dir"])
    monkeypatch.setattr(fd, "ask_directory",
                        lambda title, initialdir=None: next(picks))

    ran: dict = {}

    async def fake_run_patch(dv, romfs):
        ran["dv"] = dv
        ran["romfs"] = romfs

    ctx._run_patch = fake_run_patch  # type: ignore[assignment]
    await ctx._patch_interactive()

    assert ran == {"dv": "/dv/dir", "romfs": "/romfs/dir"}
    cfg = json.loads(cfgfile.read_text())
    assert cfg["dreadvania_dir"] == "/dv/dir"
    assert cfg["vanilla_romfs_dir"] == "/romfs/dir"


@pytest.mark.asyncio
async def test_patch_interactive_cancel_does_not_run(ctx, tmp_path, monkeypatch):
    cfgfile = tmp_path / "config.json"
    monkeypatch.setattr("dread.client.context._user_config_path", lambda: cfgfile)

    import dread.client.filedialog as fd
    monkeypatch.setattr(fd, "ask_directory",
                        lambda title, initialdir=None: None)  # user cancels

    ran = {"called": False}

    async def fake_run_patch(dv, romfs):
        ran["called"] = True

    ctx._run_patch = fake_run_patch  # type: ignore[assignment]
    await ctx._patch_interactive()
    assert ran["called"] is False
    assert not cfgfile.exists()


@pytest.mark.asyncio
async def test_patch_interactive_no_dialog_backend(ctx, tmp_path, monkeypatch):
    cfgfile = tmp_path / "config.json"
    monkeypatch.setattr("dread.client.context._user_config_path", lambda: cfgfile)

    import dread.client.filedialog as fd

    def _raise(title, initialdir=None):
        raise fd.FileDialogUnavailable("no tkinter")

    monkeypatch.setattr(fd, "ask_directory", _raise)

    ran = {"called": False}

    async def fake_run_patch(dv, romfs):
        ran["called"] = True

    ctx._run_patch = fake_run_patch  # type: ignore[assignment]
    await ctx._patch_interactive()  # must not raise
    assert ran["called"] is False
