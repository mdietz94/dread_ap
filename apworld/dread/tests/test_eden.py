"""Eden is opt-in: synthetic metadata and legacy-target isolation regressions."""
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from dread._setup import deploy
from dread._setup.eden import compatible_npdm
from dread import patcher_pipeline as pp


@pytest.fixture
def metadata():
    # Constructed fixture; no retail NPDM or game data is used.
    data = bytearray(0x400)
    data[:4] = b"META"
    for field, start, length, magic_offset, magic, cap_field, cap_offset in (
        (0x70, 0x80, 0x50, 0, b"ACI0", 0x30, 0x38),
        (0x78, 0x100, 0x250, 0x200, b"ACID", 0x230, 0x238),
    ):
        struct.pack_into("<II", data, field, start, length)
        data[start+magic_offset:start+magic_offset+4] = magic
        struct.pack_into("<II", data, start+cap_field, cap_offset, 8)
        struct.pack_into("<II", data, start+cap_offset, 0xffffffff, 0x0008ffff)
    return bytes(data)


def test_only_reserved_bits_change_and_conversion_is_idempotent(metadata):
    changed = compatible_npdm(metadata)
    expected = bytearray(metadata)
    for pos in (0xbe, 0x33e):
        expected[pos] = 0
    assert changed == bytes(expected)
    assert compatible_npdm(changed) == changed
    assert metadata[0xbe] == metadata[0x33e] == 8


@pytest.mark.parametrize("offset,value", [
    (0, b"NOPE"),
    (0x70, struct.pack("<I", 0x1000)),
    (0xb0, struct.pack("<I", 0x1000)),
    (0xb4, struct.pack("<I", 7)),
    (0xbc, struct.pack("<I", 0x1008ffff)),
    (0xbc, struct.pack("<I", 0xffffffff)),
    (0xb8, struct.pack("<I", 0x0008ffff)),
])
def test_unrecognized_metadata_rejected(metadata, offset, value):
    bad = bytearray(metadata)
    bad[offset:offset+len(value)] = value
    with pytest.raises(ValueError):
        compatible_npdm(bytes(bad))


def test_truncated_metadata_rejected(metadata):
    for size in (0, 0x78, 0x340):
        with pytest.raises(ValueError):
            compatible_npdm(metadata[:size])


@pytest.fixture
def outputs(tmp_path, metadata):
    src = tmp_path / "shared-prebuilt"
    src.mkdir()
    files = {"subsdk9": src / "subsdk9", "main.npdm": src / "main.npdm"}
    files["subsdk9"].write_bytes(b"unchanged module")
    files["main.npdm"].write_bytes(metadata)
    return files


def snapshot(path):
    return {p.relative_to(path): p.read_bytes() for p in path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("legacy", [deploy.deploy_to_sd, deploy.deploy_to_ryujinx,
                                    deploy.deploy_to_custom_folder])
def test_eden_then_legacy_deployment_has_identical_output(tmp_path, outputs, metadata, legacy):
    host = "192.168.7.42"
    old_root = tmp_path / "legacy"
    assert legacy(old_root, outputs, bridge_host=host).ok
    before = snapshot(old_root)
    eden_root = tmp_path / "portable-eden-data"
    for _ in range(2):
        result = deploy.deploy_to_eden(eden_root, outputs, bridge_host=host)
        assert result.ok, result.error
    mod = eden_root / "load" / deploy.DREAD_TITLE_ID / "DreadRandovania"
    assert (mod / "exefs/main.npdm").read_bytes() == compatible_npdm(metadata)
    assert (mod / "exefs/subsdk9").read_bytes() == b"unchanged module"
    assert (mod / "romfs/ap_config.json").read_text() == '{"bridge_host":"192.168.7.42"}'
    assert outputs["main.npdm"].read_bytes() == metadata
    assert legacy(old_root, outputs, bridge_host=host).ok
    assert snapshot(old_root) == before


def test_invalid_deploy_preserves_existing_destination(tmp_path, outputs):
    root = tmp_path / "eden"
    assert deploy.deploy_to_eden(root, outputs, bridge_host="127.0.0.1").ok
    before = snapshot(root)
    outputs["main.npdm"].write_bytes(b"unsupported metadata")
    assert not deploy.deploy_to_eden(root, outputs, bridge_host="127.0.0.1").ok
    assert snapshot(root) == before


@pytest.mark.parametrize("compatibility,eden", [("ryujinx", False), ("atmosphere", False),
                                               ("ryujinx", True)])
def test_pipeline_reasserts_metadata_after_patcher(monkeypatch, tmp_path, outputs, metadata,
                                                  compatibility, eden):
    mod = (tmp_path / "load/title/DreadRandovania" if compatibility == "ryujinx"
           else tmp_path / "atmosphere/contents/010093801237c000")
    romfs = tmp_path / "romfs"
    romfs.mkdir()
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        exefs = mod / "exefs"
        exefs.mkdir(parents=True, exist_ok=True)
        (exefs / "main.npdm").write_bytes(b"patcher replacement")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pp, "check_dependencies", lambda _: None)
    monkeypatch.setattr(pp, "verify_romfs_version", lambda _: None)
    monkeypatch.setattr(pp.subprocess, "run", run)
    kwargs = dict(placements={"slot_name": "Tester", "seed_id": "test", "placements": []},
                  dreadvania_install_dir=mod, vanilla_romfs_dir=romfs,
                  exefs_overlay=outputs, mod_compatibility=compatibility,
                  bridge_host="127.0.0.1")
    result = pp.patch(**kwargs, eden_compatibility=eden)
    assert result.ok, result.message
    expected = compatible_npdm(metadata) if eden else metadata
    assert (mod / "exefs/main.npdm").read_bytes() == expected
    assert outputs["main.npdm"].read_bytes() == metadata
    # Repatch a previously Eden-adapted destination through a legacy target.
    assert pp.patch(**kwargs).ok
    assert (mod / "exefs/main.npdm").read_bytes() == metadata
    assert len(calls) == 2


def test_invalid_pipeline_metadata_fails_before_patching(monkeypatch, tmp_path, outputs):
    outputs["main.npdm"].write_bytes(b"bad metadata")
    def unexpected(*args, **kwargs):
        pytest.fail("invalid Eden metadata must fail before invoking the patcher")
    monkeypatch.setattr(pp, "check_dependencies", unexpected)
    result = pp.patch(placements={}, dreadvania_install_dir=tmp_path / "out",
                      vanilla_romfs_dir=tmp_path / "romfs", exefs_overlay=outputs,
                      mod_compatibility="ryujinx", eden_compatibility=True)
    assert not result.ok
    assert not (tmp_path / "out").exists()
