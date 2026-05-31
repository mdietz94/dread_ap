"""Tests for the installer-UX additions on DreadContext:
remembered Switch IP, the backoff supervisor, and interactive /patch."""
from __future__ import annotations

import asyncio
import json
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
        switch_host="127.0.0.1",
    )


# ---- remembered IP ------------------------------------------------------

def test_remember_switch_target_persists(ctx, tmp_path, monkeypatch):
    cfgfile = tmp_path / "config.json"
    monkeypatch.setattr("dread.client.context._user_config_path", lambda: cfgfile)
    ctx.switch_host = "10.0.0.5"
    ctx.switch_port = 7000
    ctx._remember_switch_target()
    cfg = json.loads(cfgfile.read_text())
    assert cfg["switch_host"] == "10.0.0.5"
    assert cfg["switch_port"] == 7000


def test_remember_preserves_other_config_keys(ctx, tmp_path, monkeypatch):
    cfgfile = tmp_path / "config.json"
    cfgfile.write_text(json.dumps({"dreadvania_python": "C:/py/python.exe"}))
    monkeypatch.setattr("dread.client.context._user_config_path", lambda: cfgfile)
    ctx._remember_switch_target()
    cfg = json.loads(cfgfile.read_text())
    assert cfg["dreadvania_python"] == "C:/py/python.exe"
    assert cfg["switch_host"] == "127.0.0.1"


# ---- redial signal ------------------------------------------------------

def test_request_redial_sets_event(ctx):
    ctx._redial_event.clear()
    ctx.request_redial()
    assert ctx._redial_event.is_set()


# ---- backoff supervisor -------------------------------------------------

@pytest.mark.asyncio
async def test_switch_supervisor_backoff_sequence(ctx, monkeypatch):
    """Three failed dials wait 1, 2, 4s; the fourth connects and stops."""
    waits: list[float] = []
    attempts = {"n": 0}

    async def fake_connect():
        attempts["n"] += 1
        if attempts["n"] >= 4:
            ctx.executor = object()  # type: ignore[assignment]
            ctx.exit_event.set()

    ctx.connect_switch = fake_connect  # type: ignore[assignment]

    async def fake_wait_for(awaitable, timeout):
        waits.append(timeout)
        if hasattr(awaitable, "close"):
            awaitable.close()  # avoid "coroutine never awaited"
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    # NB: don't wrap in asyncio.wait_for — it's monkeypatched. The fake never
    # really sleeps, and exit_event stops the loop on connect.
    await ctx._switch_supervisor()
    assert waits == [1.0, 2.0, 4.0]
    assert attempts["n"] == 4


@pytest.mark.asyncio
async def test_switch_supervisor_redial_resets_backoff(ctx, monkeypatch):
    """A manual redial during the backoff sleep resets it to the start."""
    waits: list[float] = []
    attempts = {"n": 0}

    async def fake_connect():
        attempts["n"] += 1
        # Fail twice, then a redial fires, then connect on the 3rd attempt.
        if attempts["n"] >= 3:
            ctx.executor = object()  # type: ignore[assignment]
            ctx.exit_event.set()

    ctx.connect_switch = fake_connect  # type: ignore[assignment]

    async def fake_wait_for(awaitable, timeout):
        waits.append(timeout)
        if hasattr(awaitable, "close"):
            awaitable.close()
        # On the 2nd backoff sleep, simulate a manual redial (returns instead
        # of timing out) so the supervisor resets backoff to START.
        if len(waits) == 2:
            return True
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)

    await ctx._switch_supervisor()
    # attempt1 fail -> wait 1 (backoff->2); attempt2 fail -> wait 2 but redial
    # resets backoff to START; attempt3 connects. No 4.0 wait recorded.
    assert waits == [1.0, 2.0]
    assert attempts["n"] == 3


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
