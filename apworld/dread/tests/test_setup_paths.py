"""Unit tests for ``_setup`` path helpers.

Focus: ``build_dir`` must relocate the devkitPro build tree out of any
path containing a space (a Windows username like "Vod Kanakas"), because
make re-derives the cwd unquoted inside the Makefile and dies with
``make: *** /c/Users/Vod: Is a directory. Stop.`` See ``build_dir``'s
docstring for the full diagnosis.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread import _setup  # noqa: E402


# ---- build_dir: no-space common case -----------------------------------

def test_build_dir_uses_appdata_when_no_space(tmp_path, monkeypatch):
    appdata = tmp_path / "AppData" / "Roaming" / "dread_ap"
    monkeypatch.setattr(_setup, "appdata_root", lambda: appdata)
    appdata.mkdir(parents=True)

    d = _setup.build_dir()

    assert d == appdata / "build"
    assert d.is_dir()


# ---- build_dir: spaced username relocates ------------------------------

def test_build_dir_relocates_when_appdata_has_space(tmp_path, monkeypatch):
    spaced = tmp_path / "Vod Kanakas" / "AppData" / "Roaming" / "dread_ap"
    spaced.mkdir(parents=True)
    monkeypatch.setattr(_setup, "appdata_root", lambda: spaced)

    public = tmp_path / "Public"  # space-free relocation target
    monkeypatch.setenv("PUBLIC", str(public))

    d = _setup.build_dir()

    assert " " not in str(d), f"relocated build dir still has a space: {d}"
    assert d == public / "dread_ap" / "build"
    assert d.is_dir()


# ---- _space_free_build_root preference order ---------------------------

def test_space_free_root_prefers_public(tmp_path, monkeypatch):
    public = tmp_path / "Public"
    programdata = tmp_path / "ProgramData"
    monkeypatch.setenv("PUBLIC", str(public))
    monkeypatch.setenv("ProgramData", str(programdata))

    root = _setup._space_free_build_root()

    assert root == public / "dread_ap"


def test_space_free_root_skips_spaced_candidate(tmp_path, monkeypatch):
    # PUBLIC itself contains a space (pathological, but make would choke on
    # it too) → must be skipped in favour of the space-free ProgramData.
    spaced_public = tmp_path / "Pub lic"
    programdata = tmp_path / "ProgramData"
    monkeypatch.setenv("PUBLIC", str(spaced_public))
    monkeypatch.setenv("ProgramData", str(programdata))

    root = _setup._space_free_build_root()

    assert " " not in str(root)
    assert root == programdata / "dread_ap"


def test_space_free_root_falls_back_to_system_drive(tmp_path, monkeypatch):
    monkeypatch.delenv("PUBLIC", raising=False)
    monkeypatch.delenv("ProgramData", raising=False)
    # Point SystemDrive at a writable space-free tmp location.
    monkeypatch.setenv("SystemDrive", str(tmp_path / "sysroot"))

    root = _setup._space_free_build_root()

    assert " " not in str(root)
    assert root.name == "dread_ap"
    assert root.is_dir()
