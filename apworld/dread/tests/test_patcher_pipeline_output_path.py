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
    # These tests use empty romfs dirs and exercise output-path logic, not the
    # romfs-version pre-flight (covered separately) — bypass it like the dep check.
    monkeypatch.setattr(pp, "verify_romfs_version", lambda d: None)
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


def test_first_deploy_creates_missing_install_dir(monkeypatch, tmp_path):
    """First-ever deploy: the per-title install dir doesn't exist yet (the
    SD-mount guard in _maybe_auto_patch admits a freshly-mounted card that has
    only atmosphere/). patch() must CREATE the install dir and proceed, not fail
    with 'install dir not found', so a first deploy works identically to later
    ones."""
    mod_dir = tmp_path / "sd" / "atmosphere" / "contents" / "010093801237c000"
    assert not mod_dir.exists()  # the case this guards: title-id dir missing
    romfs = tmp_path / "romfs"
    romfs.mkdir()

    result, _ = _patch_with_fakes(
        monkeypatch, mod_dir, romfs, mod_compatibility="atmosphere")

    assert result.ok, result.message
    assert mod_dir.is_dir(), "patch() must create the install dir on first deploy"


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


def test_ap_config_reasserted_into_romfs(monkeypatch, tmp_path):
    """The upstream patcher writes a FRESH romfs that doesn't carry our
    rom:/ap_config.json — the bridge sysmodule's SOLE /24 discovery-sweep
    seed. patch() must re-assert it (with the given bridge_host) or the Switch
    only sweeps loopback and never finds the PC on real hardware. Regression
    for 'connects on Ryujinx, not on hardware after an auto-patch'."""
    import json

    mod_dir = tmp_path / "sd" / "atmosphere" / "contents" / "010093801237c000"
    mod_dir.mkdir(parents=True)
    romfs = tmp_path / "romfs"
    romfs.mkdir()

    captured: dict = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(pp, "check_dependencies", lambda py=None: None)
    monkeypatch.setattr(pp, "verify_romfs_version", lambda d: None)
    monkeypatch.setattr(pp.subprocess, "run", _fake_run)

    result = pp.patch(
        placements=_minimal_placements(),
        dreadvania_install_dir=mod_dir,
        vanilla_romfs_dir=romfs,
        python_executable="py",
        mod_compatibility="atmosphere",
        bridge_host="192.168.1.153",
    )

    assert result.ok, result.message
    ap_config = mod_dir / "romfs" / "ap_config.json"
    assert ap_config.is_file(), "patch() must re-assert rom:/ap_config.json"
    assert json.loads(ap_config.read_text()) == {"bridge_host": "192.168.1.153"}


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


# --- romfs version pre-flight -----------------------------------------------

import hashlib  # noqa: E402


def _write_toc(romfs: Path, content: bytes) -> str:
    """Write system/files.toc with the given bytes; return its md5 hexdigest."""
    (romfs / "system").mkdir(parents=True, exist_ok=True)
    (romfs / "system" / "files.toc").write_bytes(content)
    return hashlib.md5(content).hexdigest()


def test_verify_romfs_version_missing_toc(tmp_path):
    """A folder without system/files.toc is reported as 'not a romfs', naming
    the missing file — the 'wrong folder' case."""
    romfs = tmp_path / "romfs"
    romfs.mkdir()
    msg = pp.verify_romfs_version(romfs)
    assert msg is not None
    assert "system/files.toc" in msg


def test_verify_romfs_version_unknown_hash(tmp_path):
    """A present-but-unrecognized TOC (patched/incomplete dump) is reported
    with the actionable 'not a clean retail dump' guidance + the digest."""
    romfs = tmp_path / "romfs"
    romfs.mkdir()
    digest = _write_toc(romfs, b"this is not a real dread toc")
    msg = pp.verify_romfs_version(romfs)
    assert msg is not None
    assert digest in msg
    assert "recognized vanilla Dread version" in msg
    # Names every supported version so the user knows what's expected.
    for ver in ("1.0.0", "1.0.1", "2.0.0", "2.1.0"):
        assert ver in msg


def test_verify_romfs_version_known_hash_passes(tmp_path, monkeypatch):
    """A TOC whose md5 is in the known table verifies (None). We can't ship a
    real Dread TOC, so register a synthetic file's hash as 'supported'."""
    romfs = tmp_path / "romfs"
    romfs.mkdir()
    digest = _write_toc(romfs, b"pretend-this-is-2.1.0")
    monkeypatch.setitem(pp.KNOWN_DREAD_TOC_HASHES, digest, "2.1.0")
    assert pp.verify_romfs_version(romfs) is None


