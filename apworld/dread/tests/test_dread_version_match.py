"""Tests for the connect-time Dread-version mismatch detector.

When the setup wizard persists ``romfs_version`` in ``setup_state.json``,
``DreadContext`` compares it to the live ``GameVersion`` reported by the
Switch's API probe and surfaces a loud warning if they disagree.

Covered:

* No saved state → no expectation, no warning (clean install case).
* Corrupt state → ditto (don't crash on a bad file).
* ``romfs_version`` absent or unknown → ditto.
* Match (with or without ``v`` prefix) → no warning.
* Real mismatch → warning string mentions both versions + ``/setup``.

The helpers are pure functions of the JSON file + the version string so
they're easy to exercise without spinning up a real session.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client import context as dread_context  # noqa: E402


def _write(path: Path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    p = tmp_path / "setup_state.json"
    monkeypatch.setattr(dread_context, "setup_state_path", lambda: p)
    return p


def test_no_state_file_skips_check(state_file):
    # state_file does not exist on disk
    assert dread_context._expected_romfs_version() is None
    assert dread_context._check_dread_version_match("2.1.0") is None


def test_corrupt_state_skips_check(state_file):
    state_file.write_text("{this is not json", encoding="utf-8")
    assert dread_context._expected_romfs_version() is None
    assert dread_context._check_dread_version_match("2.1.0") is None


def test_state_without_romfs_version_skips(state_file):
    _write(state_file, {"romfs_path": "/wherever"})
    assert dread_context._expected_romfs_version() is None
    assert dread_context._check_dread_version_match("2.1.0") is None


def test_unknown_version_value_skips(state_file):
    # Reject garbage values rather than treat them as expectations.
    _write(state_file, {"romfs_version": "9.9.9"})
    assert dread_context._expected_romfs_version() is None
    assert dread_context._check_dread_version_match("2.1.0") is None


def test_exact_match_2_1_0(state_file):
    _write(state_file, {"romfs_version": "2.1.0"})
    assert dread_context._check_dread_version_match("2.1.0") is None


def test_v_prefixed_live_string_still_matches(state_file):
    _write(state_file, {"romfs_version": "2.1.0"})
    assert dread_context._check_dread_version_match("v2.1.0") is None


def test_exact_match_1_0_0(state_file):
    _write(state_file, {"romfs_version": "1.0.0"})
    assert dread_context._check_dread_version_match("1.0.0") is None


def test_mismatch_returns_warning_mentioning_both_versions(state_file):
    _write(state_file, {"romfs_version": "1.0.0"})
    warning = dread_context._check_dread_version_match("2.1.0")
    assert warning is not None
    assert "1.0.0" in warning
    assert "2.1.0" in warning
    assert "/setup" in warning


def test_mismatch_other_direction(state_file):
    _write(state_file, {"romfs_version": "2.1.0"})
    warning = dread_context._check_dread_version_match("1.0.0")
    assert warning is not None
    assert "1.0.0" in warning
    assert "2.1.0" in warning


def test_normalize_strips_leading_v():
    assert dread_context._normalize_game_version("v2.1.0") == "2.1.0"
    assert dread_context._normalize_game_version("V1.0.0") == "1.0.0"
    assert dread_context._normalize_game_version("  2.1.0 ") == "2.1.0"
    assert dread_context._normalize_game_version("2.1.0") == "2.1.0"
