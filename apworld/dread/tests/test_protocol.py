"""Tests for protocol helpers (lua-table rendering, receive-pickup builder)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client.protocol import (  # noqa: E402
    _to_lua_table, build_receive_pickup_lua, DreadPickupLocation,
    pickup_class_for, build_kill_player_lua, build_read_death_count_lua,
    DEATH_COUNT_PROP, BOSS_ARENA_CAMERAS, build_boss_arenas_lua_table,
)


def test_build_kill_player_lua_calls_rl_killplayer():
    lua = build_kill_player_lua()
    assert "RL.KillPlayer()" in lua
    assert lua.strip().endswith("return ''")


def test_build_read_death_count_lua_reads_progress_stat():
    lua = build_read_death_count_lua()
    assert DEATH_COUNT_PROP == "ProgressStat_PlayerDeaths"
    assert DEATH_COUNT_PROP in lua
    assert lua.startswith("return tostring(")
    # `or 0` makes it safe at the main menu where the prop is absent.
    assert "or 0" in lua


def test_to_lua_table_scalars():
    assert _to_lua_table(1) == "1"
    assert _to_lua_table(1.5) == "1.5"
    assert _to_lua_table(True) == "true"
    assert _to_lua_table(False) == "false"
    assert _to_lua_table(None) == "nil"


def test_to_lua_table_string_escaping():
    assert _to_lua_table("hi") == '"hi"'
    assert _to_lua_table('quoted "thing"') == '"quoted \\"thing\\""'
    assert _to_lua_table("back\\slash") == '"back\\\\slash"'


def test_boss_arenas_lua_table_shape():
    lua = build_boss_arenas_lua_table()
    # Nested table keyed by scenario then collision camera, value true. Kraid's
    # arena (s020_magma / collision_camera_063) is the reported brick case.
    assert lua.startswith("{") and lua.endswith("}")
    assert "s020_magma={" in lua
    assert "collision_camera_063=true" in lua
    assert "s090_skybase={collision_camera_004=true}" in lua
    # No quotes — camera ids are emitted as barewords (valid Lua identifiers).
    assert '"' not in lua


def test_boss_arena_keys_are_lua_barewords():
    # Every scenario id and camera id must be a valid Lua identifier or the
    # rendered table is malformed; the renderer enforces this.
    import re as _re
    ident = _re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
    for scenario, cams in BOSS_ARENA_CAMERAS.items():
        assert ident.match(scenario), scenario
        for cam in cams:
            assert ident.match(cam), cam


def test_to_lua_table_list():
    assert _to_lua_table([1, 2, 3]) == "{1, 2, 3}"


def test_to_lua_table_dict():
    out = _to_lua_table({"item_id": "ITEM_X", "quantity": 2})
    # dict iteration order is insertion order in CPython 3.7+
    assert out == '{item_id="ITEM_X", quantity=2}'


def test_to_lua_table_nested_progression():
    progression = [[
        {"item_id": "ITEM_WEAPON_MISSILE_MAX", "quantity": 2}
    ]]
    out = _to_lua_table(progression)
    assert out == '{{{item_id="ITEM_WEAPON_MISSILE_MAX", quantity=2}}}'


def test_build_receive_pickup_lua_shape():
    # Delivers via the bootstrap's RL.ReceivePickup — the idempotent,
    # cutscene-safe path. The two trailing ints are the index match the Switch
    # checks against its live ReceivedPickups / InventoryIndex counters.
    lua = build_receive_pickup_lua(
        message="Received Missile Tank",
        progression=[[{"item_id": "ITEM_WEAPON_MISSILE_MAX", "quantity": 2}]],
        received_pickup_index=3,
        inventory_index=5,
    )
    assert lua.startswith("RL.ReceivePickup(")
    assert lua.endswith(", 3, 5)")              # received index, inventory index
    assert "RandomizerPowerup" in lua            # default pickup class (bareword)
    assert '"Received Missile Tank"' in lua      # message arg quoted
    assert "ITEM_WEAPON_MISSILE_MAX" in lua      # item id present
    # progression is passed as a Lua STRING (loadstring'd on the Switch), so its
    # inner quotes are escaped:
    assert '\\"ITEM_WEAPON_MISSILE_MAX\\"' in lua
    assert "quantity=2" in lua


def test_build_receive_pickup_lua_custom_class():
    lua = build_receive_pickup_lua(
        message="m", progression=[[{"item_id": "ITEM_SPEED_BOOSTER", "quantity": 1}]],
        received_pickup_index=0, inventory_index=0, cls="RandomizerSpeedBooster",
    )
    assert ", RandomizerSpeedBooster, " in lua


def test_pickup_location_key():
    p = DreadPickupLocation(scenario="s010_cave", actor="Item_MissileTank011")
    assert p.key == "s010_cave/Item_MissileTank011"


@pytest.mark.parametrize("patcher_item_id, expected_class", [
    # Items the user reported broken — every one needs its specific class.
    ("ITEM_WEAPON_WIDE_BEAM", "RandomizerWideBeam"),
    ("ITEM_WEAPON_PLASMA_BEAM", "RandomizerPlasmaBeam"),
    ("ITEM_SPEED_BOOSTER", "RandomizerSpeedBooster"),
    ("ITEM_WEAPON_ICE_MISSILE", "RandomizerIceMissile"),
    ("ITEM_MULTILOCKON", "RandomizerStormMissile"),
    ("ITEM_OPTIC_CAMOUFLAGE", "RandomizerPhantomCloak"),
    ("ITEM_GHOST_AURA", "RandomizerFlashShift"),
    # Flash Shift Upgrade falls through to RandomizerPowerup — RandomizerFlashShift
    # would zero its quantity once the player has Flash Shift.
    ("ITEM_UPGRADE_FLASH_SHIFT_CHAIN", "RandomizerPowerup"),
    # Upgrades / additive resources without a specific class.
    ("ITEM_UPGRADE_SPEED_BOOST_CHARGE", "RandomizerPowerup"),
    ("ITEM_WEAPON_CHARGE_BEAM", "RandomizerPowerup"),
    ("ITEM_WEAPON_GRAPPLE_BEAM", "RandomizerPowerup"),
    ("ITEM_WEAPON_POWER_BOMB_MAX", "RandomizerPowerup"),
    ("ITEM_WEAPON_POWER_BOMB", "RandomizerPowerBomb"),
    ("ITEM_LIFE_SHARDS", "RandomizerEnergyPart"),
    # Items the user reported working — fall through to the additive base class.
    ("ITEM_SPACE_JUMP", "RandomizerPowerup"),
    ("ITEM_VARIA_SUIT", "RandomizerPowerup"),
    ("ITEM_GRAVITY_SUIT", "RandomizerPowerup"),
    ("ITEM_MORPH_BALL", "RandomizerPowerup"),
    ("ITEM_WEAPON_MISSILE_MAX", "RandomizerPowerup"),
    ("ITEM_ENERGY_TANKS", "RandomizerPowerup"),
    ("ITEM_RANDO_ARTIFACT_1", "RandomizerPowerup"),
])
def test_pickup_class_for(patcher_item_id, expected_class):
    assert pickup_class_for(patcher_item_id) == expected_class


def test_pickup_resource_stage_power_bomb_grants_unlock_plus_capacity():
    """The Main Power Bomb must grant the unlock flag AND the ammo capacity;
    granting only ITEM_WEAPON_POWER_BOMB leaves the player at 0/0 (unusable,
    shows "?")."""
    from dread.client.protocol import pickup_resource_stage
    assert pickup_resource_stage("ITEM_WEAPON_POWER_BOMB", 2) == [
        {"item_id": "ITEM_WEAPON_POWER_BOMB", "quantity": 1},
        {"item_id": "ITEM_WEAPON_POWER_BOMB_MAX", "quantity": 2},
    ]


def test_pickup_resource_stage_passthrough_for_ordinary_items():
    from dread.client.protocol import pickup_resource_stage
    assert pickup_resource_stage("ITEM_WEAPON_MISSILE_MAX", 10) == [
        {"item_id": "ITEM_WEAPON_MISSILE_MAX", "quantity": 10},
    ]
