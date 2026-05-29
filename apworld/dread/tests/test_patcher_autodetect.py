"""Tests for patcher-Python autodetection (patcher_pipeline)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread import patcher_pipeline as pp  # noqa: E402


def test_autodetect_no_candidates(monkeypatch):
    monkeypatch.setattr(pp, "_candidate_pythons", lambda: [])
    path, msg = pp.autodetect_patcher_python()
    assert path is None
    assert "Install Python" in msg


def test_autodetect_returns_first_with_deps(monkeypatch):
    monkeypatch.setattr(pp, "_candidate_pythons", lambda: ["badpy", "goodpy", "otherpy"])
    monkeypatch.setattr(pp, "check_dependencies",
                        lambda p: None if p == "goodpy" else "missing")
    path, msg = pp.autodetect_patcher_python()
    assert path == "goodpy"
    assert "goodpy" in msg


def test_autodetect_all_missing_returns_pip_command(monkeypatch):
    monkeypatch.setattr(pp, "_candidate_pythons", lambda: ["firstpy", "secondpy"])
    monkeypatch.setattr(pp, "check_dependencies", lambda p: "missing")
    path, msg = pp.autodetect_patcher_python()
    assert path is None
    # Names the best (first) candidate in the actionable command.
    assert "firstpy -m pip install open-dread-rando" in msg


def test_candidate_pythons_excludes_frozen_launcher(tmp_path, monkeypatch):
    launcher = tmp_path / "ArchipelagoLauncher.exe"
    launcher.write_text("")
    real = tmp_path / "python.exe"
    real.write_text("")
    # Pretend we're running from the frozen launcher; a real Python is on PATH.
    monkeypatch.setattr(pp.sys, "executable", str(launcher))
    monkeypatch.setattr(pp.shutil, "which",
                        lambda name: str(real) if name in ("py", "python", "python3") else None)
    # Skip the win32-only `py -3` subprocess + LOCALAPPDATA glob branch.
    monkeypatch.setattr(pp.sys, "platform", "linux")

    cands = pp._candidate_pythons()
    assert str(real.resolve()) in cands
    assert all("ArchipelagoLauncher" not in c for c in cands)


def test_candidate_pythons_dedupes(tmp_path, monkeypatch):
    real = tmp_path / "python.exe"
    real.write_text("")
    monkeypatch.setattr(pp.sys, "executable", str(real))
    monkeypatch.setattr(pp.shutil, "which",
                        lambda name: str(real) if name in ("py", "python", "python3") else None)
    monkeypatch.setattr(pp.sys, "platform", "linux")

    cands = pp._candidate_pythons()
    assert cands.count(str(real.resolve())) == 1
