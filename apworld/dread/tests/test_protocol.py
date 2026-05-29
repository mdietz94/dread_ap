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
    # Now emits RandomizerPowerup.OnPickedUp(nil, ...) — RL.ReceivePickup
    # was the old upstream API surface; current open-dread-rando-exlaunch
    # doesn't define it. See build_receive_pickup_lua's docstring.
    lua = build_receive_pickup_lua(
        message="Received Missile Tank",
        parent_ref=0,
        progression=[[{"item_id": "ITEM_WEAPON_MISSILE_MAX", "quantity": 2}]],
        num_pickups=1,
        inventory_index=0,
    )
    assert lua.startswith("do ")
    assert lua.endswith(" end")
    assert "RandomizerPowerup.OnPickedUp(nil, " in lua
    assert "Game.LogWarn" in lua
    assert '"Received Missile Tank"' in lua  # message arg quoted
    assert "ITEM_WEAPON_MISSILE_MAX" in lua  # item id present
    assert "quantity=2" in lua  # Lua key=value, not JSON


def test_pickup_location_key():
    p = DreadPickupLocation(scenario="s010_cave", actor="Item_MissileTank011")
    assert p.key == "s010_cave/Item_MissileTank011"
