"""Unit tests for the bootstrap generator (client/bootstrap.py).

Pure functions over the data tables — no network. Validates that we reproduce
randovania's get_bootstrapper_for faithfully from our own items/locations data:
all TEMPLATE holes filled, pickup keys map to the right bitfield index, and the
chunker matches the executor's packing.

Run with:  python -m pytest apworld/dread/tests/test_bootstrap.py -v
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client import bootstrap as bs  # noqa: E402

DATA = ROOT / "data"


@pytest.fixture
def real_rows():
    items = json.loads((DATA / "items.json").read_text(encoding="utf-8"))
    locations = json.loads((DATA / "locations.json").read_text(encoding="utf-8"))
    return items, locations


def test_build_from_real_data_has_no_unfilled_templates(real_rows):
    items, locations = real_rows
    blocks = bs.build_bootstrap_code(items, locations)
    joined = "\n".join(blocks)
    assert 'TEMPLATE("' not in joined
    assert not re.search(r"T__\w+__T", joined)


def test_last_block_flips_bootstrap_flag(real_rows):
    items, locations = real_rows
    blocks = bs.build_bootstrap_code(items, locations)
    assert blocks[-1] == "RL.Bootstrap=true"


def test_num_pickup_nodes_matches_data(real_rows):
    items, locations = real_rows
    pickups = [loc for loc in locations if loc.get("pickup_index") is not None]
    expected = max(int(loc["pickup_index"]) for loc in pickups) + 1
    blocks = bs.build_bootstrap_code(items, locations)
    # part_0 contains `for i=1,<n> do RL.Pickups[i]='' end`
    assert f"for i=1,{expected} do" in blocks[0]
    assert expected == 149  # 137 actor + 12 boss/EMMI/cutscene pickups


def test_known_pickup_keyed_to_index_plus_one(real_rows):
    items, locations = real_rows
    blocks = bs.build_bootstrap_code(items, locations)
    joined = "\n".join(blocks)
    # Artaria ChargeBeam is pickup_index 0 (actor ItemSphere_ChargeBeam) in
    # scenario s010_cave → RL.Pickups[1], keyed off "s010_cave_".
    assert "ItemSphere_ChargeBeam=1" in joined
    assert "'s010_cave_'" in joined
    # A boss/callback pickup keyed the same way (Corpius, index 138 → 139).
    assert "OnCorpiusDeath_CUSTOM=139" in joined


def test_one_locations_block_per_scenario(real_rows):
    items, locations = real_rows
    scenarios = {loc["scenario"] for loc in locations if loc.get("pickup_index") is not None}
    blocks = bs.build_bootstrap_code(items, locations)
    loc_blocks = [b for b in blocks if "RL.Pickups[i]=RandomizerPowerup.PropertyForLocation" in b]
    assert len(loc_blocks) == len(scenarios)


def test_inventory_list_is_lua_table_of_item_ids(real_rows):
    items, locations = real_rows
    blocks = bs.build_bootstrap_code(items, locations)
    # part_1 ends with `RL.InventoryItems={...}`
    assert "RL.InventoryItems={" in blocks[1]
    assert "'ITEM_WEAPON_MISSILE_MAX'" in blocks[1]


def test_rejects_non_identifier_pickup_key():
    items = [{"patcher_item_id": "ITEM_X"}]
    bad = [{"scenario": "s010_cave", "actor": "has-a-hyphen", "pickup_index": 0}]
    with pytest.raises(ValueError, match="not a valid Lua identifier"):
        bs.build_bootstrap_code(items, bad)


def test_rejects_duplicate_pickup_index():
    items = [{"patcher_item_id": "ITEM_X"}]
    dupe = [
        {"scenario": "s010_cave", "actor": "A", "pickup_index": 0},
        {"scenario": "s010_cave", "actor": "B", "pickup_index": 0},
    ]
    with pytest.raises(ValueError, match="duplicate pickup_index"):
        bs.build_bootstrap_code(items, dupe)


# ---- chunker --------------------------------------------------------------

def test_chunk_packs_and_preserves_order():
    blocks = ["aaaa", "bbbb", "cccc", "dddd"]
    chunks = bs.chunk_lua_blocks(blocks, buffer_size=10)
    # Each chunk <= buffer_size, and joining chunks (by ;) recovers all blocks.
    for c in chunks:
        assert len(c) <= 10
    assert ";".join(chunks).split(";") == blocks


def test_chunk_single_block_within_buffer():
    chunks = bs.chunk_lua_blocks(["short"], buffer_size=4096)
    assert chunks == ["short"]


def test_chunk_rejects_oversized_single_block():
    with pytest.raises(ValueError, match="single bootstrap block"):
        bs.chunk_lua_blocks(["x" * 100], buffer_size=10)


def test_real_bootstrap_chunks_fit_buffer(real_rows):
    items, locations = real_rows
    blocks = bs.build_bootstrap_code(items, locations)
    chunks = bs.chunk_lua_blocks(blocks, buffer_size=4096)
    assert chunks  # non-empty
    for c in chunks:
        assert len(c) <= 4096
