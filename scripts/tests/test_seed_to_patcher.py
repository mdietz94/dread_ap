"""Unit tests for the AP-seed → patcher-overrides converter.

Exercises the conversion logic with synthetic placements JSON — does not
require Archipelago to be installed. The actual generation pipeline
(yaml → seed zip → overrides → patcher input → RomFS) is documented in
``docs/e2e-runbook.md`` and exercised manually.

Run with:  python -m pytest scripts/tests/test_seed_to_patcher.py -v
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from seed_to_patcher_overrides import (  # noqa: E402
    CROSS_SLOT_PLACEHOLDER,
    _layout_uuid_from_seed,
    find_placements_in_zip,
    placements_to_overrides,
)


# ---- pure-function tests --------------------------------------------------

def _own(name, sc, ac, item_name, patcher_id, qty, idx, pickup_type="actor",
         patcher_model="powerup_chargebeam"):
    return {
        "location_name": name,
        "scenario": sc,
        "actor": ac,
        "pickup_type": pickup_type,
        "pickup_index": idx,
        "ap_item_name": item_name,
        "patcher_item_id": patcher_id,
        "patcher_model": patcher_model,
        "quantity": qty,
        "recipient_slot_name": "Samus",
        "is_own_player": True,
    }


def _cross(name, sc, ac, item_name, recipient, idx, pickup_type="actor"):
    return {
        "location_name": name,
        "scenario": sc,
        "actor": ac,
        "pickup_type": pickup_type,
        "pickup_index": idx,
        "ap_item_name": item_name,
        "patcher_item_id": "",  # cross-slot; we don't know the dest game's IDs
        "quantity": 1,
        "recipient_slot_name": recipient,
        "is_own_player": False,
    }


def test_layout_uuid_matches_schema_regex():
    pattern = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )
    uuid = _layout_uuid_from_seed("seed-123", "Samus")
    assert pattern.match(uuid), f"derived UUID {uuid} doesn't match schema regex"


def test_layout_uuid_stable_for_same_input():
    a = _layout_uuid_from_seed("seed-123", "Samus")
    b = _layout_uuid_from_seed("seed-123", "Samus")
    assert a == b


def test_layout_uuid_differs_across_slots():
    a = _layout_uuid_from_seed("seed-123", "Samus")
    b = _layout_uuid_from_seed("seed-123", "Other")
    assert a != b


def test_own_slot_item_becomes_pickup_resource():
    placements = {
        "slot_name": "Samus",
        "seed_id": "12345678",
        "starting_area": 0,
        "starting_items": {"ITEM_WEAPON_MISSILE_MAX": 0},
        "placements": [
            _own("Artaria: Charge Beam Room", "s010_cave", "ItemSphere_ChargeBeam",
                 "Charge Beam", "ITEM_WEAPON_CHARGE_BEAM", 1, 0),
        ],
    }
    out = placements_to_overrides(placements)
    key = "s010_cave/ItemSphere_ChargeBeam"
    assert key in out["pickup_resources"]
    res = out["pickup_resources"][key]
    assert res == [[{"item_id": "ITEM_WEAPON_CHARGE_BEAM", "quantity": 1}]]
    # Own-item captions are overwritten to name the AP item (was: stale template).
    assert out["pickup_captions"][key] == "Charge Beam acquired."
    # ...and so is the in-world model, so the sphere matches the item granted
    # instead of keeping the starter preset's vanilla/progressive model.
    assert out["pickup_models"][key] == ["powerup_chargebeam"]


def test_cross_slot_item_becomes_placeholder_with_caption():
    placements = {
        "slot_name": "Samus",
        "seed_id": "12345678",
        "starting_area": 0,
        "starting_items": {},
        "placements": [
            _cross("Artaria: Charge Beam Room", "s010_cave", "ItemSphere_ChargeBeam",
                   "The Big Button", "ButtonPusher", 0),
        ],
    }
    out = placements_to_overrides(placements)
    key = "s010_cave/ItemSphere_ChargeBeam"
    assert out["pickup_resources"][key] == [[dict(CROSS_SLOT_PLACEHOLDER)]]
    # The placeholder must grant nothing locally (the real item is sent to the
    # recipient over the wire); quantity 0 is Randovania's coop convention.
    assert CROSS_SLOT_PLACEHOLDER["quantity"] == 0
    assert out["pickup_captions"][key] == "Sent The Big Button to ButtonPusher"
    # The in-world sprite is rewritten to the neutral orb so foreign items
    # look generic instead of leaking what they grant via the vanilla model.
    assert out["pickup_models"][key] == ["itemsphere"]


def test_own_slot_item_emits_its_own_model():
    """Own-slot pickups are re-skinned to the model of the item they grant, so a
    location the starter preset baked as (say) a Charge Beam now shows the
    shuffled item's model instead of the stale vanilla one."""
    placements = {
        "slot_name": "Samus",
        "seed_id": "12345678",
        "starting_area": 0,
        "starting_items": {},
        "placements": [
            _own("Artaria: Charge Beam Room", "s010_cave", "ItemSphere_ChargeBeam",
                 "Missile Tank", "ITEM_WEAPON_MISSILE_MAX", 2, 0,
                 patcher_model="item_missiletank"),
        ],
    }
    out = placements_to_overrides(placements)
    assert out["pickup_models"]["s010_cave/ItemSphere_ChargeBeam"] == ["item_missiletank"]


