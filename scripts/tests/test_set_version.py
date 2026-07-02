"""Tests for scripts/set_version.py — the single writer of the apworld's
release version (world_version + __version__)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import set_version  # noqa: E402


MANIFEST_TEMPLATE = (
    '{\n'
    '    "compatible_version": 7,\n'
    '    "version": 7,\n'
    '    "game": "Metroid Dread",\n'
    '    "world_version": "0.6.0",\n'
    '    "minimum_ap_version": "0.5.0",\n'
    '    "authors": ["maxdietz", "Randovania Dread contributors"]\n'
    '}\n'
)
INIT_TEMPLATE = (
    '"""docstring"""\n'
    'from __future__ import annotations\n'
    '\n'
    '\n'
    '__version__ = "0.7.2"\n'
)


@pytest.fixture()
def files(tmp_path: Path) -> tuple[Path, Path]:
    manifest = tmp_path / "archipelago.json"
    init = tmp_path / "__init__.py"
    manifest.write_text(MANIFEST_TEMPLATE, encoding="utf-8")
    init.write_text(INIT_TEMPLATE, encoding="utf-8")
    return manifest, init


def test_set_version_stamps_both_files(files):
    manifest, init = files
    ret = set_version.set_version("0.19.0", manifest=manifest, init=init)
    assert ret == "0.19.0"
    assert set_version.read_versions(manifest, init) == ("0.19.0", "0.19.0")
    assert '"world_version": "0.19.0"' in manifest.read_text(encoding="utf-8")
    assert '__version__ = "0.19.0"' in init.read_text(encoding="utf-8")


def test_leading_v_is_stripped(files):
    manifest, init = files
    assert set_version.set_version("v1.2.3", manifest=manifest, init=init) == "1.2.3"
    assert set_version.read_versions(manifest, init) == ("1.2.3", "1.2.3")


def test_prerelease_suffix_allowed(files):
    manifest, init = files
    set_version.set_version("0.20.0-rc1", manifest=manifest, init=init)
    assert set_version.read_versions(manifest, init) == ("0.20.0-rc1", "0.20.0-rc1")


@pytest.mark.parametrize("bad", ["1.2", "abc", "1.2.3.4-", "", "v"])
def test_invalid_version_rejected(files, bad):
    manifest, init = files
    with pytest.raises(ValueError):
        set_version.set_version(bad, manifest=manifest, init=init)
    # Files are only written after validation, so they stay untouched.
    assert set_version.read_versions(manifest, init) == ("0.6.0", "0.7.2")


def test_formatting_preserved_minimal_diff(files):
    manifest, init = files
    before = manifest.read_text(encoding="utf-8")
    set_version.set_version("0.19.0", manifest=manifest, init=init)
    after = manifest.read_text(encoding="utf-8")
    # Only the world_version line changed; every other line is byte-identical.
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines())
               if a != b]
    assert changed == [
        ('    "world_version": "0.6.0",', '    "world_version": "0.19.0",'),
    ]


def test_missing_field_raises(tmp_path):
    manifest = tmp_path / "archipelago.json"
    init = tmp_path / "__init__.py"
    manifest.write_text('{"game": "Metroid Dread"}\n', encoding="utf-8")
    init.write_text(INIT_TEMPLATE, encoding="utf-8")
    with pytest.raises(ValueError):
        set_version.set_version("0.19.0", manifest=manifest, init=init)


def test_real_repo_files_are_consistent():
    """The actual shipped files must always agree — this is the invariant the
    install-time drift guard enforces."""
    world_version, dunder = set_version.read_versions()
    assert world_version is not None and dunder is not None
    assert world_version == dunder
