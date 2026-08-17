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
    CROSS_SLOT_MAP_BASE_ICON,
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


def _own_progressive(name, sc, ac, item_name, stages, models, map_icon_id, idx):
    return {
        "location_name": name,
        "scenario": sc,
        "actor": ac,
        "pickup_type": "actor",
        "pickup_index": idx,
        "ap_item_name": item_name,
        "patcher_item_id": "",
        "patcher_model": "",
        "quantity": 1,
        "recipient_slot_name": "Samus",
        "is_own_player": True,
        "progression_stages": stages,
        "models": models,
        "map_icon_id": map_icon_id,
    }


_PROG_BEAM_STAGES = [
    [{"item_id": "ITEM_WEAPON_WIDE_BEAM", "quantity": 1}],
    [{"item_id": "ITEM_WEAPON_PLASMA_BEAM", "quantity": 1}],
    [{"item_id": "ITEM_WEAPON_WAVE_BEAM", "quantity": 1}],
]
_PROG_BEAM_MODELS = ["powerup_widebeam", "powerup_plasmabeam", "powerup_wavebeam"]


def test_progressive_item_emits_multistage_resources_models_icon():
    """An own-slot progressive placement threads its full multi-stage resources,
    per-tier model list, and progressive map-icon id into the overrides."""
    placements = {
        "slot_name": "Samus",
        "seed_id": "12345678",
        "starting_area": 0,
        "starting_items": {},
        "placements": [
            _own_progressive("Artaria: Beam", "s010_cave", "ItemSphere_ChargeBeam",
                             "Progressive Beam", _PROG_BEAM_STAGES,
                             _PROG_BEAM_MODELS, "PROGRESSIVE_BEAM", 0),
        ],
    }
    out = placements_to_overrides(placements)
    key = "s010_cave/ItemSphere_ChargeBeam"
    assert out["pickup_resources"][key] == _PROG_BEAM_STAGES
    assert out["pickup_models"][key] == _PROG_BEAM_MODELS
    assert out["pickup_captions"][key] == "Progressive Beam acquired."
    assert out["pickup_map_icons"][key] == {"icon_id": "PROGRESSIVE_BEAM"}


def test_progressive_merge_round_trips_and_preserves_original_actor():
    """Through merge_overrides the multi-stage resources + model list land on the
    template pickup, and the map_icon swaps to the progressive icon while keeping
    the template's original_actor anchor."""
    from build_patcher_json import merge_overrides  # noqa: E402

    template = {
        "configuration_identifier": "VANILLA",
        "layout_uuid": "00000000-0000-0000-0000-000000000000",
        "starting_location": {"scenario": "s010_cave", "actor": "OldStart"},
        "starting_items": {},
        "pickups": [
            {
                "pickup_type": "actor",
                "caption": "Charge Beam acquired.",
                "resources": [[{"item_id": "ITEM_WEAPON_CHARGE_BEAM", "quantity": 1}]],
                "pickup_actor": {"scenario": "s010_cave", "actor": "ItemSphere_ChargeBeam"},
                "model": ["powerup_chargebeam"],
                "map_icon": {"icon_id": "STALE",
                             "original_actor": {"scenario": "s010_cave",
                                                "actor": "MapProp"}},
            },
        ],
    }
    placements = {
        "slot_name": "Samus",
        "seed_id": "deadbeef",
        "starting_area": 0,
        "starting_items": {},
        "placements": [
            _own_progressive("Artaria: Beam", "s010_cave", "ItemSphere_ChargeBeam",
                             "Progressive Beam", _PROG_BEAM_STAGES,
                             _PROG_BEAM_MODELS, "PROGRESSIVE_BEAM", 0),
        ],
    }
    merged = merge_overrides(template, placements_to_overrides(placements))
    p = merged["pickups"][0]
    assert p["resources"] == _PROG_BEAM_STAGES
    assert p["model"] == _PROG_BEAM_MODELS
    assert p["caption"] == "Progressive Beam acquired."
    assert p["map_icon"]["icon_id"] == "PROGRESSIVE_BEAM"
    # original_actor anchor preserved; the stale icon branch dropped.
    assert p["map_icon"]["original_actor"] == {"scenario": "s010_cave",
                                               "actor": "MapProp"}


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


