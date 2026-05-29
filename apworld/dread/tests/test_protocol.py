"""Tests for protocol helpers (lua-table rendering, receive-pickup builder)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client.protocol import (  # noqa: E402
    _to_lua_table, build_receive_pickup_lua, DreadPickupLocation,
)


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
