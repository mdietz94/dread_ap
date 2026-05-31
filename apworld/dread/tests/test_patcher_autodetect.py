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
    assert str(real.absolute()) in cands
    assert all("ArchipelagoLauncher" not in c for c in cands)


def test_candidate_pythons_dedupes(tmp_path, monkeypatch):
    real = tmp_path / "python.exe"
    real.write_text("")
    monkeypatch.setattr(pp.sys, "executable", str(real))
    monkeypatch.setattr(pp.shutil, "which",
                        lambda name: str(real) if name in ("py", "python", "python3") else None)
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    cands = pp._candidate_pythons()
    assert cands.count(str(real.absolute())) == 1


def test_candidate_pythons_skips_sys_executable_when_frozen(tmp_path, monkeypatch):
    """When this process is a PyInstaller-frozen bundle (`sys.frozen` set),
    `sys.executable` points at the wrapper exe and must NOT be probed:
    its bundled site-packages can never satisfy `import open_dread_rando`,
    so probing it would just slow the wizard down. The name-based
    backstop in `_add` (via `describe_python`) only catches known
    Archipelago exe names; `sys.frozen` covers forked/renamed builds."""
    frozen_wrapper = tmp_path / "TotallyCustomFork.exe"
    frozen_wrapper.write_text("")
    real = tmp_path / "python.exe"
    real.write_text("")
    monkeypatch.setattr(pp.sys, "executable", str(frozen_wrapper))
    monkeypatch.setattr(pp.sys, "frozen", True, raising=False)
    monkeypatch.setattr(pp.shutil, "which",
                        lambda name: str(real) if name in ("py", "python", "python3") else None)
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    cands = pp._candidate_pythons()
    assert str(frozen_wrapper.absolute()) not in cands
    assert str(real.absolute()) in cands


def test_candidate_pythons_keeps_venv_distinct_from_resolved_base(tmp_path, monkeypatch):
    """A venv's bin/python is typically a symlink to the base interpreter.
    Dedup must NOT collapse them: invoking the resolved base path skips
    venv activation and misses the venv's site-packages."""
    import os as _os
    base = tmp_path / "usr_bin_python3.12"
    base.write_text("")
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    venv_py = venv / "bin" / "python"
    try:
        _os.symlink(str(base), str(venv_py))
    except (OSError, NotImplementedError):
        # No symlink support (e.g. Windows without privilege) — skip.
        import pytest
        pytest.skip("symlinks unavailable")
    monkeypatch.setattr(pp.sys, "executable", str(venv_py))
    monkeypatch.setattr(pp.shutil, "which",
                        lambda name: str(base) if name in ("py", "python", "python3") else None)
    monkeypatch.setattr(pp.sys, "platform", "linux")
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)

    cands = pp._candidate_pythons()
    assert str(venv_py.absolute()) in cands
    assert str(base.absolute()) in cands
