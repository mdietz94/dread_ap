"""Unit tests for ``_setup.deploy``.

Strategy: fake an SD / Ryujinx layout under ``tmp_path``, then assert
``deploy_to_*`` lands the build outputs at the right paths and the
``DeployResult`` summary is shaped correctly.

``detect_sd_candidates`` and ``detect_ryujinx_path`` are tested through
their non-Windows short-circuit (returns ``[]`` / ``None``) so the suite
stays platform-independent. The Windows-only drive-letter probe is a
single ``for letter in A-Z`` loop with an ``atmosphere/`` exists() check
— covered well enough by the manual SD test in PR-A5.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread._setup import deploy  # noqa: E402

# Explicit LAN IP passed to every deploy_to_* call so the suite never touches
# the real network (an unset bridge_host would auto-detect via detect_lan_ip).
FAKE_HOST = "192.168.7.42"


def _read_ap_config(romfs_dir: Path) -> dict:
    import json
    return json.loads((romfs_dir / "ap_config.json").read_text(encoding="utf-8"))


@pytest.fixture
def fake_outputs(tmp_path):
    """Produce a build_outputs dict the deploy_to_* functions accept."""
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    subsdk9 = src_dir / "subsdk9"
    subsdk9.write_bytes(b"\x7fELF" + b"\x00" * 1020)
    main_npdm = src_dir / "main.npdm"
    main_npdm.write_bytes(b"META" + b"\x00" * 252)
    return {"subsdk9": subsdk9, "main.npdm": main_npdm}


# ---- detect_sd_candidates / detect_ryujinx_path -----------------------

def test_detect_sd_candidates_returns_empty_on_non_windows(monkeypatch):
    """The function is scoped to Windows for v1; non-Windows returns []
    so the wizard's "Browse..." button is the only path."""
    monkeypatch.setattr(deploy.sys, "platform", "linux")
    assert deploy.detect_sd_candidates() == []


def test_detect_ryujinx_path_returns_none_on_non_windows(monkeypatch):
    """Same as above — non-Windows returns None; the user browses."""
    monkeypatch.setattr(deploy.sys, "platform", "linux")
    assert deploy.detect_ryujinx_path() is None


def test_detect_ryujinx_path_resolves_appdata_when_present(tmp_path, monkeypatch):
    """On Windows, ``%APPDATA%/Ryujinx`` is the canonical default."""
    monkeypatch.setattr(deploy.sys, "platform", "win32")
    appdata = tmp_path / "AppData"
    ryu = appdata / "Ryujinx"
    ryu.mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    p = deploy.detect_ryujinx_path()
    assert p == ryu


