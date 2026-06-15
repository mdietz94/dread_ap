"""Tests for reading the ``dreadvania_python`` host.yaml setting.

``dreadvania_python`` is an AP ``OptionalUserFilePath``, which resolves a
blank value relative to the user-data dir — i.e. to the Archipelago folder
itself (a directory). ``_settings_dreadvania_python`` must treat such a
directory (and an empty value) as "unset" so the client auto-detects an
interpreter instead of trying to exec a folder.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client import context as ctx  # noqa: E402


def _install_fake_settings(monkeypatch, value: str) -> None:
    """Stub the ``settings`` module so get_settings()['dread_options']
    ['dreadvania_python'] returns ``value``."""
    fake = types.ModuleType("settings")
    fake.get_settings = lambda: {"dread_options": {"dreadvania_python": value}}
    monkeypatch.setitem(sys.modules, "settings", fake)


def test_blank_resolves_to_none(monkeypatch):
    _install_fake_settings(monkeypatch, "")
    assert ctx._settings_dreadvania_python() is None


def test_directory_resolves_to_none(monkeypatch, tmp_path):
    # The blank OptionalUserFilePath default resolves to the user folder (a
    # directory with a trailing slash). That must NOT be handed on as a Python.
    _install_fake_settings(monkeypatch, str(tmp_path) + "/")
    assert ctx._settings_dreadvania_python() is None


def test_real_file_path_passes_through(monkeypatch, tmp_path):
    py = tmp_path / "python3"
    py.write_text("")
    _install_fake_settings(monkeypatch, str(py))
    assert ctx._settings_dreadvania_python() == str(py)


def test_nonexistent_file_passes_through(monkeypatch, tmp_path):
    # A typo'd interpreter path is still returned so the downstream
    # dependency check can surface "configured Python not found".
    typo = tmp_path / "pythn3"
    _install_fake_settings(monkeypatch, str(typo))
    assert ctx._settings_dreadvania_python() == str(typo)


def test_missing_settings_module_resolves_to_none(monkeypatch):
    monkeypatch.setitem(sys.modules, "settings", None)
    assert ctx._settings_dreadvania_python() is None