def test_own_slot_item_emits_icon_id_map_icon():
    """An own-slot item with a concrete model also re-skins its MAP icon to
    that model's icon_id, mirroring the in-world model rewrite — otherwise the
    minimap legend keeps showing whatever item the starter preset baked here."""
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
    assert out["pickup_map_icons"]["s010_cave/ItemSphere_ChargeBeam"] == {
        "icon_id": "item_missiletank"
    }


def test_own_slot_orb_item_emits_custom_icon_label():
    """Items rendered as the neutral orb (model ``itemsphere`` — e.g. Metroid
    DNA) have no dedicated icon_id, so they get a labelled custom_icon instead,
    matching Randovania's own exporter for itemsphere-model pickups."""
    placements = {
        "slot_name": "Samus",
        "seed_id": "12345678",
        "starting_area": 0,
        "starting_items": {},
        "placements": [
            _own("Artaria: Corpius Reward", "s010_cave", "ItemSphere_ChargeBeam",
                 "Metroid DNA 1", "ITEM_RANDO_ARTIFACT_1", 1, 0,
                 patcher_model="itemsphere"),
        ],
    }
    out = placements_to_overrides(placements)
    assert out["pickup_map_icons"]["s010_cave/ItemSphere_ChargeBeam"] == {
        "custom_icon": {"label": "METROID DNA 1"}
    }


def test_own_slot_item_without_model_leaves_map_icon_untouched():
    """No model on the placement ⇒ no map-icon override either, so the
    template's baked icon is left alone (same backward-compat rule as the
    in-world model)."""
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
    assert out["pickup_map_icons"] == {}


def test_cross_slot_item_emits_unknown_custom_icon():
    """A foreign item's map icon becomes the neutral "?" (base_icon ``unknown``)
    labelled with the RECIPIENT and item name — it must name who the off-world
    pickup is for, and must not keep advertising the Dread item the starter
    preset baked at this spot."""
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
    assert out["pickup_map_icons"]["s010_cave/ItemSphere_ChargeBeam"] == {
        "custom_icon": {"label": "BUTTONPUSHER'S THE BIG BUTTON",
                        "base_icon": CROSS_SLOT_MAP_BASE_ICON}
    }


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


def test_configuration_identifier_includes_seed_prefix_only():
    placements = {
        "slot_name": "Samus",
        "seed_id": "1234567890abcdef",
        "starting_area": 0,
        "starting_items": {},
        "placements": [],
    }
    out = placements_to_overrides(placements)
    assert out["configuration_identifier"] == "AP-12345678"


# ---- split_saves stays OFF -------------------------------------------------

def test_split_saves_is_disabled_in_the_starter_preset():
    """``cosmetic_patches.split_saves`` must stay False.

    Turning it on arms open-dread-rando's ``patch_saveslot`` (writes
    ``rom:/RDVHASH``), which in turn arms the exlaunch sysmodule's
    ``setSeedSaveProfile``. That runs at ``nn::fs::MountRom`` on every boot and
    writes three CStrId entries at a HARDCODED bank index (``STRINGBANK_PROFILE0
    = 917``) under a per-version bank base — a boot-time in-place overwrite whose
    first consumer is the main menu's save-slot list. See the ``SPLIT_SAVES``
    block in patcher_pipeline for the full rationale.

    This pins the JSON against the documented constant so a future re-import of
    the starter preset (upstream's schema default is False, but the file is
    hand-maintained) can't silently flip it back on."""
    from dread.patcher_pipeline import SPLIT_SAVES, load_starter_template
    preset = load_starter_template()
    assert preset["cosmetic_patches"]["split_saves"] is SPLIT_SAVES is False