def test_own_slot_item_without_model_leaves_template_model():
    """When the placement carries no model (older payloads / the offline CLI
    flow predating the field), the template's baked model is left untouched
    rather than guessed — backward compatible, no crash."""
    placements = {
        "slot_name": "Samus",
        "seed_id": "12345678",
        "starting_area": 0,
        "starting_items": {},
        "placements": [
            _own("Artaria: Charge Beam Room", "s010_cave", "ItemSphere_ChargeBeam",
                 "Charge Beam", "ITEM_WEAPON_CHARGE_BEAM", 1, 0,
                 patcher_model=""),
        ],
    }
    out = placements_to_overrides(placements)
    assert out["pickup_models"] == {}


def test_event_placements_are_skipped():
    placements = {
        "slot_name": "Samus",
        "seed_id": "x",
        "starting_area": 0,
        "starting_items": {},
        "placements": [
            {
                "location_name": "Event: ArtariaCU",
                "scenario": "s010_cave",
                "actor": "",
                "pickup_type": "event",
                "pickup_index": None,
                "ap_item_name": "Event: ArtariaCU",
                "patcher_item_id": "",
                "quantity": 1,
                "recipient_slot_name": "Samus",
                "is_own_player": True,
            },
        ],
    }
    out = placements_to_overrides(placements)
    assert out["pickup_resources"] == {}
    assert out["pickup_captions"] == {}


def test_non_actor_pickups_are_overridden():
    """Gate B: EMMI / corex / corpius / cutscene rewards ARE overridden now.
    Their (scenario, actor) keys the template's pickup_lua_callback, so an
    AP-placed item (e.g. a Metroid DNA → ITEM_RANDO_ARTIFACT_k) lands on the
    boss instead of leaving its vanilla drop."""
    placements = {
        "slot_name": "Samus",
        "seed_id": "x",
        "starting_area": 0,
        "starting_items": {},
        "placements": [
            _own("Artaria: Corpius", "s010_cave", "OnCorpiusDeath_CUSTOM",
                 "Metroid DNA 1", "ITEM_RANDO_ARTIFACT_1", 1, 138,
                 pickup_type="corpius"),
        ],
    }
    out = placements_to_overrides(placements)
    assert out["pickup_resources"] == {
        "s010_cave/OnCorpiusDeath_CUSTOM": [
            [{"item_id": "ITEM_RANDO_ARTIFACT_1", "quantity": 1}]
        ]
    }


def test_configuration_identifier_includes_slot_and_seed_prefix():
    placements = {
        "slot_name": "Samus",
        "seed_id": "1234567890abcdef",
        "starting_area": 0,
        "starting_items": {},
        "placements": [],
    }
    out = placements_to_overrides(placements)
    assert out["configuration_identifier"] == "AP-12345678-Samus"


def test_starting_location_for_artaria():
    placements = {
        "slot_name": "Samus",
        "seed_id": "x",
        "starting_area": 0,
        "starting_items": {},
        "placements": [],
    }
    out = placements_to_overrides(placements)
    assert out["starting_location"] == {"scenario": "s010_cave", "actor": "StartPoint0"}


def test_starting_items_round_trip():
    placements = {
        "slot_name": "Samus",
        "seed_id": "x",
        "starting_area": 0,
        "starting_items": {"ITEM_WEAPON_MISSILE_MAX": 15, "ITEM_MAX_LIFE": 99},
        "placements": [],
    }
    out = placements_to_overrides(placements)
    assert out["starting_items"] == {"ITEM_WEAPON_MISSILE_MAX": 15, "ITEM_MAX_LIFE": 99}


def test_layout_uuid_override_honored():
    placements = {
        "slot_name": "Samus",
        "seed_id": "x",
        "starting_area": 0,
        "starting_items": {},
        "placements": [],
    }
    out = placements_to_overrides(
        placements, layout_uuid="11111111-2222-3333-4444-555555555555"
    )
    assert out["layout_uuid"] == "11111111-2222-3333-4444-555555555555"