def test_detect_ryujinx_path_none_when_dir_missing(tmp_path, monkeypatch):
    """%APPDATA%/Ryujinx doesn't exist: detector returns None even though
    APPDATA is set (correct — we don't want to suggest a path that isn't
    a real install)."""
    monkeypatch.setattr(deploy.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    assert deploy.detect_ryujinx_path() is None


# ---- _sd_layout / _ryujinx_layout --------------------------------------

def test_sd_layout_paths():
    """SD layout writes under atmosphere/contents/<title>/exefs|romfs."""
    layout = deploy._sd_layout(Path("/sd"))
    assert layout["subsdk9"] == Path("/sd") / "atmosphere" / "contents" / deploy.DREAD_TITLE_ID / "exefs" / "subsdk9"
    assert layout["main.npdm"] == Path("/sd") / "atmosphere" / "contents" / deploy.DREAD_TITLE_ID / "exefs" / "main.npdm"


def test_ryujinx_layout_paths():
    """Ryujinx layout writes under mods/contents/<title>/<modname>/exefs."""
    layout = deploy._ryujinx_layout(Path("/ryu"))
    assert layout["subsdk9"] == Path("/ryu") / "mods" / "contents" / deploy.DREAD_TITLE_ID / deploy.RYU_MOD_NAME / "exefs" / "subsdk9"


# ---- deploy_to_ryujinx --------------------------------------------------

def test_deploy_to_ryujinx_lands_both_files(tmp_path, fake_outputs):
    """Files land at exefs/<filename> under the mod directory; byte sizes
    match the source; DeployResult.ok is True."""
    ryu = tmp_path / "Ryujinx"
    ryu.mkdir()
    result = deploy.deploy_to_ryujinx(ryu, fake_outputs, bridge_host=FAKE_HOST)
    assert result.ok is True
    assert "Ryujinx" in result.target
    exefs = ryu / "mods" / "contents" / deploy.DREAD_TITLE_ID / deploy.RYU_MOD_NAME / "exefs"
    assert (exefs / "subsdk9").is_file()
    assert (exefs / "main.npdm").is_file()
    assert (exefs / "subsdk9").stat().st_size == fake_outputs["subsdk9"].stat().st_size
    # subsdk9 + main.npdm + ap_config.json
    assert len(result.files) == 3
    # ap_config.json lands in the sdcard romfs layer (separate from exefs).
    romfs = ryu / "sdcard" / "atmosphere" / "contents" / deploy.DREAD_TITLE_ID / "romfs"
    assert _read_ap_config(romfs) == {"bridge_host": FAKE_HOST}


def test_deploy_to_ryujinx_creates_parent_dirs(tmp_path, fake_outputs):
    """Even when only the root exists, deploy creates the full path tree."""
    ryu = tmp_path / "Ryujinx"
    ryu.mkdir()
    # no mods/, no contents/, no anything
    result = deploy.deploy_to_ryujinx(ryu, fake_outputs, bridge_host=FAKE_HOST)
    assert result.ok is True


# ---- deploy_to_sd -------------------------------------------------------

def test_deploy_to_sd_lands_both_files(tmp_path, fake_outputs):
    """Files land at atmosphere/contents/<title>/exefs/<filename>."""
    sd = tmp_path / "sd"
    sd.mkdir()
    result = deploy.deploy_to_sd(sd, fake_outputs, bridge_host=FAKE_HOST)
    assert result.ok is True
    assert "SD card" in result.target
    base = sd / "atmosphere" / "contents" / deploy.DREAD_TITLE_ID
    exefs = base / "exefs"
    assert (exefs / "subsdk9").is_file()
    assert (exefs / "main.npdm").is_file()
    # ap_config.json sits in the same romfs the patcher seed populates, so
    # the sysmodule reads it as rom:/ap_config.json.
    assert _read_ap_config(base / "romfs") == {"bridge_host": FAKE_HOST}


# ---- deploy_to_custom_folder -------------------------------------------

def test_deploy_to_custom_folder_uses_sd_layout(tmp_path, fake_outputs):
    """The custom folder deploy produces the same atmosphere/contents/...
    subtree as deploy_to_sd, so the user can drop it on any SD card."""
    custom = tmp_path / "staging"
    custom.mkdir()
    result = deploy.deploy_to_custom_folder(custom, fake_outputs, bridge_host=FAKE_HOST)
    assert result.ok is True
    assert "Custom folder" in result.target
    base = custom / "atmosphere" / "contents" / deploy.DREAD_TITLE_ID
    assert (base / "exefs" / "subsdk9").is_file()
    assert _read_ap_config(base / "romfs") == {"bridge_host": FAKE_HOST}


# ---- _copy_files error paths -------------------------------------------

def test_deploy_to_sd_missing_source_returns_error(tmp_path):
    """If the build output file no longer exists, the DeployResult
    surfaces a clear 'Re-run the Build step' message rather than raising."""
    sd = tmp_path / "sd"
    sd.mkdir()
    missing = tmp_path / "vanished" / "subsdk9"
    # Don't create it.
    result = deploy.deploy_to_sd(sd, {"subsdk9": missing, "main.npdm": missing})
    assert result.ok is False
    assert "unreadable" in result.error.lower() or "no such" in result.error.lower()


def test_deploy_to_custom_folder_preserves_source_byte_sizes(tmp_path, fake_outputs):
    """The copy verifies dest size == src size after shutil.copy2 (the
    Windows-FS-filter early-return guard)."""
    custom = tmp_path / "staging"
    custom.mkdir()
    result = deploy.deploy_to_custom_folder(custom, fake_outputs, bridge_host=FAKE_HOST)
    assert result.ok is True
    for src_path, dst_path in result.files:
        assert dst_path.stat().st_size == src_path.stat().st_size
