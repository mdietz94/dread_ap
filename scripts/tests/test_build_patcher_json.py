"""Unit tests for build_patcher_json.merge_overrides.

Doesn't require the real Randovania template — uses a tiny synthetic one.
Run with:  python -m pytest scripts/tests/test_build_patcher_json.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from build_patcher_json import merge_overrides  # noqa: E402


def _template() -> dict:
    return {
        "configuration_identifier": "VANILLA",
        "layout_uuid": "00000000-0000-0000-0000-000000000000",
        "starting_location": {"scenario": "s010_cave", "actor": "StartPoint0"},
        "starting_items": {"ITEM_WEAPON_MISSILE_MAX": 0},
        "pickups": [
            {
                "pickup_type": "actor",
                "caption": "Morph Ball acquired.",
                "resources": [[{"item_id": "ITEM_MORPH_BALL", "quantity": 1}]],
                "pickup_actor": {"scenario": "s010_cave", "actor": "ItemSphere_ChargeBeam"},
                "model": ["powerup_morphball"],
                "map_icon": {
                    "icon_id": "powerup_morphball",
                    "original_actor": {"scenario": "s010_cave", "actor": "powerup_chargebeam"},
                },
            },
            {
                "pickup_type": "actor",
                "caption": "Missile Tank acquired.",
                "resources": [[{"item_id": "ITEM_WEAPON_MISSILE_MAX", "quantity": 2}]],
                "pickup_actor": {"scenario": "s010_cave", "actor": "Item_MissileTank011"},
                "model": ["item_missiletank"],
                "map_icon": {"icon_id": "item_missiletank"},
            },
            {
                "pickup_type": "corpius",
                "caption": "Metroid DNA 1 acquired.",
                "resources": [[{"item_id": "ITEM_RANDO_ARTIFACT_1", "quantity": 1}]],
                "pickup_actor": None,
                "pickup_lua_callback": {"scenario": "s010_cave",
                                        "function": "OnCorpiusDeath_CUSTOM"},
            },
        ],
        "cosmetic_patches": {
            "config": {"AIManager": {"bShowBossLifebar": True, "bShowEnemyLife": False,
                                     "bShowEnemyDamage": False, "bShowPlayerDamage": True}},
            "lua": {"custom_init": {"enable_death_counter": True,
                                    "enable_room_name_display": "NEVER"}},
        },
        "game_patches": {"raven_beak_damage_table_handling": "consistent_low",
                         "nerf_power_bombs": True, "default_x_released": False},
        "objective": {"required_artifacts": 3, "hints": ["hint text"]},
        # The real starter preset bakes Randovania's own example placements
        # here (rendered into the end credits by patch_credits) — false for
        # any AP seed.
        "spoiler_log": {"Grapple Beam": "Burenia - Teleport to Ferenia"},
    }


def test_no_overrides_round_trips():
    # merge_overrides always forces enable_remote_lua=True (the in-game Lua
    # gates RL.Init() on this flag; without it the exlaunch socket never
    # binds and the wire is dead). So with empty overrides the output
    # equals template + that one field; everything else is unchanged.
    t = _template()
    out = merge_overrides(t, {})
    assert out["enable_remote_lua"] is True
    expected = {**t, "enable_remote_lua": True}
    assert out == expected


def test_top_level_overrides_applied():
    t = _template()
    out = merge_overrides(t, {
        "layout_uuid": "11111111-2222-3333-4444-555555555555",
        "configuration_identifier": "AP-test",
        "starting_location": {"scenario": "s020_magma", "actor": "StartPoint5"},
        "starting_items": {"ITEM_VARIA_SUIT": 1},
    })
    assert out["layout_uuid"] == "11111111-2222-3333-4444-555555555555"
    assert out["configuration_identifier"] == "AP-test"
    assert out["starting_location"]["scenario"] == "s020_magma"
    assert out["starting_items"] == {"ITEM_VARIA_SUIT": 1}


def test_pickup_resource_override():
    t = _template()
    out = merge_overrides(t, {
        "pickup_resources": {
            "s010_cave/ItemSphere_ChargeBeam": [[
                {"item_id": "ITEM_WEAPON_PLASMA_BEAM", "quantity": 1}
            ]],
        },
    })
    morph = next(p for p in out["pickups"]
                 if p["pickup_actor"]["actor"] == "ItemSphere_ChargeBeam")
    missile = next(p for p in out["pickups"]
                   if p["pickup_actor"]["actor"] == "Item_MissileTank011")
    assert morph["resources"][0][0]["item_id"] == "ITEM_WEAPON_PLASMA_BEAM"
    # Untouched pickup keeps its vanilla resource
    assert missile["resources"][0][0]["item_id"] == "ITEM_WEAPON_MISSILE_MAX"


def test_pickup_caption_override():
    t = _template()
    out = merge_overrides(t, {
        "pickup_captions": {
            "s010_cave/Item_MissileTank011": "Sent Missile Tank to Player 2",
        },
    })
    missile = next(p for p in out["pickups"]
                   if p["pickup_actor"]["actor"] == "Item_MissileTank011")
    assert missile["caption"] == "Sent Missile Tank to Player 2"


def test_pickup_model_override():
    t = _template()
    out = merge_overrides(t, {
        "pickup_models": {
            "s010_cave/Item_MissileTank011": ["itemsphere"],
        },
    })
    missile = next(p for p in out["pickups"]
                   if p["pickup_actor"]["actor"] == "Item_MissileTank011")
    # Targeted pickup is reskinned; unrelated pickup keeps its vanilla model.
    assert missile["model"] == ["itemsphere"]
    morph = next(p for p in out["pickups"]
                 if p["pickup_actor"]["actor"] == "ItemSphere_ChargeBeam")
    assert morph["model"] == ["powerup_morphball"]


def test_pickup_model_override_skips_non_actor_pickups():
    """Non-actor pickups (boss / EMMI drops) have no ``model`` field in the
    template — there's no in-world sphere to re-skin. The override must not
    inject the field, since the patcher would either ignore it or choke on
    the unexpected shape."""
    t = _template()
    out = merge_overrides(t, {
        "pickup_models": {
            "s010_cave/OnCorpiusDeath_CUSTOM": ["itemsphere"],
        },
    })
    corpius = next(p for p in out["pickups"] if p["pickup_actor"] is None)
    assert "model" not in corpius


def test_pickup_map_icon_override_icon_id_preserves_original_actor():
    """An ``icon_id`` map-icon override replaces the icon while keeping the
    template's ``original_actor`` (it anchors the icon to the right map prop)."""
    t = _template()
    out = merge_overrides(t, {
        "pickup_map_icons": {
            "s010_cave/ItemSphere_ChargeBeam": {"icon_id": "item_powerbombtank"},
        },
    })
    charge = next(p for p in out["pickups"]
                  if p["pickup_actor"]["actor"] == "ItemSphere_ChargeBeam")
    assert charge["map_icon"] == {
        "icon_id": "item_powerbombtank",
        "original_actor": {"scenario": "s010_cave", "actor": "powerup_chargebeam"},
    }
    # Unrelated pickup keeps its vanilla map icon.
    missile = next(p for p in out["pickups"]
                   if p["pickup_actor"]["actor"] == "Item_MissileTank011")
    assert missile["map_icon"] == {"icon_id": "item_missiletank"}


