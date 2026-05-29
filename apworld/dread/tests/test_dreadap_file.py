"""Tests for the `.dreadap` launcher-file schema (client/dreadap_file.py)."""
from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client.dreadap_file import (  # noqa: E402
    DREADAP_METADATA_ENTRY,
    DreadapFile,
    dreadap_to_launch_args,
    parse_dreadap,
)


def test_write_then_parse_roundtrip(tmp_path):
    path = tmp_path / "Samus.dreadap"
    DreadapFile(slot_name="Samus", seed_name="SEED123").write(path)
    parsed = parse_dreadap(path)
    assert parsed.slot_name == "Samus"
    assert parsed.seed_name == "SEED123"
    assert parsed.game == "Metroid Dread"


def test_written_file_is_zip_with_metadata_entry(tmp_path):
    path = tmp_path / "Samus.dreadap"
    DreadapFile(slot_name="Samus").write(path)
    with zipfile.ZipFile(path) as zf:
        assert zf.namelist() == [DREADAP_METADATA_ENTRY]
        meta = json.loads(zf.read(DREADAP_METADATA_ENTRY))
    assert meta["slot_name"] == "Samus"
    assert meta["game"] == "Metroid Dread"
    assert meta["version"] == 1


def test_parse_accepts_bare_json_text():
    text = json.dumps({"game": "Metroid Dread", "version": 1, "slot_name": "Joey"})
    parsed = parse_dreadap(text)
    assert parsed.slot_name == "Joey"


def test_parse_accepts_bare_json_file(tmp_path):
    path = tmp_path / "legacy.dreadap"
    path.write_text(json.dumps(
        {"game": "Metroid Dread", "version": 1, "slot_name": "Legacy"}))
    assert parse_dreadap(path).slot_name == "Legacy"


def test_parse_rejects_wrong_game():
    with pytest.raises(ValueError):
        parse_dreadap(json.dumps(
            {"game": "Some Other Game", "version": 1, "slot_name": "X"}))


def test_parse_rejects_missing_slot_name():
    with pytest.raises(ValueError):
        parse_dreadap(json.dumps({"game": "Metroid Dread", "version": 1}))


def test_parse_rejects_future_version():
    with pytest.raises(ValueError):
        parse_dreadap(json.dumps(
            {"game": "Metroid Dread", "version": 999, "slot_name": "X"}))


def test_parse_ignores_unknown_keys():
    parsed = parse_dreadap(json.dumps({
        "game": "Metroid Dread", "version": 1, "slot_name": "X",
        "future_field": "ignored",
    }))
    assert parsed.slot_name == "X"


def test_launch_args_name_only():
    args = dreadap_to_launch_args(DreadapFile(slot_name="Samus"))
    assert args == ["--name", "Samus"]


def test_launch_args_with_server_and_password():
    args = dreadap_to_launch_args(DreadapFile(
        slot_name="Samus", server_address="ap.gg:38281", password="pw"))
    assert args == ["--name", "Samus", "--connect", "ap.gg:38281",
                    "--password", "pw"]