# ---- Switch save-fs entry-name budget (64-byte hardware cap) ---------------
#
# open-dread-rando derives the in-game save profile directory as
# RDV_{configuration_identifier}_{layout_uuid}, and the exlaunch sysmodule
# appends _0/_1/_2. Nintendo's save-data filesystem caps entry names at
# 64 bytes; an over-long name fails save creation on REAL HARDWARE ONLY
# (Ryujinx maps saves to the host fs and never catches it). AP slot names
# go up to 16 characters (Generate.py truncates at [:16]) and are NOT byte-
# limited, so the identifier must never embed the slot — layout_uuid
# (sha256 of seed:slot) already disambiguates.

def _save_entry_helpers():
    from dread.patcher_pipeline import (
        SWITCH_SAVE_ENTRY_MAX_BYTES,
        save_entry_name,
    )
    return SWITCH_SAVE_ENTRY_MAX_BYTES, save_entry_name


@pytest.mark.parametrize("slot_name", [
    "WWWWWWWWWWWWWWWW",          # 16-char worst case (AP's max slot name)
    "サムス・アラン" * 2 + "夢夢",  # 16 chars, multi-byte UTF-8
    "Samus",
])
def test_save_entry_name_fits_switch_savefs(slot_name):
    """The FINAL save entry name must stay under the 64-byte Switch cap for
    any AP slot name, including the 16-char maximum and non-ASCII names."""
    max_bytes, save_entry_name = _save_entry_helpers()
    placements = {
        "slot_name": slot_name,
        "seed_id": "98765432109876543210",  # AP seed_name: up to 20 digits
        "starting_area": 0,
        "starting_items": {},
        "placements": [],
    }
    out = placements_to_overrides(placements)
    entry = save_entry_name(out["configuration_identifier"], out["layout_uuid"])
    n = len(entry.encode("utf-8"))
    assert n <= max_bytes, f"{entry!r} is {n} bytes (> {max_bytes})"
    # Pin the exact construction: RDV_(4) + AP-________(11) + _(1) + uuid(36)
    # + _0(2) = 54 bytes, independent of the slot name.
    assert n == 54
    assert entry.startswith("RDV_AP-98765432_")
    assert entry.endswith("_0")


def test_save_entry_guard_rejects_old_slot_bearing_identifier():
    """Regression pin: the PRE-FIX identifier form AP-{seed}-{slot} pushed the
    entry name to ~71 bytes for a 16-char slot. If anyone reintroduces the
    slot component (or any over-long identifier), merge_overrides must fail
    fast at patch time instead of letting save creation fail on hardware."""
    from dread.patcher_pipeline import merge_overrides

    template = {
        "configuration_identifier": "VANILLA",
        "layout_uuid": "00000000-0000-0000-0000-000000000000",
        "starting_location": {"scenario": "s010_cave", "actor": "OldStart"},
        "starting_items": {},
        "pickups": [],
    }
    overrides = {
        # The exact pre-65d28f2 shape with AP's 16-char max slot name.
        "configuration_identifier": "AP-98765432-WWWWWWWWWWWWWWWW",
        "layout_uuid": "12345678-1234-4234-8234-123456789012",
    }
    with pytest.raises(ValueError, match="64"):
        merge_overrides(template, overrides)


def test_save_entry_guard_accepts_template_passthrough():
    """A template-only merge (no identifier overrides) must still pass the
    guard — the starter preset's 8-char identifier is well under budget."""
    from dread.patcher_pipeline import merge_overrides, save_entry_name

    template = {
        "configuration_identifier": "EPDRRG6F",
        "layout_uuid": "00000000-0000-0000-0000-000000000000",
        "starting_location": {"scenario": "s010_cave", "actor": "OldStart"},
        "starting_items": {},
        "pickups": [],
    }
    merged = merge_overrides(template, {})
    entry = save_entry_name(merged["configuration_identifier"],
                            merged["layout_uuid"])
    assert len(entry.encode("utf-8")) == 51  # RDV_ + 8 + _ + 36 + _0


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