def test_pickup_map_icon_override_custom_icon_drops_icon_id_branch():
    """A ``custom_icon`` override must drop the template's ``icon_id`` branch —
    the schema's map_icon is a oneOf, so the two icon branches can't coexist."""
    t = _template()
    out = merge_overrides(t, {
        "pickup_map_icons": {
            "s010_cave/ItemSphere_ChargeBeam": {
                "custom_icon": {"label": "SOME ITEM", "base_icon": "unknown"},
            },
        },
    })
    charge = next(p for p in out["pickups"]
                  if p["pickup_actor"]["actor"] == "ItemSphere_ChargeBeam")
    assert "icon_id" not in charge["map_icon"]
    assert charge["map_icon"]["custom_icon"] == {"label": "SOME ITEM", "base_icon": "unknown"}
    # original_actor still carried over.
    assert charge["map_icon"]["original_actor"] == {
        "scenario": "s010_cave", "actor": "powerup_chargebeam"
    }


def test_pickup_map_icon_override_skips_non_actor_pickups():
    """Non-actor pickups (boss / EMMI drops) have no ``map_icon`` in the
    template — they don't appear on the item map — so the override must not
    inject one."""
    t = _template()
    out = merge_overrides(t, {
        "pickup_map_icons": {
            "s010_cave/OnCorpiusDeath_CUSTOM": {
                "custom_icon": {"label": "DNA", "base_icon": "unknown"},
            },
        },
    })
    corpius = next(p for p in out["pickups"] if p["pickup_actor"] is None)
    assert "map_icon" not in corpius


