"""Unit tests for ``_setup.prereqs``.

Strategy: monkeypatch ``prereqs._safe_run`` so detectors take a scripted
result instead of shelling out. Each detector test asserts the resulting
``PrereqResult.ok`` + key + auto_installable, plus a substring of the
detail line so the assertion remains stable across phrasing tweaks.

Path-existence detectors (``check_devkitpro``, ``check_open_dread_rando``)
also use ``tmp_path`` + monkeypatch so a green dev workstation does NOT
make an "is X installed?" detector pass by accident.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread._setup import prereqs  # noqa: E402


# ---- check_python312 ----------------------------------------------------

def test_python312_winget_launcher_path_resolves(tmp_path, monkeypatch):
    """When the winget Launcher py.exe exists and reports a version, the
    row is OK and auto-installable."""
    localapp = tmp_path / "LocalAppData"
    launcher = localapp / "Programs" / "Python" / "Launcher" / "py.exe"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(localapp))

    def fake(cmd):
        if cmd[0] == str(launcher):
            return (0, "Python 3.12.7\n", "")
        return None

    monkeypatch.setattr(prereqs, "_safe_run", fake)
    r = prereqs.check_python312()
    assert r.ok is True
    assert r.key == "python312"
    assert r.auto_installable is True
    assert "Python 3.12" in r.detail


def test_python312_falls_through_to_py_dash_three_twelve(tmp_path, monkeypatch):
    """No winget paths but py -3.12 --version works."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    def fake(cmd):
        if cmd[:2] == ["py", "-3.12"]:
            return (0, "Python 3.12.4\n", "")
        return None

    monkeypatch.setattr(prereqs, "_safe_run", fake)
    r = prereqs.check_python312()
    assert r.ok is True
    assert "Python 3.12" in r.detail


