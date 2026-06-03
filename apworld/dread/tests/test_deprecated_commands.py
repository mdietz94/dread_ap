"""Tests for removed client commands.

``/patch`` and ``/patch_python`` were removed entirely (no shim) once the
wizard's auto-detect flow + auto-patch-on-connect became the only
configuration path. The DeathLink test commands ``/send_deathlink`` and
``/kill_switch`` were likewise removed after DeathLink was validated. Typing
any of these now yields the standard "Unknown command" response, which is the
expected behavior."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client.datapackage import DataPackage  # noqa: E402
from dread.client.state import BridgeState  # noqa: E402


@pytest.fixture
def proc():
    from dread.client.context import (  # noqa: E402
        DreadClientCommandProcessor,
        DreadContext,
    )
    ctx = DreadContext(
        server_address=None,
        password=None,
        state=BridgeState(),
        datapackage=DataPackage(apworld_data_dir=ROOT / "data"),
        bridge_port=0,
        discovery_port=0,
    )
    p = DreadClientCommandProcessor(ctx)
    p.outputs = []
    p.output = p.outputs.append  # type: ignore[assignment]
    return p


def test_patch_command_is_gone(proc):
    """``/patch`` was removed entirely; the command handler must not exist."""
    assert not hasattr(proc, "_cmd_patch")


def test_patch_python_command_is_gone(proc):
    """``/patch_python`` was removed; the command handler must not exist."""
    assert not hasattr(proc, "_cmd_patch_python")


def test_deathlink_test_commands_are_gone(proc):
    """The DeathLink test commands were removed after validation."""
    assert not hasattr(proc, "_cmd_send_deathlink")
    assert not hasattr(proc, "_cmd_kill_switch")


def test_help_text_omits_removed_commands():
    """``commands.HELP_TEXT`` references ``/setup`` and lists only live
    commands — none of the removed ones appear."""
    from dread.client.commands import HELP_TEXT  # noqa: E402
    assert "/setup" in HELP_TEXT
    assert "/patch" not in HELP_TEXT
    assert "/send_deathlink" not in HELP_TEXT
    assert "/kill_switch" not in HELP_TEXT
