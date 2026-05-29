"""Pickup-index ↔ AP-location-id map invariants.

The Switch's PACKET_COLLECTED_INDICES push carries a bitfield indexed by
``pickup_index`` (the position in the patcher template's `pickups`
array). The client needs to translate set bits back into AP location_ids.

These tests assert that the `pickup_index` field on each non-event entry
in locations.json:

  1. Exists for every actor / EMMI / corex / corpius / cutscene location.
  2. Is unique across all non-event locations.
  3. Forms a contiguous 0..148 range matching the 149 pickups in the
     patcher template.
  4. Matches the actual position in the template (spot-checked).
  5. Is NOT present on synthetic event locations.

Run with:  python -m pytest apworld/dread_archipelago/tests/test_pickup_index_map.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

DATA = ROOT / "data"
TEMPLATE = (
    ROOT.parent.parent
    / "vendor"
    / "open-dread-rando"
    / "tests"
    / "test_files"
    / "patcher_files"
    / "starter_preset_patcher.json"
)


@pytest.fixture(scope="module")
def locations():
    return json.loads((DATA / "locations.json").read_text())


@pytest.fixture(scope="module")
def template():
    return json.loads(TEMPLATE.read_text())


NON_EVENT_TYPES = {"actor", "emmi", "corex", "corpius", "cutscene"}


def test_every_non_event_location_has_pickup_index(locations):
    for l in locations:
        if l["pickup_type"] in NON_EVENT_TYPES:
            assert "pickup_index" in l, f"non-event location missing pickup_index: {l['name']}"
            assert isinstance(l["pickup_index"], int)


def test_event_locations_have_no_pickup_index(locations):
    """Events are AP-synthetic and never collected via the COLLECTED_INDICES
    push, so they MUST NOT claim a pickup_index — that would shadow a real one."""
    for l in locations:
        if l["pickup_type"] == "event":
            assert "pickup_index" not in l, (
                f"event location {l['name']} has pickup_index — events must not"
            )


def test_pickup_indices_are_unique(locations):
    indices = [l["pickup_index"] for l in locations if "pickup_index" in l]
    assert len(indices) == len(set(indices)), "duplicate pickup_index found"


def test_pickup_indices_form_contiguous_zero_through_148(locations):
    """149 game pickups in the patcher template → indices 0..148."""
    indices = sorted(l["pickup_index"] for l in locations if "pickup_index" in l)
    assert indices == list(range(149)), (
        f"expected 0..148 contiguous, got {len(indices)} entries; "
        f"min={indices[0] if indices else None} max={indices[-1] if indices else None}"
    )


def test_pickup_indices_match_template_order(locations, template):
    """For each non-event location, its pickup_index must point at a
    template `pickups` entry whose (scenario, actor) — or scenario +
    lua_callback.function for non-actor pickups — matches our entry."""
    template_pickups = template["pickups"]
    by_index = {l["pickup_index"]: l for l in locations if "pickup_index" in l}
    for idx, loc in by_index.items():
        tp = template_pickups[idx]
        pa = tp.get("pickup_actor")
        if pa:
            assert loc["scenario"] == pa["scenario"], (
                f"index {idx}: scenario mismatch loc={loc['scenario']} template={pa['scenario']}"
            )
            assert loc["actor"] == pa["actor"], (
                f"index {idx}: actor mismatch loc={loc['actor']} template={pa['actor']}"
            )
        else:
            cb = tp["pickup_lua_callback"]
            assert loc["scenario"] == cb["scenario"], (
                f"index {idx}: scenario mismatch loc={loc['scenario']} cb={cb['scenario']}"
            )
            assert loc["actor"] == cb["function"], (
                f"index {idx}: actor (callback fn) mismatch loc={loc['actor']} cb={cb['function']}"
            )


def test_known_spot_check_index_zero_is_artaria_charge_beam(locations):
    """Sanity: pickup_index 0 is the very first pickup in the template,
    which is Artaria's Charge Beam pedestal (s010_cave/ItemSphere_ChargeBeam)."""
    by_index = {l["pickup_index"]: l for l in locations if "pickup_index" in l}
    zero = by_index[0]
    assert zero["scenario"] == "s010_cave"
    assert zero["actor"] == "ItemSphere_ChargeBeam"


def test_known_spot_check_last_index_is_kraid_cutscene(locations):
    """pickup_index 148 is the final non-actor pickup in the template
    (Kraid's death cutscene in Cataris/s020_magma)."""
    by_index = {l["pickup_index"]: l for l in locations if "pickup_index" in l}
    last = by_index[148]
    assert last["scenario"] == "s020_magma"
    assert last["actor"] == "OnKraidDeath_CUSTOM"
    assert last["pickup_type"] == "cutscene"


def test_known_spot_check_index_137_is_first_non_actor(locations):
    """The template puts all 137 actor pickups first (0..136), then 12
    non-actor pickups (137..148). Index 137 is the boundary."""
    by_index = {l["pickup_index"]: l for l in locations if "pickup_index" in l}
    boundary = by_index[137]
    assert boundary["pickup_type"] != "actor"
    # And 136 is the last actor
    assert by_index[136]["pickup_type"] == "actor"


def test_datapackage_pickup_index_to_location_id_round_trip():
    """DataPackage.pickup_index_to_location_id should resolve to a valid
    AP location_id, and that ID should resolve back to the same scenario/actor."""
    from dread_archipelago.client.datapackage import DataPackage  # noqa: E402

    dp = DataPackage(apworld_data_dir=DATA)
    # Spot-check index 0
    loc_id = dp.pickup_index_to_location_id(0)
    assert loc_id is not None, "datapackage didn't load pickup_index 0"
    pickup = dp.location_id_to_pickup(loc_id)
    assert pickup is not None
    assert pickup.scenario == "s010_cave"
    assert pickup.actor == "ItemSphere_ChargeBeam"


def test_datapackage_returns_none_for_unknown_pickup_index():
    from dread_archipelago.client.datapackage import DataPackage  # noqa: E402

    dp = DataPackage(apworld_data_dir=DATA)
    assert dp.pickup_index_to_location_id(9999) is None
    assert dp.pickup_index_to_location_id(-1) is None