def test_python312_nothing_resolves_returns_not_found(tmp_path, monkeypatch):
    """Every probe returns None: row is failed, install URL surfaced."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(prereqs, "_safe_run", lambda _cmd: None)
    r = prereqs.check_python312()
    assert r.ok is False
    assert r.key == "python312"
    assert r.install_url == prereqs.INSTALL_URLS["python312"]
    assert r.auto_installable is True


# ---- check_devkitpro ----------------------------------------------------

def _make_fake_devkitpro_root(root: Path) -> None:
    """Lay out a minimally-realistic devkitPro install rooted at ``root``."""
    (root / "devkitA64" / "bin").mkdir(parents=True)
    (root / "devkitA64" / "bin" / "aarch64-none-elf-g++.exe").write_bytes(b"")
    (root / "msys2" / "usr" / "bin").mkdir(parents=True)
    (root / "msys2" / "usr" / "bin" / "bash.exe").write_bytes(b"")


def test_devkitpro_env_var_resolves(tmp_path, monkeypatch):
    """DEVKITPRO env var points at a working install: OK + env var is
    overwritten to the resolved root."""
    root = tmp_path / "devkitPro"
    _make_fake_devkitpro_root(root)
    monkeypatch.setenv("DEVKITPRO", str(root))
    monkeypatch.setattr(
        prereqs, "_safe_run",
        lambda cmd: (0, "g++ (devkitPro) 13.2.0", "") if "g++" in cmd[0].replace("\\", "/") else None,
    )
    r = prereqs.check_devkitpro()
    assert r.ok is True
    assert r.key == "devkitpro"
    assert r.auto_installable is False
    assert prereqs.os.environ.get("DEVKITPRO") == str(root)


def test_devkitpro_env_var_bogus_falls_back_to_default(tmp_path, monkeypatch):
    """DEVKITPRO=/opt/devkitpro (msys2-form, missing on Win): falls
    through to the default roots tuple."""
    fake_default = tmp_path / "devkitPro"
    _make_fake_devkitpro_root(fake_default)
    monkeypatch.setenv("DEVKITPRO", "/opt/devkitpro")
    monkeypatch.setattr(
        prereqs, "_DEVKITPRO_DEFAULT_ROOTS", (fake_default,),
    )
    monkeypatch.setattr(
        prereqs, "_safe_run",
        lambda cmd: (0, "g++ (devkitPro) 13.2.0", "") if "g++" in cmd[0].replace("\\", "/") else None,
    )
    r = prereqs.check_devkitpro()
    assert r.ok is True
    assert prereqs.os.environ["DEVKITPRO"] == str(fake_default)


def test_devkitpro_msys2_missing_on_windows_fails(tmp_path, monkeypatch):
    """On Windows, devkitPro must include the msys2 component."""
    root = tmp_path / "devkitPro"
    (root / "devkitA64" / "bin").mkdir(parents=True)
    (root / "devkitA64" / "bin" / "aarch64-none-elf-g++.exe").write_bytes(b"")
    monkeypatch.setenv("DEVKITPRO", str(root))
    monkeypatch.setattr(
        prereqs, "_safe_run",
        lambda cmd: (0, "g++ (devkitPro) 13.2.0", "") if "g++" in cmd[0].replace("\\", "/") else None,
    )
    monkeypatch.setattr(prereqs.os, "name", "nt")
    r = prereqs.check_devkitpro()
    assert r.ok is False
    assert "msys2" in r.detail


def test_devkitpro_binary_runs_but_version_fails(tmp_path, monkeypatch):
    """g++ exists but --version returns non-zero: failure with the
    actionable hint."""
    root = tmp_path / "devkitPro"
    _make_fake_devkitpro_root(root)
    monkeypatch.setenv("DEVKITPRO", str(root))
    monkeypatch.setattr(prereqs, "_safe_run",
                        lambda cmd: (1, "", "exec format error"))
    r = prereqs.check_devkitpro()
    assert r.ok is False
    assert "--version" in r.detail


def test_devkitpro_nothing_found_returns_install_url(tmp_path, monkeypatch):
    """No env var, no default roots usable: failure with install URL."""
    monkeypatch.delenv("DEVKITPRO", raising=False)
    monkeypatch.setattr(prereqs, "_DEVKITPRO_DEFAULT_ROOTS",
                        (tmp_path / "does-not-exist",))
    monkeypatch.setattr(prereqs, "_safe_run", lambda _cmd: None)
    r = prereqs.check_devkitpro()
    assert r.ok is False
    assert r.install_url == prereqs.INSTALL_URLS["devkitpro"]
    assert r.auto_installable is False


# ---- check_open_dread_rando --------------------------------------------

def test_open_dread_rando_importable(monkeypatch):
    """The probe subprocess returns 0 + a version: row is OK."""
    monkeypatch.setattr(prereqs, "_safe_run",
                        lambda _cmd: (0, "0.10.4\n", ""))
    r = prereqs.check_open_dread_rando(python="/fake/python.exe")
    assert r.ok is True
    assert r.key == "open_dread_rando"
    assert "0.10.4" in r.detail


def test_open_dread_rando_not_importable_surfaces_pip_command(monkeypatch):
    """The subprocess exits non-zero: failure includes a pip-install note
    pointing at the chosen Python."""
    monkeypatch.setattr(
        prereqs, "_safe_run",
        lambda _cmd: (1, "", "ModuleNotFoundError: open_dread_rando"),
    )
    r = prereqs.check_open_dread_rando(python="/fake/python.exe")
    assert r.ok is False
    assert "open-dread-rando" in r.note or "open_dread_rando" in r.note
    assert "/fake/python.exe" in r.note


def test_open_dread_rando_no_python_returns_install_python312_hint(monkeypatch):
    """When no Python is detected, the row suggests installing Python 3.12
    first."""
    monkeypatch.setattr(prereqs, "candidate_pythons", lambda: [])
    r = prereqs.check_open_dread_rando(python=None)
    assert r.ok is False
    assert "Python 3.12" in r.detail


def test_open_dread_rando_enumerates_multiple_pythons(monkeypatch):
    """When multiple Pythons are detected and ANY has open-dread-rando, the
    row goes OK pointing at that interpreter — the user may have installed
    into a Python the wizard wouldn't have picked as its first choice."""
    candidates = ["/fake/badpy.exe", "/fake/goodpy.exe", "/fake/otherpy.exe"]
    monkeypatch.setattr(prereqs, "candidate_pythons", lambda: candidates)

    def _fake_safe_run(cmd):
        # cmd is [python, "-c", probe_source]; succeed only for goodpy.
        if cmd[0] == "/fake/goodpy.exe":
            return (0, "0.10.4\n", "")
        return (1, "", "ModuleNotFoundError: open_dread_rando")

    monkeypatch.setattr(prereqs, "_safe_run", _fake_safe_run)
    r = prereqs.check_open_dread_rando(python=None)
    assert r.ok is True
    assert "/fake/goodpy.exe" in r.detail


