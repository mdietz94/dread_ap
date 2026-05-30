"""Tests for PR-A3: ``/patch`` and ``/patch_python`` are now deprecation
shims that point users at ``/setup``."""
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
        switch_host="127.0.0.1",
    )
    p = DreadClientCommandProcessor(ctx)
    p.outputs = []
    p.output = p.outputs.append  # type: ignore[assignment]
    return p


def test_cmd_patch_emits_deprecation_message(proc):
    """``/patch`` (no args) prints the deprecation message pointing at
    ``/setup`` and returns True (handled)."""
    assert proc._cmd_patch() is True
    msg = " ".join(proc.outputs)
    assert "/patch is deprecated" in msg
    assert "/setup" in msg


def test_cmd_patch_with_args_still_emits_deprecation(proc):
    """Old-style ``/patch <dv-dir> <romfs-dir>`` invocations also hit the
    shim — the args are accepted and ignored so a muscle-memory typer
    doesn't see an unknown-command error."""
    assert proc._cmd_patch("/dv/dir", "/romfs/dir") is True
    msg = " ".join(proc.outputs)
    assert "/patch is deprecated" in msg


def test_cmd_patch_python_emits_deprecation_message(proc):
    """``/patch_python`` (no args) prints the deprecation pointer at
    ``/setup``'s Prereqs page."""
    assert proc._cmd_patch_python() is True
    msg = " ".join(proc.outputs)
    assert "/patch_python is deprecated" in msg
    assert "/setup" in msg


def test_cmd_patch_python_with_path_arg_still_emits_deprecation(proc):
    """The shim accepts the legacy positional path argument so users who
    pasted ``/patch_python C:\\Python312\\python.exe`` see the
    deprecation message rather than a TypeError."""
    assert proc._cmd_patch_python("C:/Python312/python.exe") is True
    msg = " ".join(proc.outputs)
    assert "/patch_python is deprecated" in msg


def test_help_text_advertises_setup_not_patch():
    """``commands.HELP_TEXT`` references ``/setup``, not the deprecated
    ``/patch`` command."""
    from dread.client.commands import HELP_TEXT  # noqa: E402
    assert "/setup" in HELP_TEXT
    # The deprecated commands should not appear in the help list — typing
    # ``help`` should only surface live commands.
    assert "/patch <" not in HELP_TEXT
