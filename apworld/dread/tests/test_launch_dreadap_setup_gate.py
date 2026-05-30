"""Tests for PR-A4: the first-run wizard gate on launch_dread_client.

Existing test_launch_dreadap.py covers the .dreadap parse + arg-build
path. This file adds coverage for the new first-run gate that pops the
Kivy setup wizard when setup_state.json is missing or incomplete.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import dread  # noqa: E402


def _install_launch_stubs(monkeypatch) -> dict:
    """Mirror test_launch_dreadap._install_launch_stubs, but also stub
    subprocess.Popen so the wizard spawn doesn't actually fork."""
    recorder: dict = {"client": None, "popen": []}

    wl = types.ModuleType("worlds.LauncherComponents")

    def _fake_launch(func, name=None, args=None):
        recorder["client"] = {"name": name, "args": list(args or [])}

    wl.launch = _fake_launch  # type: ignore[attr-defined]
    worlds_pkg = sys.modules.get("worlds") or types.ModuleType("worlds")
    monkeypatch.setitem(sys.modules, "worlds", worlds_pkg)
    monkeypatch.setitem(sys.modules, "worlds.LauncherComponents", wl)

    fake_main = types.ModuleType("dread.client.main")
    fake_main.launch = lambda *a: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dread.client.main", fake_main)

    import subprocess
    def _fake_popen(argv, *a, **kw):
        recorder["popen"].append(list(argv))
        return types.SimpleNamespace(pid=12345)
    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    return recorder


def _point_setup_state_at(monkeypatch, path: Path) -> None:
    """Stub setup_state_path so _setup_state_incomplete reads from tmp."""
    import dread._setup as setup_pkg
    monkeypatch.setattr(setup_pkg, "setup_state_path", lambda: path)


def test_first_run_no_state_pops_wizard(tmp_path, monkeypatch):
    state_file = tmp_path / "setup_state.json"  # never written
    _point_setup_state_at(monkeypatch, state_file)
    recorder = _install_launch_stubs(monkeypatch)
    dread.launch_dread_client()
    assert recorder["client"] is not None
    assert recorder["client"]["args"] == []
    # The wizard subprocess was spawned (Popen called exactly once).
    assert len(recorder["popen"]) == 1
    spawned = " ".join(recorder["popen"][0])
    assert "run_setup_wizard" in spawned


def test_first_run_complete_state_skips_wizard(tmp_path, monkeypatch):
    state_file = tmp_path / "setup_state.json"
    state_file.write_text(json.dumps({
        "romfs_path": str(tmp_path / "romfs"),
        "deploy_target": "ryujinx",
        "ryujinx_root": str(tmp_path / "Ryujinx"),
    }), encoding="utf-8")
    _point_setup_state_at(monkeypatch, state_file)
    recorder = _install_launch_stubs(monkeypatch)
    dread.launch_dread_client()
    assert recorder["client"] is not None
    # State is complete → no wizard subprocess.
    assert recorder["popen"] == []


def test_first_run_missing_romfs_path_pops_wizard(tmp_path, monkeypatch):
    state_file = tmp_path / "setup_state.json"
    state_file.write_text(json.dumps({
        "deploy_target": "ryujinx",
        "ryujinx_root": str(tmp_path / "Ryujinx"),
        # romfs_path absent
    }), encoding="utf-8")
    _point_setup_state_at(monkeypatch, state_file)
    recorder = _install_launch_stubs(monkeypatch)
    dread.launch_dread_client()
    assert len(recorder["popen"]) == 1


def test_first_run_missing_deploy_target_pops_wizard(tmp_path, monkeypatch):
    state_file = tmp_path / "setup_state.json"
    state_file.write_text(json.dumps({
        "romfs_path": str(tmp_path / "romfs"),
        # deploy_target absent
    }), encoding="utf-8")
    _point_setup_state_at(monkeypatch, state_file)
    recorder = _install_launch_stubs(monkeypatch)
    dread.launch_dread_client()
    assert len(recorder["popen"]) == 1


def test_first_run_unreadable_state_pops_wizard(tmp_path, monkeypatch):
    state_file = tmp_path / "setup_state.json"
    state_file.write_text("not valid json", encoding="utf-8")
    _point_setup_state_at(monkeypatch, state_file)
    recorder = _install_launch_stubs(monkeypatch)
    dread.launch_dread_client()
    assert len(recorder["popen"]) == 1


def test_first_run_popen_failure_does_not_block_client(
    tmp_path, monkeypatch
):
    """A subprocess.Popen failure for the wizard must not block the
    DreadClient launch — wizard popping is opportunistic, the client is
    the user-facing primary."""
    state_file = tmp_path / "setup_state.json"  # missing → would pop wizard
    _point_setup_state_at(monkeypatch, state_file)
    recorder = _install_launch_stubs(monkeypatch)
    # Override the patched Popen to raise.
    import subprocess
    def _raising_popen(*a, **kw):
        raise OSError("no exec")
    monkeypatch.setattr(subprocess, "Popen", _raising_popen)
    dread.launch_dread_client()
    # DreadClient still launched.
    assert recorder["client"] is not None