def test_open_dread_rando_miss_targets_first_candidate(monkeypatch):
    """When none of the detected Pythons has it, the install hint targets
    the first (highest-priority) candidate so re-probe lands on the same
    interpreter pip installed into."""
    candidates = ["/fake/firstpy.exe", "/fake/secondpy.exe"]
    monkeypatch.setattr(prereqs, "candidate_pythons", lambda: candidates)
    monkeypatch.setattr(
        prereqs, "_safe_run",
        lambda _cmd: (1, "", "ModuleNotFoundError: open_dread_rando"),
    )
    r = prereqs.check_open_dread_rando(python=None)
    assert r.ok is False
    assert "/fake/firstpy.exe -m pip install open-dread-rando" in r.note


# ---- check_all + all_ok -------------------------------------------------

def test_check_all_returns_three_results_in_display_order(monkeypatch):
    """check_all produces one PrereqResult per detector in wizard-display
    order (devkitPro, Python, open_dread_rando)."""
    monkeypatch.setattr(prereqs, "check_devkitpro",
                        lambda: prereqs.PrereqResult("devkitpro", "devkitPro / devkitA64",
                                                     True, "ok"))
    monkeypatch.setattr(prereqs, "check_python312",
                        lambda: prereqs.PrereqResult("python312", "Python 3.12",
                                                     True, "ok"))
    monkeypatch.setattr(prereqs, "check_open_dread_rando",
                        lambda python=None: prereqs.PrereqResult(
                            "open_dread_rando",
                            "open-dread-rando (Python patcher)",
                            True, "ok"))
    results = prereqs.check_all()
    assert [r.key for r in results] == [
        "devkitpro", "python312", "open_dread_rando",
    ]


def test_all_ok_aggregator():
    """all_ok is True iff every PrereqResult.ok is True."""
    ok = [prereqs.PrereqResult("a", "A", True, ""),
          prereqs.PrereqResult("b", "B", True, "")]
    mixed = [prereqs.PrereqResult("a", "A", True, ""),
             prereqs.PrereqResult("b", "B", False, "")]
    assert prereqs.all_ok(ok) is True
    assert prereqs.all_ok(mixed) is False


def test_check_all_accepts_vestigial_overrides(monkeypatch):
    """check_all takes hactool_override / prod_keys_override kwargs purely
    for smo-import-compat; passing them must not raise."""
    monkeypatch.setattr(prereqs, "check_devkitpro",
                        lambda: prereqs.PrereqResult("devkitpro", "X", True, "ok"))
    monkeypatch.setattr(prereqs, "check_python312",
                        lambda: prereqs.PrereqResult("python312", "Y", True, "ok"))
    monkeypatch.setattr(prereqs, "check_open_dread_rando",
                        lambda python=None: prereqs.PrereqResult(
                            "open_dread_rando", "Z", True, "ok"))
    results = prereqs.check_all(
        hactool_override=Path("/fake"), prod_keys_override=Path("/fake"))
    assert len(results) == 3
