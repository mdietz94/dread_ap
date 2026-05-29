"""Tests for the DataPackage loader."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client.datapackage import DataPackage, DEFAULT_AP_TO_PATCHER  # noqa: E402


def test_stub_loads_when_data_dir_missing(tmp_path):
    dp = DataPackage(apworld_data_dir=tmp_path / "nonexistent")
    di = dp.ap_name_to_dread("Missile Tank")
    assert di is not None
    assert di.patcher_item_id == "ITEM_WEAPON_MISSILE_MAX"
    assert di.quantity == 2


def test_stub_includes_all_default_keys():
    dp = DataPackage(apworld_data_dir=Path("nonexistent"))
    for name in DEFAULT_AP_TO_PATCHER:
        assert dp.ap_name_to_dread(name) is not None


def test_load_from_items_json(tmp_path):
    items = [
        {"name": "Missile Tank", "ap_id": 1001,
         "patcher_item_id": "ITEM_WEAPON_MISSILE_MAX", "quantity": 2},
        {"name": "Varia Suit", "ap_id": 1002,
         "patcher_item_id": "ITEM_VARIA_SUIT", "quantity": 1},
    ]
    (tmp_path / "items.json").write_text(json.dumps(items))
    dp = DataPackage(apworld_data_dir=tmp_path)
    assert dp.ap_id_to_name(1001) == "Missile Tank"
    assert dp.ap_id_to_dread(1001).quantity == 2
    assert dp.ap_id_to_dread(1002).patcher_item_id == "ITEM_VARIA_SUIT"


def test_load_from_locations_json(tmp_path):
    items = [{"name": "Missile Tank", "ap_id": 1001,
              "patcher_item_id": "ITEM_WEAPON_MISSILE_MAX"}]
    (tmp_path / "items.json").write_text(json.dumps(items))
    locations = [
        {"name": "Artaria Pickup 1", "ap_id": 2001,
         "scenario": "s010_cave", "actor": "Item_MissileTank011"},
        {"name": "Artaria Pickup 2", "ap_id": 2002,
         "scenario": "s010_cave", "actor": "Item_MissileTank012"},
    ]
    (tmp_path / "locations.json").write_text(json.dumps(locations))
    dp = DataPackage(apworld_data_dir=tmp_path)
    p = dp.location_id_to_pickup(2001)
    assert p is not None
    assert p.scenario == "s010_cave"
    assert p.actor == "Item_MissileTank011"
    assert sorted(dp.all_location_ids()) == [2001, 2002]


def test_load_unknown_ap_id_returns_none(tmp_path):
    dp = DataPackage(apworld_data_dir=tmp_path)
    assert dp.ap_id_to_dread(99999) is None
    assert dp.location_id_to_pickup(99999) is None