def test_unknown_pickup_key_raises():
    # merge_overrides is now a pure library function — raises ValueError
    # rather than SystemExit. The CLI script (scripts/build_patcher_json.py)
    # catches the ValueError and re-raises as SystemExit so users still see
    # a clean error from the command line.
    t = _template()
    with pytest.raises(ValueError, match="pickup keys"):
        merge_overrides(t, {
            "pickup_resources": {
                "s010_cave/DoesNotExist": [[{"item_id": "ITEM_X", "quantity": 1}]],
            },
        })


def test_cosmetic_combat_overrides_applied():
    t = _template()
    out = merge_overrides(t, {
        "cosmetic_combat": {
            "bShowEnemyLife": True,
            "enable_room_name_display": "WITH_FADE",
            "show_dna_in_hud": True,
            "raven_beak_damage_table_handling": "consistent_high",
            "nerf_power_bombs": False,
            "default_x_released": True,
        },
    })
    ai = out["cosmetic_patches"]["config"]["AIManager"]
    assert ai["bShowEnemyLife"] is True
    # Untouched leaves keep template values.
    assert ai["bShowBossLifebar"] is True
    assert ai["bShowPlayerDamage"] is True
    assert out["cosmetic_patches"]["lua"]["custom_init"]["enable_room_name_display"] == "WITH_FADE"
    assert out["cosmetic_patches"]["lua"]["custom_init"]["enable_death_counter"] is True
    # show_dna_in_hud is ADDED to custom_init (the template may omit it; newer
    # open-dread-rando indexes it directly, so we must always write it).
    assert out["cosmetic_patches"]["lua"]["custom_init"]["show_dna_in_hud"] is True
    assert out["game_patches"]["raven_beak_damage_table_handling"] == "consistent_high"
    assert out["game_patches"]["nerf_power_bombs"] is False
    assert out["game_patches"]["default_x_released"] is True


def test_cosmetic_combat_absent_leaves_template_untouched():
    t = _template()
    out = merge_overrides(t, {})
    assert out["cosmetic_patches"] == _template()["cosmetic_patches"]
    assert out["game_patches"] == _template()["game_patches"]


def test_cosmetic_combat_missing_parent_raises():
    t = _template()
    del t["game_patches"]
    with pytest.raises(KeyError, match="game_patches"):
        merge_overrides(t, {"cosmetic_combat": {"nerf_power_bombs": False}})


def test_light_patches_added_to_mass_delete_actors():
    """Disabled Lights lands as open-dread-rando mass_delete_actors entries —
    Randovania's exact _light_patches shape."""
    t = _template()
    out = merge_overrides(t, {
        "light_patches": [
            {"scenario": "s030_baselab", "actor_layer": "rLightsLayer",
             "method": "all"},
        ],
    })
    assert out["mass_delete_actors"] == {
        "to_remove": [{"scenario": "s030_baselab",
                       "actor_layer": "rLightsLayer", "method": "all"}],
        "to_keep": [],
    }


def test_light_patches_merge_into_existing_deletions():
    """A hand-written override file's own actor deletions must survive: light
    patches append to to_remove rather than replacing it."""
    t = _template()
    t["mass_delete_actors"] = {
        "to_remove": [{"scenario": "s010_cave", "method": "all"}],
        "to_keep": [],
    }
    out = merge_overrides(t, {
        "light_patches": [
            {"scenario": "s020_magma", "actor_layer": "rLightsLayer",
             "method": "all"},
        ],
    })
    assert out["mass_delete_actors"]["to_remove"] == [
        {"scenario": "s010_cave", "method": "all"},
        {"scenario": "s020_magma", "actor_layer": "rLightsLayer",
         "method": "all"},
    ]


