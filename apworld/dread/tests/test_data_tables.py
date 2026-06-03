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
    """149 game pickups: 137 actor + 12 boss/EMMI/cutscene/corex. Events are
    inlined into the per-pickup rules and are NOT AP locations (Option A); a
    leftover event-typed location is a regression."""
    pickup = [l for l in locations if l["pickup_type"] != "event"]
    leaked_events = [l for l in locations if l["pickup_type"] == "event"]
    assert len(pickup) == 149, f"expected 149 game pickups, got {len(pickup)}"
    assert not leaked_events, \
        f"event locations leaked into the table: {[l['name'] for l in leaked_events[:5]]}"
    assert len(locations) == len(pickup)


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
    ITEM_* identifier. DNA items map to ITEM_RANDO_ARTIFACT_k."""
    for it in items:
        assert it["patcher_item_id"], f"missing patcher_item_id: {it}"
        assert it["patcher_item_id"].startswith("ITEM_"), f"bad shape: {it}"
        assert isinstance(it["quantity"], int)
        assert it["quantity"] >= 1


def test_every_item_has_pool_count(items):
    """pool_count drives the default number of copies World.create_items puts
    in the pool (option-overridable for tank types). DNA items are added
    explicitly by RequiredArtifacts, so pool_count=0 is correct for them.
    Every other item must have pool_count >= 1 so the loop adds at least
    one copy."""
    for it in items:
        assert "pool_count" in it, f"missing pool_count: {it}"
        assert isinstance(it["pool_count"], int)
        assert it["pool_count"] >= 0, f"negative pool_count: {it}"
        if it["name"].startswith("Metroid DNA"):
            continue
        assert it["pool_count"] >= 1, f"non-DNA item with pool_count=0: {it}"


def test_every_item_has_valid_classification(items):
    valid = {"progression", "progression_skip_balancing", "useful", "filler", "trap"}
    for it in items:
        assert it["classification"] in valid, f"bad classification: {it}"


def test_every_location_has_scenario_and_actor(locations):
    """Every AP location is a real game pickup the patcher writes to RomFS,
    so it needs scenario+actor. Events were inlined into rules and are no
    longer AP locations."""
    pickup_types = {"actor", "emmi", "corex", "corpius", "cutscene"}
    for l in locations:
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


# Resource ids the game grants that are NOT collectible AP items (the patcher's
# "empty pickup" sentinel). Mirrors extract_dread_data._NON_ITEM_RESOURCES.
_NON_ITEM_RESOURCES = {"ITEM_NONE"}


def test_every_preset_granted_resource_has_a_pool_item(items):
    """Every resource the canonical layout actually grants must be
    representable by some item in items.json (matched on patcher_item_id).

    This is the regression net for the missing-Super-Missile bug: Super
    Missile is granted in the starter preset (as the first stage of a
    ``[[Super],[Ice]]`` progressive pickup) but had no pool entry, so it could
    never be collected. ``test_vanilla_items_resolve_to_known_item`` did NOT
    catch it, because the extractor silently relabeled the orphaned pickup as a
    (valid) Missile Tank. Checking against the GRANTED resources — not the
    post-fallback labels — is what makes the gap visible. Spans all resource
    groups so progressive second/third stages are covered too."""
    template = json.loads((DATA / "starter_preset_patcher.json").read_text())
    pool_pids = {it["patcher_item_id"] for it in items}
    missing: dict[str, str] = {}
    for pickup in template.get("pickups", []):
        for group in pickup.get("resources", []) or []:
            for resource in group:
                pid = resource.get("item_id")
                if not pid or pid in _NON_ITEM_RESOURCES:
                    continue
                if pid not in pool_pids:
                    actor = (pickup.get("pickup_actor") or {}).get(
                        "actor", pickup.get("pickup_type", "?"))
                    missing.setdefault(pid, actor)
    assert not missing, (
        "starter preset grants resources with no items.json entry: "
        f"{ {pid: f'e.g. {actor}' for pid, actor in sorted(missing.items())} }. "
        "Add each to ITEM_TABLE + items.json (or _NON_ITEM_RESOURCES if it is "
        "a non-collectible sentinel)."
    )


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
