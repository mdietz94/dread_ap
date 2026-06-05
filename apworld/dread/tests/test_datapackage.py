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


def test_progressive_item_builds_full_progression_and_first_tier_class(tmp_path):
    """A progressive entry resolves to the FULL multi-stage progression (one
    stage per tier) with the first tier's specific Lua class — the same
    (cls, progression) the game's local progressive pickup uses, so delivery
    auto-advances to the next missing tier."""
    items = [
        {"name": "Wide Beam", "ap_id": 1,
         "patcher_item_id": "ITEM_WEAPON_WIDE_BEAM", "quantity": 1},
        {"name": "Plasma Beam", "ap_id": 2,
         "patcher_item_id": "ITEM_WEAPON_PLASMA_BEAM", "quantity": 1},
        {"name": "Wave Beam", "ap_id": 3,
         "patcher_item_id": "ITEM_WEAPON_WAVE_BEAM", "quantity": 1},
        {"name": "Progressive Beam", "ap_id": 10, "patcher_item_id": "",
         "quantity": 1,
         "progression_tiers": ["Wide Beam", "Plasma Beam", "Wave Beam"]},
    ]
    (tmp_path / "items.json").write_text(json.dumps(items))
    dp = DataPackage(apworld_data_dir=tmp_path)
    di = dp.ap_id_to_dread(10)
    assert di is not None
    assert di.progression_stages == [
        [{"item_id": "ITEM_WEAPON_WIDE_BEAM", "quantity": 1}],
        [{"item_id": "ITEM_WEAPON_PLASMA_BEAM", "quantity": 1}],
        [{"item_id": "ITEM_WEAPON_WAVE_BEAM", "quantity": 1}],
    ]
    # First tier (Wide Beam) → RandomizerWideBeam (its pick_up_beam switches to
    # the progressive branch when it sees a multi-stage progression).
    assert di.pickup_cls == "RandomizerWideBeam"


def test_progressive_suit_uses_generic_powerup_class(tmp_path):
    """A progressive whose first tier isn't in SPECIFIC_CLASSES (Varia Suit)
    falls back to RandomizerPowerup, which grants the next missing stage."""
    items = [
        {"name": "Varia Suit", "ap_id": 1,
         "patcher_item_id": "ITEM_VARIA_SUIT", "quantity": 1},
        {"name": "Gravity Suit", "ap_id": 2,
         "patcher_item_id": "ITEM_GRAVITY_SUIT", "quantity": 1},
        {"name": "Progressive Suit", "ap_id": 10, "patcher_item_id": "",
         "quantity": 1, "progression_tiers": ["Varia Suit", "Gravity Suit"]},
    ]
    (tmp_path / "items.json").write_text(json.dumps(items))
    dp = DataPackage(apworld_data_dir=tmp_path)
    di = dp.ap_id_to_dread(10)
    assert di.pickup_cls == "RandomizerPowerup"
    assert [s[0]["item_id"] for s in di.progression_stages] == [
        "ITEM_VARIA_SUIT", "ITEM_GRAVITY_SUIT"]


def test_real_items_json_progressives_load():
    """The shipped items.json progressives resolve end-to-end via the default
    (importlib.resources) loader."""
    dp = DataPackage()
    di = dp.ap_name_to_dread("Progressive Beam")
    assert di is not None and di.progression_stages is not None
    assert di.pickup_cls == "RandomizerWideBeam"
    assert len(di.progression_stages) == 3
