"""Regression tests for patch()'s output-path handling + exefs re-assert.

Guards the fix for the nested-mod bug: the upstream open-dread-rando patcher,
in RYUJINX compatibility mode, appends a ``DreadRandovania`` segment to
``--output-path``. Our callers already include that leaf, so patch() must hand
the patcher the PARENT (else output double-nests to
``.../DreadRandovania/DreadRandovania`` and Ryujinx loads the patcher's bundled
upstream server-mode subsdk9 — port 6969 — instead of our patched TCP-client
build). patch() must then re-assert ``exefs_overlay`` over the upstream subsdk9
the patcher wrote, or the Switch never dials the client.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from dread import patcher_pipeline as pp  # noqa: E402


class _FakeProc:
    returncode = 0
    stdout = ""
    stderr = ""


def _minimal_placements() -> dict:
    # build_patcher_input_from_placements loads the bundled starter template
    # (mod_compatibility == "ryujinx"); only slot_name + placements are needed.
    return {"slot_name": "Tester", "seed_id": "deadbeef", "placements": []}


def _patch_with_fakes(monkeypatch, dreadvania_dir: Path, romfs_dir: Path,
                      exefs_overlay=None):
    """Run patch() with deps + subprocess mocked; return (result, captured_cmd)."""
    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(pp, "check_dependencies", lambda py=None: None)
    monkeypatch.setattr(pp.subprocess, "run", _fake_run)

    result = pp.patch(
        placements=_minimal_placements(),
        dreadvania_install_dir=dreadvania_dir,
        vanilla_romfs_dir=romfs_dir,
        python_executable="py",
        exefs_overlay=exefs_overlay,
    )
    return result, captured.get("cmd", [])


def _output_path_from(cmd: list[str]) -> Path:
    i = cmd.index("--output-path")
    return Path(cmd[i + 1])


def test_ryujinx_output_path_is_parent_not_the_mod_dir(monkeypatch, tmp_path):
    """When the install dir ends in DreadRandovania, --output-path is the
    PARENT so the patcher's append re-creates exactly that dir (no nesting)."""
    mod_dir = tmp_path / "mods" / "contents" / "010093801237c000" / "DreadRandovania"
    mod_dir.mkdir(parents=True)
    romfs = tmp_path / "romfs"
    romfs.mkdir()

    result, cmd = _patch_with_fakes(monkeypatch, mod_dir, romfs)

    assert result.ok, result.message
    out = _output_path_from(cmd)
    # Parent — so patcher append yields exactly mod_dir, never mod_dir/DreadRandovania.
    assert out == mod_dir.parent.resolve()
    assert out.name == "010093801237c000"


def test_exefs_overlay_overwrites_upstream_subsdk9(monkeypatch, tmp_path):
    """Our patched subsdk9 is re-asserted over whatever the patcher wrote."""
    mod_dir = tmp_path / "mods" / "contents" / "010093801237c000" / "DreadRandovania"
    (mod_dir / "exefs").mkdir(parents=True)
    romfs = tmp_path / "romfs"
    romfs.mkdir()

    # Simulate the patcher having written its UPSTREAM (6969) subsdk9 + an IPS.
    (mod_dir / "exefs" / "subsdk9").write_bytes(b"UPSTREAM-SERVER-MODE")
    (mod_dir / "exefs" / "HasRandomizerPatches.ips").write_bytes(b"IPS")

    # Our locally-built patched outputs.
    build = tmp_path / "build"
    build.mkdir()
    (build / "subsdk9").write_bytes(b"PATCHED-CLIENT-MODE")
    (build / "main.npdm").write_bytes(b"NPDM")
    overlay = {"subsdk9": build / "subsdk9", "main.npdm": build / "main.npdm"}

    result, _ = _patch_with_fakes(monkeypatch, mod_dir, romfs, exefs_overlay=overlay)

    assert result.ok, result.message
    # Our patched subsdk9 won; main.npdm landed; the patcher's IPS is untouched.
    assert (mod_dir / "exefs" / "subsdk9").read_bytes() == b"PATCHED-CLIENT-MODE"
    assert (mod_dir / "exefs" / "main.npdm").read_bytes() == b"NPDM"
    assert (mod_dir / "exefs" / "HasRandomizerPatches.ips").read_bytes() == b"IPS"
    assert any("re-asserted patched sysmodule" in n for n in result.notes)


def test_no_overlay_leaves_exefs_as_patcher_wrote_it(monkeypatch, tmp_path):
    """exefs_overlay=None → no re-assert, no crash (degraded but explicit)."""
    mod_dir = tmp_path / "mods" / "contents" / "010093801237c000" / "DreadRandovania"
    (mod_dir / "exefs").mkdir(parents=True)
    romfs = tmp_path / "romfs"
    romfs.mkdir()
    (mod_dir / "exefs" / "subsdk9").write_bytes(b"UPSTREAM-SERVER-MODE")

    result, _ = _patch_with_fakes(monkeypatch, mod_dir, romfs, exefs_overlay=None)

    assert result.ok, result.message
    assert (mod_dir / "exefs" / "subsdk9").read_bytes() == b"UPSTREAM-SERVER-MODE"
    assert result.notes == []