# ---- zip extraction tests -------------------------------------------------

def test_find_placements_in_zip(tmp_path):
    zip_path = tmp_path / "AP_demo.zip"
    p1 = {
        "slot_name": "Samus",
        "seed_id": "x",
        "starting_area": 0,
        "starting_items": {},
        "placements": [],
    }
    p2 = {
        "slot_name": "ButtonPusher",
        "seed_id": "x",
        "placements": [],
    }
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("AP_demo.archipelago", b"<binary multidata>")
        zf.writestr("AP_demo_P1_Dread_Samus.json", json.dumps(p1))
        zf.writestr("AP_demo_P2_Dread_Other.json", json.dumps(p2))

    found = find_placements_in_zip(zip_path, "Samus")
    assert found["slot_name"] == "Samus"
    assert found["seed_id"] == "x"


def test_find_placements_missing_slot_raises(tmp_path):
    zip_path = tmp_path / "AP_demo.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("AP_demo.archipelago", b"<binary>")
        zf.writestr("AP_demo_P1_Dread_Samus.json",
                    json.dumps({"slot_name": "Samus", "placements": []}))
    with pytest.raises(SystemExit, match="no Dread placements JSON"):
        find_placements_in_zip(zip_path, "NonExistent")


# ---- integration with build_patcher_json -----------------------------------

def test_overrides_round_trip_through_build_patcher_json(tmp_path):
    """The overrides JSON we produce must be consumable by build_patcher_json
    against a real template — same end-to-end shape the runbook describes."""
    from build_patcher_json import merge_overrides  # noqa: E402

    template = {
        "configuration_identifier": "VANILLA",
        "layout_uuid": "00000000-0000-0000-0000-000000000000",
        "starting_location": {"scenario": "s010_cave", "actor": "OldStart"},
        "starting_items": {},
        "pickups": [
            {
                "pickup_type": "actor",
                "caption": "Morph Ball acquired.",
                "resources": [[{"item_id": "ITEM_MORPH_BALL", "quantity": 1}]],
                "pickup_actor": {"scenario": "s010_cave", "actor": "ItemSphere_ChargeBeam"},
                "model": ["x"],
            },
            {
                "pickup_type": "actor",
                "caption": "Missile Tank acquired.",
                "resources": [[{"item_id": "ITEM_WEAPON_MISSILE_MAX", "quantity": 2}]],
                "pickup_actor": {"scenario": "s010_cave", "actor": "Item_MissileTank011"},
                "model": ["y"],
            },
        ],
    }
    placements = {
        "slot_name": "Samus",
        "seed_id": "deadbeef",
        "starting_area": 0,
        "starting_items": {"ITEM_WEAPON_MISSILE_MAX": 15},
        "placements": [
            _own("Artaria: Charge Beam Room", "s010_cave", "ItemSphere_ChargeBeam",
                 "Charge Beam", "ITEM_WEAPON_CHARGE_BEAM", 1, 0),
            _cross("Artaria: MissileTank011", "s010_cave", "Item_MissileTank011",
                   "Big Button", "ButtonPusher", 1),
        ],
    }
    overrides = placements_to_overrides(placements)
    merged = merge_overrides(template, overrides)

    # Top-level fields applied
    assert merged["layout_uuid"] != template["layout_uuid"]
    assert merged["configuration_identifier"] == "AP-deadbeef-Samus"
    assert merged["starting_location"] == {"scenario": "s010_cave", "actor": "StartPoint0"}
    assert merged["starting_items"] == {"ITEM_WEAPON_MISSILE_MAX": 15}

    # Own-slot pickup overridden with our item AND re-skinned to its model, so
    # the template's stale vanilla model ("x") no longer leaks through.
    morph = next(p for p in merged["pickups"]
                 if p["pickup_actor"]["actor"] == "ItemSphere_ChargeBeam")
    assert morph["resources"][0][0]["item_id"] == "ITEM_WEAPON_CHARGE_BEAM"
    assert morph["model"] == ["powerup_chargebeam"]

    # Cross-slot pickup gets placeholder resource + custom caption + the
    # neutral itemsphere model so it doesn't visually leak the dest item.
    missile = next(p for p in merged["pickups"]
                   if p["pickup_actor"]["actor"] == "Item_MissileTank011")
    assert missile["resources"][0][0]["item_id"] == "ITEM_WEAPON_MISSILE_MAX"
    # ...but quantity 0, so collecting a foreign item grants the local player
    # NOTHING (regression guard: it used to bake quantity 2 → +2 missiles).
    assert missile["resources"][0][0]["quantity"] == 0
    assert missile["caption"] == "Sent Big Button to ButtonPusher"
    assert missile["model"] == ["itemsphere"]
