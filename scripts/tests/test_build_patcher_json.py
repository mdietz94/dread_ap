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
            },
            {
                "pickup_type": "actor",
                "caption": "Missile Tank acquired.",
                "resources": [[{"item_id": "ITEM_WEAPON_MISSILE_MAX", "quantity": 2}]],
                "pickup_actor": {"scenario": "s010_cave", "actor": "Item_MissileTank011"},
                "model": ["item_missiletank"],
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
                         "nerf_power_bombs": True},
        "objective": {"required_artifacts": 3, "hints": ["hint text"]},
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
            "raven_beak_damage_table_handling": "consistent_high",
            "nerf_power_bombs": False,
        },
    })
    ai = out["cosmetic_patches"]["config"]["AIManager"]
    assert ai["bShowEnemyLife"] is True
    # Untouched leaves keep template values.
    assert ai["bShowBossLifebar"] is True
    assert ai["bShowPlayerDamage"] is True
    assert out["cosmetic_patches"]["lua"]["custom_init"]["enable_room_name_display"] == "WITH_FADE"
    assert out["cosmetic_patches"]["lua"]["custom_init"]["enable_death_counter"] is True
    assert out["game_patches"]["raven_beak_damage_table_handling"] == "consistent_high"
    assert out["game_patches"]["nerf_power_bombs"] is False


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


def test_objective_required_artifacts_applied():
    t = _template()
    out = merge_overrides(t, {"required_artifacts": 7})
    assert out["objective"]["required_artifacts"] == 7
    # hints preserved
    assert out["objective"]["hints"] == ["hint text"]


def test_objective_absent_when_not_supplied():
    t = _template()
    out = merge_overrides(t, {})
    assert out["objective"]["required_artifacts"] == 3


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