def test_spoiler_log_passes_through():
    """The payload's real end-credits spoiler log rides the overrides
    verbatim (merge_overrides writes it over the template's baked example)."""
    log = {"Grapple Beam": "Cataris: Kraid Arena",
           "Progressive Beam": "Artaria: Spot A\nSamusB's Cool Zone"}
    placements = {
        "slot_name": "Samus",
        "seed_id": "x",
        "starting_area": 0,
        "starting_items": {},
        "placements": [],
        "spoiler_log": log,
    }
    out = placements_to_overrides(placements)
    assert out["spoiler_log"] == log


def test_spoiler_log_defaults_to_blank_not_passthrough():
    """A payload predating the spoiler_log key must yield {} — every AP seed's
    template log is false, so old payloads blank the credits section rather
    than keep the starter preset's example placements."""
    placements = {
        "slot_name": "Samus",
        "seed_id": "x",
        "starting_area": 0,
        "starting_items": {},
        "placements": [],
    }
    out = placements_to_overrides(placements)
    assert out["spoiler_log"] == {}


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
    assert merged["configuration_identifier"] == "AP-deadbeef"
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


def test_light_patches_ride_the_payload():
    """Disabled Lights: the world's light_patches carry through the payload into
    the patcher's mass_delete_actors. Absent ⇒ no block at all."""
    from build_patcher_json import merge_overrides  # noqa: E402

    template = {
        "configuration_identifier": "VANILLA",
        "layout_uuid": "00000000-0000-0000-0000-000000000000",
        "pickups": [],
    }
    base = {"slot_name": "Samus", "seed_id": "deadbeef",
            "starting_area": 0, "placements": []}

    overrides = placements_to_overrides(dict(base, light_patches=[
        {"scenario": "s050_forest", "actor_layer": "rLightsLayer",
         "method": "all"},
    ]))
    merged = merge_overrides(template, overrides)
    assert merged["mass_delete_actors"]["to_remove"] == [
        {"scenario": "s050_forest", "actor_layer": "rLightsLayer",
         "method": "all"},
    ]

    # A payload predating the option (or a lights-on seed) leaves it absent.
    assert placements_to_overrides(base)["light_patches"] == []
    assert "mass_delete_actors" not in merge_overrides(
        template, placements_to_overrides(base))


def test_transport_room_names_merge_into_camera_dict():
    """Transport rando: room-name overrides rewrite ONLY the named transport
    collision cameras, leaving other room names intact."""
    from build_patcher_json import merge_overrides  # noqa: E402

    template = {
        "configuration_identifier": "VANILLA",
        "layout_uuid": "00000000-0000-0000-0000-000000000000",
        "pickups": [],
        "cosmetic_patches": {"lua": {"camera_names_dict": {
            "s010_cave": {
                "collision_camera_034": "Transport to Dairon",   # will be rewritten
                "collision_camera_050": "Some Other Room",        # untouched
            },
        }}},
    }
    placements = {
        "slot_name": "Samus",
        "seed_id": "deadbeef",
        "starting_area": 0,
        "placements": [],
        "transport_room_names": {
            "s010_cave": {"collision_camera_034": "Transport to Ghavoran - Early Supers"},
        },
    }
    merged = merge_overrides(template, placements_to_overrides(placements))
    cam = merged["cosmetic_patches"]["lua"]["camera_names_dict"]["s010_cave"]
    assert cam["collision_camera_034"] == "Transport to Ghavoran - Early Supers"
    assert cam["collision_camera_050"] == "Some Other Room"


def test_no_transport_room_names_leaves_camera_dict_untouched():
    """Transport rando off (empty overrides) ⇒ camera_names_dict byte-identical."""
    from build_patcher_json import merge_overrides  # noqa: E402

    cam_in = {"s010_cave": {"collision_camera_034": "Transport to Dairon"}}
    template = {
        "configuration_identifier": "VANILLA",
        "layout_uuid": "00000000-0000-0000-0000-000000000000",
        "pickups": [],
        "cosmetic_patches": {"lua": {"camera_names_dict": cam_in}},
    }
    placements = {"slot_name": "Samus", "seed_id": "deadbeef",
                  "starting_area": 0, "placements": []}
    merged = merge_overrides(template, placements_to_overrides(placements))
    assert merged["cosmetic_patches"]["lua"]["camera_names_dict"] == cam_in