def test_no_light_patches_leaves_mass_delete_actors_absent():
    """The template has no mass_delete_actors key (the patcher schema defaults it
    to {}), so a lights-on seed must not gain an empty block."""
    t = _template()
    assert "mass_delete_actors" not in t
    assert "mass_delete_actors" not in merge_overrides(t, {})
    assert "mass_delete_actors" not in merge_overrides(t, {"light_patches": []})


_VENDOR_SCHEMA = (ROOT.parent / "vendor" / "open-dread-rando" / "src"
                  / "open_dread_rando" / "files" / "schema.json")


def test_light_patches_validate_against_upstream_schema():
    """Our emitted mass_delete_actors block must satisfy open-dread-rando's real
    schema (the patcher validates its input before touching the romfs)."""
    import json

    jsonschema = pytest.importorskip("jsonschema")
    if not _VENDOR_SCHEMA.is_file():
        pytest.skip("vendor/open-dread-rando submodule not checked out")

    schema = json.loads(_VENDOR_SCHEMA.read_text(encoding="utf-8"))
    sys.path.insert(0, str(ROOT.parent / "apworld"))
    from dread.patcher_pipeline import (  # noqa: E402
        LIGHT_REGION_TO_SCENARIO, light_patches_for_regions,
    )

    out = merge_overrides(_template(), {
        "light_patches": light_patches_for_regions(
            list(LIGHT_REGION_TO_SCENARIO)),   # every region dark
    })
    sub = dict(schema["properties"]["mass_delete_actors"])
    sub["$defs"] = schema["$defs"]
    jsonschema.validate(out["mass_delete_actors"], sub)


def test_light_patches_unknown_region_raises():
    """A region name the scenario table doesn't know must fail loudly rather than
    silently emitting no patch."""
    sys.path.insert(0, str(ROOT.parent / "apworld"))
    from dread.patcher_pipeline import light_patches_for_regions  # noqa: E402

    with pytest.raises(KeyError, match="norfair"):
        light_patches_for_regions(["norfair"])


def test_objective_required_artifacts_applied():
    t = _template()
    out = merge_overrides(t, {"required_artifacts": 7})
    assert out["objective"]["required_artifacts"] == 7
    # Stale per-guardian hints are replaced with a neutral, count-accurate,
    # non-spoiler line (the template's "guarded by Corpius" text is false under
    # AP placement).
    assert out["objective"]["hints"] == [
        "Recover {c1}7 Metroid DNA{c0} to complete your mission."
    ]


def test_objective_zero_artifacts_blanks_dna_hint():
    t = _template()
    out = merge_overrides(t, {"required_artifacts": 0})
    assert out["objective"]["required_artifacts"] == 0
    # No DNA required ⇒ the hint must not claim any DNA exist.
    assert out["objective"]["hints"] == ["Return to your ship to escape ZDR."]
    assert "Metroid DNA" not in out["objective"]["hints"][0]


def test_objective_absent_when_not_supplied():
    t = _template()
    out = merge_overrides(t, {})
    assert out["objective"]["required_artifacts"] == 3


def test_nav_station_hints_neutralized():
    """Top-level `hints[]` are the Nav Station entries the starter preset
    bakes against Randovania's own placement; under AP they're false.
    merge_overrides must replace each `text` with the AP-aware filler while
    preserving accesspoint_actor/hint_id so patch_hints still unlocks doors."""
    t = _template()
    t["hints"] = [
        {
            "accesspoint_actor": {"scenario": "s010_cave", "actor": "PRP_CV_AccessPoint002"},
            "hint_id": "CAVE_2",
            "text": ["A {c1}Progressive Beam{c0} can be found in {c5}Cataris{c0}."],
        },
        {
            "accesspoint_actor": {"scenario": "s020_magma", "actor": "accesspoint"},
            "hint_id": "MAGMA_1",
            "text": ["A {c1}Progressive Bomb{c0} can be found in {c5}Artaria{c0}."],
        },
    ]
    out = merge_overrides(t, {})
    assert len(out["hints"]) == 2
    expected_text = ["You're playing Archipelago! There's already a hint system!"]
    for src, patched in zip(t["hints"], out["hints"]):
        assert patched["accesspoint_actor"] == src["accesspoint_actor"]
        assert patched["hint_id"] == src["hint_id"]
        assert patched["text"] == expected_text


