"""Import-smoke for the Kivy GUI module.

gui.py pulls kvui/Kivy, which isn't present in headless unit-test isolation,
so this auto-skips there. When Kivy IS available (e.g. running inside an
Archipelago checkout) it catches import/syntax errors and confirms the public
classes exist — instantiating a GameManager needs a running Kivy app, which is
out of scope for a smoke test.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def test_gui_module_imports_when_kivy_present():
    pytest.importorskip("kvui")
    from dread.client import gui

    assert hasattr(gui, "DreadManager")
    assert hasattr(gui, "ReconnectPopup")
    # The log pane tails the client package logger; confirm the derivation
    # resolves to the shared "<pkg>.client" parent (not gui itself).
    assert gui._CLIENT_LOGGER.endswith(".client")
