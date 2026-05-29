"""Integrity tests on the extracted data tables (items.json, locations.json,
regions.json). These run without Archipelago — pure data validation."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


@pytest.fixture(scope="module")
def items():
    return json.loads((DATA / "items.json").read_text())


@pytest.fixture(scope="module")
def locations():
    return json.loads((DATA / "locations.json").read_text())


@pytest.fixture(scope="module")
def regions():
    return json.loads((DATA / "regions.json").read_text())


# ---- counts ----

def test_location_count_matches_table_sum(locations):
    """149 game pickups (137 actor + 12 boss/EMMI/cutscene) plus one
    synthetic event location per compiled event (post-M2). Pickup count
    is locked by the Randovania starter preset; event count tracks the
    compiler's output (~184 events at M2 ship)."""
    pickup = [l for l in locations if l["pickup_type"] != "event"]
    event = [l for l in locations if l["pickup_type"] == "event"]
    assert len(pickup) == 149, f"expected 149 game pickups, got {len(pickup)}"
    assert event, "expected at least one event location (M2)"
    assert len(locations) == len(pickup) + len(event)


def test_region_count_is_8(regions):
    """Itorash (s090_skybase) isn't represented in the starter preset
    template; we have the 8 scenarios that ARE: Artaria, Burenia,
    Cataris, Dairon, Elun, Ferenia, Ghavoran, Hanubia."""
    assert len(regions) == 8
    names = {r["name"] for r in regions}
    expected = {"Artaria", "Burenia", "Cataris", "Dairon",
                "Elun", "Ferenia", "Ghavoran", "Hanubia"}
    assert names == expected


def test_item_count_at_least_30(items):
    """30 items is the v0.1 baseline; v0.2 may add progressive variants
    bringing this higher."""
    assert len(items) >= 30


# ---- uniqueness ----

def test_item_ids_unique(items):
    ids = [it["ap_id"] for it in items]
    assert len(ids) == len(set(ids))


def test_location_ids_unique(locations):
    ids = [l["ap_id"] for l in locations]
    assert len(ids) == len(set(ids))


def test_item_names_unique(items):
    names = [it["name"] for it in items]
    assert len(names) == len(set(names))


def test_location_names_unique(locations):
    names = [l["name"] for l in locations]
    assert len(names) == len(set(names))


def test_item_and_location_id_ranges_disjoint(items, locations):
    """If an item ID collides with a location ID the AP server can't
    disambiguate. Our extractor seeds the two ranges separately."""
    item_ids = {it["ap_id"] for it in items}
    loc_ids = {l["ap_id"] for l in locations}
    assert not (item_ids & loc_ids)


# ---- structure ----

def test_every_item_has_patcher_id_and_quantity(items):
    """Game items must have a patcher_item_id that maps to a runtime
    ITEM_* identifier; event items (synthetic, M2) carry no game data
    so their patcher_item_id is intentionally empty."""
    for it in items:
        if it["name"].startswith("Event: "):
            assert it["patcher_item_id"] == "", f"event item must have empty patcher_item_id: {it}"
            assert it["classification"] == "progression", f"event item must be progression: {it}"
            continue
        assert it["patcher_item_id"], f"missing patcher_item_id: {it}"
        assert it["patcher_item_id"].startswith("ITEM_"), f"bad shape: {it}"
        assert isinstance(it["quantity"], int)
        assert it["quantity"] >= 1


def test_every_item_has_valid_classification(items):
    valid = {"progression", "progression_skip_balancing", "useful", "filler", "trap"}
    for it in items:
        assert it["classification"] in valid, f"bad classification: {it}"


def test_every_location_has_scenario_and_actor(locations):
    """Game pickup locations need scenario+actor for the patcher to
    place a real pickup; synthetic event locations don't (they're
    locked at generation time, never written to RomFS)."""
    pickup_types = {"actor", "emmi", "corex", "corpius", "cutscene"}
    for l in locations:
        if l["pickup_type"] == "event":
            assert l["actor"] == "", f"event location must have empty actor: {l}"
            continue
        assert l["scenario"], f"missing scenario: {l}"
        assert l["actor"], f"missing actor: {l}"
        assert l["pickup_type"] in pickup_types, f"bad type: {l}"


def test_every_location_region_is_in_regions_table(locations, regions):
    region_names = {r["name"] for r in regions}
    for l in locations:
        assert l["region"] in region_names, f"orphan region: {l}"


def test_vanilla_items_resolve_to_known_item(locations, items):
    item_names = {it["name"] for it in items}
    for l in locations:
        assert l["vanilla_item"] in item_names, \
            f"vanilla_item {l['vanilla_item']!r} not in items.json"


# ---- distribution sanity ----

def test_artaria_has_most_pickups(locations):
    """Artaria is the starting area and has the most pickups in vanilla."""
    counts: dict[str, int] = {}
    for l in locations:
        counts[l["region"]] = counts.get(l["region"], 0) + 1
    assert counts["Artaria"] == max(counts.values())


def test_progression_items_include_core_traversal(items):
    names = {it["name"] for it in items if it["classification"] == "progression"}
    must_have = {"Morph Ball", "Varia Suit", "Charge Beam", "Grapple Beam",
                 "Space Jump", "Speed Booster"}
    missing = must_have - names
    assert not missing, f"missing core progression items: {missing}"