def test_nav_station_hints_filled_from_generated():
    """When the placements carry AP-generated `nav_hints`, merge_overrides
    bakes each one onto a Nav Station plaque (in order), preserving
    accesspoint_actor/hint_id. A plaque past the generated count falls back to
    the neutral filler so nothing keeps leaking Randovania's placement."""
    t = _template()
    t["hints"] = [
        {
            "accesspoint_actor": {"scenario": "s010_cave", "actor": "PRP_CV_AccessPoint002"},
            "hint_id": "CAVE_2",
            "text": ["A {c1}Progressive Beam{c0} can be found in {c5}Cataris{c0}."],
        },
        {
            "accesspoint_actor": {"scenario": "s020_magma", "actor": "accesspoint"},
            "hint_id": "MAGMA_1",
            "text": ["A {c1}Progressive Bomb{c0} can be found in {c5}Artaria{c0}."],
        },
    ]
    out = merge_overrides(t, {
        "nav_hints": [
            {"text": "Your {c1}Screw Attack{c0} is at {c5}Beebee{c0}'s {c5}Ship{c0}."},
        ],
    })
    assert len(out["hints"]) == 2
    # First plaque gets the generated hint; door-unlock metadata preserved.
    assert out["hints"][0]["accesspoint_actor"] == t["hints"][0]["accesspoint_actor"]
    assert out["hints"][0]["hint_id"] == "CAVE_2"
    assert out["hints"][0]["text"] == [
        "Your {c1}Screw Attack{c0} is at {c5}Beebee{c0}'s {c5}Ship{c0}."
    ]
    # Second plaque (no generated hint left) falls back to neutral filler.
    assert out["hints"][1]["text"] == [
        "You're playing Archipelago! There's already a hint system!"
    ]


def test_nav_station_hints_absent_round_trips():
    """Templates without top-level hints (e.g. the synthetic test template)
    must round-trip unchanged — the neutralizer only fires when entries exist."""
    t = _template()
    assert "hints" not in t
    out = merge_overrides(t, {})
    assert "hints" not in out


def test_spoiler_log_replaced_with_real_placements():
    """The template's baked example spoiler_log (rendered into the end
    credits' "Major Item Locations" by patch_credits) is replaced with the
    real AP placements when the overrides supply one."""
    t = _template()
    real = {"Grapple Beam": "Cataris: Kraid Arena",
            "Progressive Beam": "Artaria: Spot A\nSamusB's Cool Zone"}
    out = merge_overrides(t, {"spoiler_log": real})
    assert out["spoiler_log"] == real


def test_spoiler_log_empty_blanks_the_credits_section():
    """An explicitly empty spoiler_log (old payloads without the key) blanks
    the template's example log — patch_credits skips an empty dict, so no
    false placement ever reaches the credits."""
    t = _template()
    out = merge_overrides(t, {"spoiler_log": {}})
    assert out["spoiler_log"] == {}


def test_spoiler_log_absent_leaves_template_untouched():
    """No spoiler_log key at all (hand-written override files / template
    passthrough) keeps the template's log byte-identical."""
    t = _template()
    out = merge_overrides(t, {})
    assert out["spoiler_log"] == {"Grapple Beam": "Burenia - Teleport to Ferenia"}


def test_non_actor_pickup_resource_override():
    """A pickup with pickup_actor=None is keyed by its pickup_lua_callback
    (scenario/function), so AP-placed DNA lands on the boss."""
    t = _template()
    out = merge_overrides(t, {
        "pickup_resources": {
            "s010_cave/OnCorpiusDeath_CUSTOM": [[
                {"item_id": "ITEM_RANDO_ARTIFACT_5", "quantity": 1}
            ]],
        },
    })
    corpius = next(p for p in out["pickups"] if p["pickup_actor"] is None)
    assert corpius["resources"][0][0]["item_id"] == "ITEM_RANDO_ARTIFACT_5"


def test_does_not_mutate_template():
    t = _template()
    t_snapshot = repr(t)
    merge_overrides(t, {
        "layout_uuid": "11111111-2222-3333-4444-555555555555",
        "pickup_resources": {
            "s010_cave/ItemSphere_ChargeBeam": [[
                {"item_id": "ITEM_WEAPON_PLASMA_BEAM", "quantity": 1}
            ]],
        },
    })
    assert repr(t) == t_snapshot, "merge_overrides must not mutate its input"
