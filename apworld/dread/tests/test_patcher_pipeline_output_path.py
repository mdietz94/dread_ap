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
                      exefs_overlay=None, mod_compatibility=None):
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
        mod_compatibility=mod_compatibility,
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


def test_atmosphere_output_path_strips_to_atmosphere_dir(monkeypatch, tmp_path):
    """An SD/Atmosphere install dir (.../atmosphere/contents/<tid>) → the CLI
    gets the atmosphere/ dir, so the patcher's contents/<tid> append lands the
    mod flat at the install dir (NOT nested under DreadRandovania, which a real
    Switch would ignore)."""
    mod_dir = tmp_path / "sd" / "atmosphere" / "contents" / "010093801237c000"
    mod_dir.mkdir(parents=True)
    romfs = tmp_path / "romfs"
    romfs.mkdir()

    result, cmd = _patch_with_fakes(
        monkeypatch, mod_dir, romfs, mod_compatibility="atmosphere")

    assert result.ok, result.message
    out = _output_path_from(cmd)
    # The patcher appends contents/<tid>; we hand it the atmosphere/ dir so the
    # mod re-creates exactly mod_dir.
    assert out == (tmp_path / "sd" / "atmosphere").resolve()
    assert out.name == "atmosphere"


def test_atmosphere_ips_land_in_global_exefs_patches(monkeypatch, tmp_path):
    """Atmosphere reads exefs IPS from the GLOBAL exefs_patches tree (sibling
    of contents/), not from inside the title folder. The version-sentinel .ips
    must land there, and NOT under contents/<tid>/exefs."""
    mod_dir = tmp_path / "sd" / "atmosphere" / "contents" / "010093801237c000"
    mod_dir.mkdir(parents=True)
    romfs = tmp_path / "romfs"
    romfs.mkdir()

    result, _ = _patch_with_fakes(
        monkeypatch, mod_dir, romfs, mod_compatibility="atmosphere")

    assert result.ok, result.message
    global_patches = tmp_path / "sd" / "atmosphere" / "exefs_patches" / "DreadRandovania"
    ips = sorted(p.name for p in global_patches.glob("*.ips"))
    assert "646761F643AFEBB379EDD5E6A5151AF2CEF93DC1.ips" in ips
    # The title-folder exefs must NOT have collected the IPS.
    assert not list((mod_dir / "exefs").glob("*.ips"))


def test_atmosphere_overlay_lands_in_contents_exefs(monkeypatch, tmp_path):
    """The patched sysmodule (LayeredFS exefs replacement) lands in
    contents/<tid>/exefs even in Atmosphere mode, while IPS go elsewhere."""
    mod_dir = tmp_path / "sd" / "atmosphere" / "contents" / "010093801237c000"
    mod_dir.mkdir(parents=True)
    romfs = tmp_path / "romfs"
    romfs.mkdir()
    build = tmp_path / "build"
    build.mkdir()
    (build / "subsdk9").write_bytes(b"PATCHED-CLIENT-MODE")
    (build / "main.npdm").write_bytes(b"NPDM")
    overlay = {"subsdk9": build / "subsdk9", "main.npdm": build / "main.npdm"}

    result, _ = _patch_with_fakes(
        monkeypatch, mod_dir, romfs, exefs_overlay=overlay,
        mod_compatibility="atmosphere")

    assert result.ok, result.message
    assert (mod_dir / "exefs" / "subsdk9").read_bytes() == b"PATCHED-CLIENT-MODE"
    assert (mod_dir / "exefs" / "main.npdm").read_bytes() == b"NPDM"


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


def test_no_overlay_leaves_sysmodule_as_patcher_wrote_it(monkeypatch, tmp_path):
    """exefs_overlay=None → no sysmodule re-assert (degraded but explicit).

    The version-sentinel .ips are still installed regardless of the overlay —
    that's covered separately below; here we only assert the patcher's subsdk9
    is untouched and no sysmodule re-assert note is emitted.
    """
    mod_dir = tmp_path / "mods" / "contents" / "010093801237c000" / "DreadRandovania"
    (mod_dir / "exefs").mkdir(parents=True)
    romfs = tmp_path / "romfs"
    romfs.mkdir()
    (mod_dir / "exefs" / "subsdk9").write_bytes(b"UPSTREAM-SERVER-MODE")

    result, _ = _patch_with_fakes(monkeypatch, mod_dir, romfs, exefs_overlay=None)

    assert result.ok, result.message
    assert (mod_dir / "exefs" / "subsdk9").read_bytes() == b"UPSTREAM-SERVER-MODE"
    assert not any("re-asserted patched sysmodule" in n for n in result.notes)


def test_version_sentinel_ips_installed_into_exefs(monkeypatch, tmp_path):
    """patch() always re-asserts the build-id-keyed version-sentinel .ips.

    Guards the "Unsupported Metroid Dread version" regression: the vendored
    open-dread-rando submodule omits these (gitignored, pip-wheel-only) and
    rmtrees the exefs dir each run, so patch() must restore them from our
    bundled data/exefs_patches/ — including the 2.1.0 build id.
    """
    mod_dir = tmp_path / "mods" / "contents" / "010093801237c000" / "DreadRandovania"
    (mod_dir / "exefs").mkdir(parents=True)
    romfs = tmp_path / "romfs"
    romfs.mkdir()

    result, _ = _patch_with_fakes(monkeypatch, mod_dir, romfs, exefs_overlay=None)

    assert result.ok, result.message
    ips = sorted(p.name for p in (mod_dir / "exefs").glob("*.ips"))
    # The 2.1.0 build id the user's ROM reports — must be present.
    assert "646761F643AFEBB379EDD5E6A5151AF2CEF93DC1.ips" in ips
    # All bundled patches are non-empty and landed.
    assert ips, "no version-sentinel .ips were installed"
    for name in ips:
        assert (mod_dir / "exefs" / name).stat().st_size > 0
    assert any("version-sentinel" in n for n in result.notes)


# Build ids open-dread-rando supports (== the .ips filenames it ships).
EXPECTED_IPS = {
    "1.0.0": "49161D9CCBC15DF944D0B6278A3C446C006B0BE8.ips",
    "2.1.0": "646761F643AFEBB379EDD5E6A5151AF2CEF93DC1.ips",
}


def test_bundled_exefs_ips_present_for_both_versions():
    """The two prebuilt version-sentinel patches must ship in the apworld."""
    from dread._data_loader import data_resource

    bundled = {e.name for e in data_resource("exefs_patches").iterdir()
               if e.name.endswith(".ips")}
    for name in EXPECTED_IPS.values():
        assert name in bundled, f"missing bundled exefs patch {name}; have {bundled}"


def test_install_exefs_ips_copies_bytes(tmp_path):
    """_install_exefs_ips writes each bundled .ips into the target dir."""
    dest = tmp_path / "exefs"
    dest.mkdir()
    copied = pp._install_exefs_ips(dest)
    assert set(copied) >= set(EXPECTED_IPS.values())
    for name in copied:
        assert (dest / name).stat().st_size > 0
