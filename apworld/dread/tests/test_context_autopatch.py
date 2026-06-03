"""Tests for the PR-A2 auto-patch hand-off on AP-connect.

When ``/setup`` has recorded the user's romfs + deploy paths,
``_on_connected`` should schedule ``_run_patch`` automatically. When the
state file is missing, incomplete, or stale, the AP session must still
proceed cleanly — only a log line is produced. These tests assert that
contract end-to-end on the ``_maybe_auto_patch`` method.
"""
from __future__ import annotations

import asyncio
import json
import logging
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
        bridge_port=0,
        discovery_port=0,
    )


def _write_state(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---- happy path ---------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_patch_schedules_run_patch_with_ryujinx_paths(
    ctx, tmp_path, monkeypatch
):
    """Setup state present + slot_data has placements → _run_patch is
    scheduled with the ryujinx-style deploy dir and the persisted romfs."""
    state_file = tmp_path / "setup_state.json"
    romfs_dir = tmp_path / "romfs"
    romfs_dir.mkdir()
    ryu_root = tmp_path / "Ryujinx"
    (ryu_root / "mods" / "contents").mkdir(parents=True)
    _write_state(state_file, {
        "deploy_target": "ryujinx",
        "ryujinx_root": str(ryu_root),
        "romfs_path": str(romfs_dir),
    })
    monkeypatch.setattr(
        "dread.client.context.setup_state_path", lambda: state_file
    )

    ctx.slot_data = {"placements": {"foo": "bar"}}

    captured: dict = {}

    async def fake_run_patch(deploy_dir, romfs, *, mod_compatibility=None):
        captured["deploy_dir"] = deploy_dir
        captured["romfs"] = romfs
        captured["mod_compatibility"] = mod_compatibility

    ctx._run_patch = fake_run_patch  # type: ignore[assignment]

    await ctx._maybe_auto_patch()
    # asyncio.ensure_future scheduled the task; let it run.
    await asyncio.sleep(0)

    expected_dir = (
        ryu_root / "mods" / "contents" / "010093801237c000" / "DreadRandovania"
    )
    assert Path(captured["deploy_dir"]) == expected_dir
    assert Path(captured["romfs"]) == romfs_dir
    # Ryujinx deploy → ryujinx compatibility (nested DreadRandovania layout).
    assert captured["mod_compatibility"] == "ryujinx"


@pytest.mark.asyncio
async def test_auto_patch_schedules_run_patch_with_sd_paths(
    ctx, tmp_path, monkeypatch
):
    state_file = tmp_path / "setup_state.json"
    romfs_dir = tmp_path / "romfs"
    romfs_dir.mkdir()
    sd_root = tmp_path / "sd"
    # A freshly-mounted SD card has only the Atmosphere CFW `atmosphere` dir;
    # the per-title contents dir doesn't exist until the patcher creates it.
    # The mount gate must accept this (first-ever-deploy) case.
    (sd_root / "atmosphere").mkdir(parents=True)
    _write_state(state_file, {
        "deploy_target": "sd",
        "sd_root": str(sd_root),
        "romfs_path": str(romfs_dir),
    })
    monkeypatch.setattr(
        "dread.client.context.setup_state_path", lambda: state_file
    )

    ctx.slot_data = {"placements": {}}

    captured: dict = {}

    async def fake_run_patch(deploy_dir, romfs, *, mod_compatibility=None):
        captured["deploy_dir"] = deploy_dir
        captured["romfs"] = romfs
        captured["mod_compatibility"] = mod_compatibility

    ctx._run_patch = fake_run_patch  # type: ignore[assignment]

    await ctx._maybe_auto_patch()
    await asyncio.sleep(0)

    expected_dir = sd_root / "atmosphere" / "contents" / "010093801237c000"
    assert Path(captured["deploy_dir"]) == expected_dir
    # SD deploy → atmosphere compatibility (flat contents/<tid> layout).
    assert captured["mod_compatibility"] == "atmosphere"


@pytest.mark.asyncio
async def test_auto_patch_sd_not_mounted_skips_and_logs(
    ctx, tmp_path, monkeypatch, caplog
):
    """SD deploy whose card isn't mounted (no `atmosphere` dir at sd_root) →
    skip + 'SD card not mounted' log, never crash. Guards the gate contract:
    mount is signalled by the Atmosphere CFW `atmosphere` dir, not by the
    per-title contents dir (which the patcher creates on first deploy)."""
    state_file = tmp_path / "setup_state.json"
    romfs_dir = tmp_path / "romfs"
    romfs_dir.mkdir()
    sd_root = tmp_path / "sd"
    sd_root.mkdir()  # drive present, but no `atmosphere` dir → not a Switch card
    _write_state(state_file, {
        "deploy_target": "sd",
        "sd_root": str(sd_root),
        "romfs_path": str(romfs_dir),
    })
    monkeypatch.setattr(
        "dread.client.context.setup_state_path", lambda: state_file
    )

    ran = {"called": False}

    async def fake_run_patch(deploy_dir, romfs, *, mod_compatibility=None):
        ran["called"] = True

    ctx._run_patch = fake_run_patch  # type: ignore[assignment]
    ctx.slot_data = {"placements": {}}

    caplog.set_level(logging.INFO, logger="Client")
    await ctx._maybe_auto_patch()
    await asyncio.sleep(0)

    assert ran["called"] is False
    assert any("SD card not mounted" in r.message for r in caplog.records)


# ---- skip paths (state missing / stale / incomplete) --------------------

@pytest.mark.asyncio
async def test_auto_patch_no_state_file_skips_and_logs(
    ctx, tmp_path, monkeypatch, caplog
):
    """No setup_state.json → no _run_patch and a 'run /setup first' log."""
    state_file = tmp_path / "setup_state.json"  # never written
    monkeypatch.setattr(
        "dread.client.context.setup_state_path", lambda: state_file
    )

    ran = {"called": False}

    async def fake_run_patch(deploy_dir, romfs):
        ran["called"] = True

    ctx._run_patch = fake_run_patch  # type: ignore[assignment]
    ctx.slot_data = {"placements": {}}

    caplog.set_level(logging.INFO, logger="Client")
    await ctx._maybe_auto_patch()
    await asyncio.sleep(0)

    assert ran["called"] is False
    msgs = " ".join(r.message for r in caplog.records)
    assert "Run /setup" in msgs or "run /setup" in msgs


@pytest.mark.asyncio
async def test_auto_patch_unreadable_state_file_skips_and_logs(
    ctx, tmp_path, monkeypatch, caplog
):
    """Corrupt JSON in setup_state.json → skip + warn (never crash)."""
    state_file = tmp_path / "setup_state.json"
    state_file.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(
        "dread.client.context.setup_state_path", lambda: state_file
    )

    ran = {"called": False}

    async def fake_run_patch(deploy_dir, romfs):
        ran["called"] = True

    ctx._run_patch = fake_run_patch  # type: ignore[assignment]
    ctx.slot_data = {"placements": {}}

    caplog.set_level(logging.WARNING, logger="Client")
    await ctx._maybe_auto_patch()
    await asyncio.sleep(0)

    assert ran["called"] is False
    assert any("unreadable" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_auto_patch_missing_romfs_path_skips_and_logs(
    ctx, tmp_path, monkeypatch, caplog
):
    """State without romfs_path → skip + 'pick your extracted romfs' log."""
    state_file = tmp_path / "setup_state.json"
    _write_state(state_file, {
        "deploy_target": "ryujinx",
        "ryujinx_root": str(tmp_path / "Ryujinx"),
        # romfs_path absent
    })
    monkeypatch.setattr(
        "dread.client.context.setup_state_path", lambda: state_file
    )

    ran = {"called": False}

    async def fake_run_patch(deploy_dir, romfs):
        ran["called"] = True

    ctx._run_patch = fake_run_patch  # type: ignore[assignment]
    ctx.slot_data = {"placements": {}}

    caplog.set_level(logging.INFO, logger="Client")
    await ctx._maybe_auto_patch()
    await asyncio.sleep(0)

    assert ran["called"] is False
    assert any("romfs_path not recorded" in r.message
               for r in caplog.records)


@pytest.mark.asyncio
async def test_auto_patch_stale_romfs_path_skips_and_logs(
    ctx, tmp_path, monkeypatch, caplog
):
    """romfs_path points at a folder that no longer exists → skip + warn."""
    state_file = tmp_path / "setup_state.json"
    _write_state(state_file, {
        "deploy_target": "ryujinx",
        "ryujinx_root": str(tmp_path / "Ryujinx"),
        "romfs_path": str(tmp_path / "deleted_romfs"),  # not created
    })
    monkeypatch.setattr(
        "dread.client.context.setup_state_path", lambda: state_file
    )

    ran = {"called": False}

    async def fake_run_patch(deploy_dir, romfs):
        ran["called"] = True

    ctx._run_patch = fake_run_patch  # type: ignore[assignment]
    ctx.slot_data = {"placements": {}}

    caplog.set_level(logging.WARNING, logger="Client")
    await ctx._maybe_auto_patch()
    await asyncio.sleep(0)

    assert ran["called"] is False
    assert any("no longer exists" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_auto_patch_missing_deploy_target_skips_and_logs(
    ctx, tmp_path, monkeypatch, caplog
):
    """deploy_target absent → skip + 'pick Ryujinx / SD / Custom' log."""
    state_file = tmp_path / "setup_state.json"
    romfs_dir = tmp_path / "romfs"
    romfs_dir.mkdir()
    _write_state(state_file, {
        "romfs_path": str(romfs_dir),
        # deploy_target absent
    })
    monkeypatch.setattr(
        "dread.client.context.setup_state_path", lambda: state_file
    )

    ran = {"called": False}

    async def fake_run_patch(deploy_dir, romfs):
        ran["called"] = True

    ctx._run_patch = fake_run_patch  # type: ignore[assignment]
    ctx.slot_data = {"placements": {}}

    caplog.set_level(logging.INFO, logger="Client")
    await ctx._maybe_auto_patch()
    await asyncio.sleep(0)

    assert ran["called"] is False
    assert any("deploy target not recorded" in r.message
               for r in caplog.records)


@pytest.mark.asyncio
async def test_auto_patch_no_placements_in_slot_data_skips_and_logs(
    ctx, tmp_path, monkeypatch, caplog
):
    """slot_data lacks 'placements' (old seed) → skip + manual /patch hint."""
    state_file = tmp_path / "setup_state.json"
    romfs_dir = tmp_path / "romfs"
    romfs_dir.mkdir()
    ryu_root = tmp_path / "Ryujinx"
    (ryu_root / "mods" / "contents").mkdir(parents=True)
    _write_state(state_file, {
        "deploy_target": "ryujinx",
        "ryujinx_root": str(ryu_root),
        "romfs_path": str(romfs_dir),
    })
    monkeypatch.setattr(
        "dread.client.context.setup_state_path", lambda: state_file
    )

    ran = {"called": False}

    async def fake_run_patch(deploy_dir, romfs):
        ran["called"] = True

    ctx._run_patch = fake_run_patch  # type: ignore[assignment]
    ctx.slot_data = {}  # no placements key

    caplog.set_level(logging.INFO, logger="Client")
    await ctx._maybe_auto_patch()
    await asyncio.sleep(0)

    assert ran["called"] is False
    assert any("no placements" in r.message for r in caplog.records)

