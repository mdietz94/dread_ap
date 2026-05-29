"""Tests for launch_dread_client's .dreadap handling (apworld __init__)."""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

import dread  # noqa: E402
from dread.client.dreadap_file import DreadapFile  # noqa: E402


def _install_launch_stubs(monkeypatch) -> dict:
    """Stub worlds.LauncherComponents.launch and dread.client.main so
    launch_dread_client runs without the AP runtime / Kivy. Returns a recorder
    dict the fake launch fills in."""
    recorder: dict = {}

    wl = types.ModuleType("worlds.LauncherComponents")

    def _fake_launch(func, name=None, args=None):
        recorder["name"] = name
        recorder["args"] = list(args or [])

    wl.launch = _fake_launch  # type: ignore[attr-defined]
    worlds_pkg = sys.modules.get("worlds") or types.ModuleType("worlds")
    monkeypatch.setitem(sys.modules, "worlds", worlds_pkg)
    monkeypatch.setitem(sys.modules, "worlds.LauncherComponents", wl)

    fake_main = types.ModuleType("dread.client.main")
    fake_main.launch = lambda *a: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dread.client.main", fake_main)
    return recorder


def test_button_launch_no_args(monkeypatch):
    recorder = _install_launch_stubs(monkeypatch)
    dread.launch_dread_client()
    assert recorder["args"] == []


def test_dreadap_arg_expands_to_name(monkeypatch, tmp_path):
    recorder = _install_launch_stubs(monkeypatch)
    path = tmp_path / "Samus.dreadap"
    DreadapFile(slot_name="Samus", seed_name="SEED").write(path)
    dread.launch_dread_client(str(path))
    # The .dreadap path is stripped; --name is prepended.
    assert recorder["args"] == ["--name", "Samus"]


def test_dreadap_arg_with_server(monkeypatch, tmp_path):
    recorder = _install_launch_stubs(monkeypatch)
    path = tmp_path / "Samus.dreadap"
    DreadapFile(slot_name="Samus", server_address="ap.gg:38281").write(path)
    dread.launch_dread_client(str(path))
    assert recorder["args"] == ["--name", "Samus", "--connect", "ap.gg:38281"]


def test_bad_dreadap_does_not_block_launch(monkeypatch, tmp_path):
    recorder = _install_launch_stubs(monkeypatch)
    bad = tmp_path / "broken.dreadap"
    bad.write_text("not valid json or zip")
    dread.launch_dread_client(str(bad))
    # The unparseable .dreadap is stripped; the client still launches unfilled.
    assert recorder["args"] == []