def test_patch_rejects_unknown_romfs_version(monkeypatch, tmp_path):
    """patch() fails fast on an unrecognized romfs — no subprocess spawned —
    with the actionable message (this is the reported 'Not a valid version!'
    crash, now caught before the raw traceback)."""
    mod_dir = tmp_path / "mods" / "contents" / "010093801237c000" / "DreadRandovania"
    mod_dir.mkdir(parents=True)
    romfs = tmp_path / "romfs"
    romfs.mkdir()
    _write_toc(romfs, b"unrecognized")

    spawned = {"ran": False}

    def _fake_run(cmd, **kwargs):
        spawned["ran"] = True
        return _FakeProc()

    monkeypatch.setattr(pp, "check_dependencies", lambda py=None: None)
    monkeypatch.setattr(pp.subprocess, "run", _fake_run)

    result = pp.patch(
        placements=_minimal_placements(),
        dreadvania_install_dir=mod_dir,
        vanilla_romfs_dir=romfs,
        python_executable="py",
    )
    assert not result.ok
    assert "recognized vanilla Dread version" in result.message
    assert not spawned["ran"], "patch() must not spawn the patcher on a bad romfs"


def test_patcher_error_hint_translates_not_a_valid_version():
    """A raw MEDS 'Not a valid version!' on the subprocess stderr is translated
    into an actionable hint (covers the table-drift case where our pre-flight
    passed but the installed MEDS rejected the romfs)."""
    hint = pp._patcher_error_hint(
        'ValueError: Not a valid version!\n  at version_validation.py:24')
    assert hint is not None
    assert "supported" in hint.lower()
    assert "pip install" in hint


def test_patcher_error_hint_none_for_unrelated_error():
    assert pp._patcher_error_hint("Traceback: some other failure") is None


# --- frozen-launcher interpreter guard --------------------------------------


def test_patch_refuses_frozen_launcher_by_sys_frozen(monkeypatch, tmp_path):
    """When the client is a frozen AppImage/PyInstaller bundle (sys.frozen) and
    no python_executable was resolved, sys.executable is the Archipelago
    launcher — NOT a Python. patch() must refuse with an actionable message and
    NEVER spawn `<launcher> -m open_dread_rando ...` (which the launcher's own
    argparse rejects with 'unrecognized arguments: -m' → exit 2)."""
    mod_dir = tmp_path / "mods" / "contents" / "010093801237c000" / "DreadRandovania"
    mod_dir.mkdir(parents=True)
    romfs = tmp_path / "romfs"
    romfs.mkdir()

    spawned = {"ran": False}

    def _fake_run(cmd, **kwargs):
        spawned["ran"] = True
        return _FakeProc()

    monkeypatch.setattr(pp, "verify_romfs_version", lambda d: None)
    monkeypatch.setattr(pp.subprocess, "run", _fake_run)
    monkeypatch.setattr(pp.sys, "frozen", True, raising=False)

    result = pp.patch(
        placements=_minimal_placements(),
        dreadvania_install_dir=mod_dir,
        vanilla_romfs_dir=romfs,
        python_executable=None,  # forces the sys.executable fallback
    )

    assert not result.ok
    assert "frozen Archipelago launcher" in result.message
    assert "pip install" in result.message
    assert not spawned["ran"], "patch() must not exec the frozen launcher"


def test_patch_refuses_launcher_by_name(monkeypatch, tmp_path):
    """Name-based backstop: some frozen builds don't set sys.frozen, but
    sys.executable still basenames to the launcher. With deps importable
    in-process (check_dependencies passes) the frozen branch wouldn't fire, so
    the describe_python name check must still refuse — never exec the launcher."""
    mod_dir = tmp_path / "mods" / "contents" / "010093801237c000" / "DreadRandovania"
    mod_dir.mkdir(parents=True)
    romfs = tmp_path / "romfs"
    romfs.mkdir()

    spawned = {"ran": False}

    def _fake_run(cmd, **kwargs):
        spawned["ran"] = True
        return _FakeProc()

    monkeypatch.setattr(pp, "verify_romfs_version", lambda d: None)
    monkeypatch.setattr(pp, "check_dependencies", lambda py=None: None)
    monkeypatch.setattr(pp.subprocess, "run", _fake_run)
    # sys.executable basenames to the launcher, but this build didn't set frozen.
    monkeypatch.setattr(pp.sys, "frozen", False, raising=False)
    monkeypatch.setattr(pp.sys, "executable", "/opt/Archipelago/ArchipelagoLauncher")

    result = pp.patch(
        placements=_minimal_placements(),
        dreadvania_install_dir=mod_dir,
        vanilla_romfs_dir=romfs,
        python_executable=None,  # falls back to sys.executable (the launcher)
    )

    assert not result.ok
    assert "frozen Archipelago launcher" in result.message
    assert not spawned["ran"], "patch() must not exec the frozen launcher"
