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
    NAV_ROOM_CAMERAS, build_nav_rooms_lua_table,
    SAVE_STATION_CAMERAS, build_save_rooms_lua_table,
    MAP_STATION_CAMERAS, build_map_rooms_lua_table,
    build_warp_src, WARP_TARGET_BY_CAMERA, WARP_TARGETS_BY_REGION,
    NAV_STATIONS, MAP_STATIONS,
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


def test_nav_rooms_lua_table_shape():
    lua = build_nav_rooms_lua_table()
    # Same nested-table shape as the boss table. s010_cave's North Navigation
    # Station (collision_camera_065) is a representative Adam room.
    assert lua.startswith("{") and lua.endswith("}")
    assert "s010_cave={" in lua
    assert "collision_camera_065=true" in lua
    # Camera ids are emitted as barewords — no quotes.
    assert '"' not in lua


def test_nav_room_keys_are_lua_barewords():
    import re as _re
    ident = _re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
    for scenario, cams in NAV_ROOM_CAMERAS.items():
        assert ident.match(scenario), scenario
        for cam in cams:
            assert ident.match(cam), cam


def test_save_rooms_lua_table_shape():
    lua = build_save_rooms_lua_table()
    # Same nested-table shape; s010_cave's West Save Station (collision_camera_012).
    assert lua.startswith("{") and lua.endswith("}")
    assert "s010_cave={" in lua
    assert "collision_camera_012=true" in lua
    assert '"' not in lua


def test_save_room_keys_are_lua_barewords():
    import re as _re
    ident = _re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
    for scenario, cams in SAVE_STATION_CAMERAS.items():
        assert ident.match(scenario), scenario
        for cam in cams:
            assert ident.match(cam), cam


def test_map_rooms_lua_table_shape():
    lua = build_map_rooms_lua_table()
    # Same nested-table shape; s020_magma's Map Station (collision_camera_030).
    assert lua.startswith("{") and lua.endswith("}")
    assert "s020_magma={" in lua
    assert "collision_camera_030=true" in lua
    assert '"' not in lua


def test_map_room_keys_are_lua_barewords():
    import re as _re
    ident = _re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")
    for scenario, cams in MAP_STATION_CAMERAS.items():
        assert ident.match(scenario), scenario
        for cam in cams:
            assert ident.match(cam), cam


def test_warp_block_tables_disjoint_per_scenario():
    # Within a single scenario, the four no-warp camera sets must not overlap, or
    # the guards would contradict / a room would be misclassified. (The SAME camera
    # id in DIFFERENT scenarios is fine — every guard keys on the live scenario.)
    tables = {
        "boss": BOSS_ARENA_CAMERAS,
        "nav": NAV_ROOM_CAMERAS,
        "save": SAVE_STATION_CAMERAS,
        "map": MAP_STATION_CAMERAS,
    }
    scenarios = set().union(*(t.keys() for t in tables.values()))
    for scenario in scenarios:
        sets = {name: set(t.get(scenario, {})) for name, t in tables.items()}
        names = list(tables)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                overlap = sets[a] & sets[b]
                assert not overlap, (scenario, a, b, overlap)


def test_warp_src_guards_in_map_room():
    # The /warp src refuses while standing in a map station (alongside boss / nav
    # / save), so warping out can't strand the map console overlay.
    src = build_warp_src("Init.sStartingScenario", "Init.sStartingActor")
    assert 'RL.IsInMapRoom and RL.IsInMapRoom() then return "in_map"' in src
    assert 'then return "in_save"' in src  # ordering: the other guards survive


def test_warp_targets_include_nav_and_map():
    # Nav + map stations are now /warp targets, keyed by (scenario, camera).
    # Hanubia (s080_shipyard) gains its only target via the nav station.
    nav = WARP_TARGET_BY_CAMERA[("s080_shipyard", "collision_camera_003")]
    assert nav.kind == "nav" and nav.region_key == "hanubia"
    assert nav.spawn == "weightactivatedplatform_access_000"
    mp = WARP_TARGET_BY_CAMERA[("s020_magma", "collision_camera_030")]
    assert mp.kind == "map" and mp.label == "Map" and mp.spawn == "maproom_platform"
    # Hanubia is reachable as a region only through its nav station.
    assert {t.kind for t in WARP_TARGETS_BY_REGION["hanubia"]} == {"nav"}
    # Every nav/map target carries a real spawn actor and a unique camera key.
    assert all(t.spawn for t in NAV_STATIONS + MAP_STATIONS)


def test_warp_target_labels_distinguish_kinds_in_a_region():
    # Artaria has both a "Save North" and a "Nav North"; labels keep them apart so
    # /warp can disambiguate by kind keyword.
    labels = {t.label for t in WARP_TARGETS_BY_REGION["artaria"]}
    assert {"Save North", "Nav North", "Map"} <= labels


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


def test_build_receive_pickup_lua_default_is_five_args():
    # No popup/reschedule overrides → the lone-item 5-arg call (bootstrap
    # defaults to 7.0s / 7.5s). This keeps normal single-item receipt unchanged.
    lua = build_receive_pickup_lua(
        message="m", progression=[[{"item_id": "ITEM_X", "quantity": 1}]],
        received_pickup_index=2, inventory_index=4,
    )
    assert lua.endswith(", 2, 4)")


def test_build_receive_pickup_lua_burst_emits_timing_overrides():
    # During a release the client passes short popup/reschedule seconds so the
    # backlog drains fast instead of one item every ~7.5s.
    lua = build_receive_pickup_lua(
        message="m", progression=[[{"item_id": "ITEM_X", "quantity": 1}]],
        received_pickup_index=2, inventory_index=4,
        popup_seconds=1.5, reschedule_seconds=0.3,
    )
    assert lua.endswith(", 2, 4, 1.5, 0.3)")


def test_build_receive_pickup_lua_partial_override_fills_default():
    # A single override still produces a valid 7-arg call, defaulting the other
    # to the bootstrap's lone-item constant.
    lua = build_receive_pickup_lua(
        message="m", progression=[[{"item_id": "ITEM_X", "quantity": 1}]],
        received_pickup_index=0, inventory_index=0,
        reschedule_seconds=0.3,
    )
    assert lua.endswith(", 0, 0, 7, 0.3)")


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


def test_pickup_resource_stage_main_flash_shift_bundles_included_ammo():
    """The main Flash Shift grants the ability flag plus its bundled
    `included_ammo` chained dashes (quantity = FlashUpgrade count) in one stage,
    so the main alone reproduces vanilla chaining. Quantity 0 → ability only."""
    from dread.client.protocol import pickup_resource_stage
    assert pickup_resource_stage("ITEM_GHOST_AURA", 2) == [
        {"item_id": "ITEM_GHOST_AURA", "quantity": 1},
        {"item_id": "ITEM_UPGRADE_FLASH_SHIFT_CHAIN", "quantity": 2},
    ]
    assert pickup_resource_stage("ITEM_GHOST_AURA", 0) == [
        {"item_id": "ITEM_GHOST_AURA", "quantity": 1},
    ]


def test_pickup_resource_stage_main_speed_booster_is_single_resource():
    """The main Speed Booster grants only the ability flag — Randovania's Speed
    Booster major includes no charge upgrades (the Speed Booster Upgrade is a
    standalone standard pickup), so the main stays a single resource."""
    from dread.client.protocol import pickup_resource_stage
    assert pickup_resource_stage("ITEM_SPEED_BOOSTER", 1) == [
        {"item_id": "ITEM_SPEED_BOOSTER", "quantity": 1},
    ]
