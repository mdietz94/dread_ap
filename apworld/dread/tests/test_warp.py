"""Tests for the ``/warp`` softlock-recovery command.

Covers the three client-side gates (no bridge / no bootstrap / connected),
the three Lua-side replies (``ok`` / ``not_ingame`` / ``no_interaction``),
and the transport-error fallbacks. The Lua source itself is asserted by
substring — exact whitespace isn't load-bearing, but the LoadScenario
call and the two guard reads must be present.
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client.datapackage import DataPackage  # noqa: E402
from dread.client.state import BridgeState  # noqa: E402
from dread.client.wire import Response  # noqa: E402

DATA = ROOT / "data"


@pytest.fixture
def ctx():
    from dread.client.context import DreadContext

    state = BridgeState()
    dp = DataPackage(apworld_data_dir=DATA)
    c = DreadContext(
        server_address=None,
        password=None,
        state=state,
        datapackage=dp,
        bridge_port=0,
        discovery_port=0,
    )
    return c


def _stub_bridge(ctx, *, connected: bool, response: Response | Exception):
    """Wire a minimal _bridge with is_connected() + run_lua() and mark
    bootstrap done. ``response`` is either a Response to return or an
    Exception to raise."""
    bridge = unittest.mock.MagicMock()
    bridge.is_connected.return_value = connected
    if isinstance(response, Exception):
        bridge.run_lua = unittest.mock.AsyncMock(side_effect=response)
    else:
        bridge.run_lua = unittest.mock.AsyncMock(return_value=response)
    ctx._bridge = bridge
    ctx._bootstrapped = True
    return bridge


@pytest.mark.asyncio
async def test_warp_no_bridge(ctx):
    msg = await ctx._warp_to_start()
    assert "no active Switch" in msg


@pytest.mark.asyncio
async def test_warp_bridge_not_connected(ctx):
    _stub_bridge(ctx, connected=False, response=Response(success=True, payload=b"ok"))
    msg = await ctx._warp_to_start()
    assert "no active Switch" in msg


@pytest.mark.asyncio
async def test_warp_bootstrap_pending(ctx):
    _stub_bridge(ctx, connected=True, response=Response(success=True, payload=b"ok"))
    ctx._bootstrapped = False
    msg = await ctx._warp_to_start()
    assert "bootstrap not complete" in msg


def _smart_bridge(ctx, *, inventory_before="", inventory_after="",
                  received="0", mode="INGAME"):
    """A bridge whose run_lua dispatches by Lua content, modelling the full
    warp+restore round-trip (snapshot → LoadScenario → re-read → restore).
    The first inventory read returns ``inventory_before``; later ones return
    ``inventory_after``."""
    reads: list[str] = []

    async def dispatch(src):
        if "RandomizerPowerup.GetItemAmount" in src:
            reads.append(src)
            body = inventory_before if len(reads) == 1 else inventory_after
            return Response(success=True, payload=body.encode())
        if "return tostring(RL.ReceivedPickups())" in src:
            return Response(success=True, payload=received.encode())
        if "Game.LoadScenario(" in src:
            return Response(success=True, payload=b"ok")
        if "Game.GetCurrentGameModeID" in src:
            return Response(success=True, payload=mode.encode())
        return Response(success=True, payload=b"")

    bridge = unittest.mock.MagicMock()
    bridge.is_connected.return_value = True
    bridge.run_lua = unittest.mock.AsyncMock(side_effect=dispatch)
    ctx._bridge = bridge
    ctx._bootstrapped = True
    return bridge


@pytest.mark.asyncio
async def test_warp_ok_restores_reverted_pickup(ctx):
    # A Speed Booster Upgrade (resource item) is in inventory before the warp
    # but gone after the LoadScenario reload → the restore must re-grant it.
    bridge = _smart_bridge(
        ctx, inventory_before="ITEM_ENERGY_TANKS=1", inventory_after="",
        received="0", mode="INGAME",
    )
    msg = await ctx._warp_to_start()
    assert "warped" in msg
    assert "restored" in msg
    srcs = [c.args[0] for c in bridge.run_lua.await_args_list]
    joined = "\n".join(srcs)
    # The warp primitive: matches Randovania's CheckWarpToStart, gated on
    # game-mode and user-interaction. A wrong scenario name (e.g. dropping
    # "c10_samus") would silently no-op.
    assert "Game.LoadScenario" in joined
    assert "Init.sStartingScenario" in joined
    assert "Init.sStartingActor" in joined
    assert 'Game.GetCurrentGameModeID() ~= "INGAME"' in joined
    assert "Scenario.IsUserInteractionEnabled(true)" in joined
    # The reverted item was re-granted via a direct OnPickedUp.
    assert any("ITEM_ENERGY_TANKS" in s and ".OnPickedUp(" in s for s in srcs)


@pytest.mark.asyncio
async def test_warp_ok_no_revert_is_a_noop(ctx):
    # Inventory identical before/after → nothing to restore.
    _smart_bridge(ctx, inventory_before="ITEM_ENERGY_TANKS=1",
                  inventory_after="ITEM_ENERGY_TANKS=1", mode="INGAME")
    msg = await ctx._warp_to_start()
    assert "warped" in msg
    assert "no pickups needed restoring" in msg


@pytest.mark.asyncio
async def test_warp_blocked_not_ingame(ctx):
    _stub_bridge(
        ctx, connected=True, response=Response(success=True, payload=b"not_ingame")
    )
    msg = await ctx._warp_to_start()
    assert "blocked" in msg
    assert "not in-game" in msg


@pytest.mark.asyncio
async def test_warp_blocked_in_boss_arena(ctx):
    bridge = _stub_bridge(
        ctx, connected=True, response=Response(success=True, payload=b"in_boss")
    )
    msg = await ctx._warp_to_start()
    assert "blocked" in msg
    assert "boss arena" in msg
    # The warp src must probe RL.IsInBossArena before LoadScenario, guarded so a
    # pre-bootstrap VM (function nil) degrades to allowing the warp.
    src = next(c.args[0] for c in bridge.run_lua.await_args_list
               if "Game.LoadScenario" in c.args[0])
    assert "RL.IsInBossArena and RL.IsInBossArena()" in src
    assert src.index("RL.IsInBossArena") < src.index("Game.LoadScenario")


@pytest.mark.asyncio
async def test_warp_blocked_in_nav_room(ctx):
    bridge = _stub_bridge(
        ctx, connected=True, response=Response(success=True, payload=b"in_nav")
    )
    msg = await ctx._warp_to_start()
    assert "blocked" in msg
    assert "Navigation" in msg
    # The warp src must probe RL.IsInNavRoom before LoadScenario, guarded so a
    # pre-bootstrap VM (function nil) degrades to allowing the warp.
    src = next(c.args[0] for c in bridge.run_lua.await_args_list
               if "Game.LoadScenario" in c.args[0])
    assert "RL.IsInNavRoom and RL.IsInNavRoom()" in src
    assert src.index("RL.IsInNavRoom") < src.index("Game.LoadScenario")
    # Boss check precedes the nav check (both before the LoadScenario).
    assert src.index("RL.IsInBossArena") < src.index("RL.IsInNavRoom")


@pytest.mark.asyncio
async def test_warp_blocked_in_save_room(ctx):
    bridge = _stub_bridge(
        ctx, connected=True, response=Response(success=True, payload=b"in_save")
    )
    msg = await ctx._warp_to_start()
    assert "blocked" in msg
    assert "save station" in msg
    src = next(c.args[0] for c in bridge.run_lua.await_args_list
               if "Game.LoadScenario" in c.args[0])
    assert "RL.IsInSaveRoom and RL.IsInSaveRoom()" in src
    assert src.index("RL.IsInSaveRoom") < src.index("Game.LoadScenario")
    # Order: boss → nav → save, all before the interaction/cutscene check.
    assert src.index("RL.IsInNavRoom") < src.index("RL.IsInSaveRoom")
    assert src.index("RL.IsInSaveRoom") < src.index("IsUserInteractionEnabled")


@pytest.mark.asyncio
async def test_warp_blocked_cutscene(ctx):
    _stub_bridge(
        ctx, connected=True, response=Response(success=True, payload=b"no_interaction")
    )
    msg = await ctx._warp_to_start()
    assert "blocked" in msg
    assert "cutscene" in msg


@pytest.mark.asyncio
async def test_warp_lua_error_surfaces(ctx):
    _stub_bridge(
        ctx, connected=True, response=Response(success=False, payload=b"syntax error near X")
    )
    msg = await ctx._warp_to_start()
    assert "lua error" in msg


@pytest.mark.asyncio
async def test_warp_timeout_surfaces(ctx):
    _stub_bridge(ctx, connected=True, response=asyncio.TimeoutError("waited too long"))
    msg = await ctx._warp_to_start()
    assert "failed" in msg


@pytest.mark.asyncio
async def test_warp_connection_error_surfaces(ctx):
    _stub_bridge(ctx, connected=True, response=ConnectionError("peer reset"))
    msg = await ctx._warp_to_start()
    assert "failed" in msg


@pytest.mark.asyncio
async def test_warp_unknown_payload(ctx):
    _stub_bridge(
        ctx, connected=True, response=Response(success=True, payload=b"surprise")
    )
    msg = await ctx._warp_to_start()
    assert "unexpected" in msg


# ---- /warp <region> [station] (per-station recovery) ----------------------

# (scenario, camera) keys for real Dairon stations (save / nav / map).
_DAIRON_EAST = ("s030_baselab", "collision_camera_000")       # save East
_DAIRON_WEST = ("s030_baselab", "collision_camera_032")       # save West
_DAIRON_NAV_NORTH = ("s030_baselab", "collision_camera_044")  # nav North
_DAIRON_NAV_SOUTH = ("s030_baselab", "collision_camera_014")  # nav South
_DAIRON_MAP = ("s030_baselab", "collision_camera_023")        # map station


@pytest.mark.asyncio
async def test_warp_region_unknown(ctx):
    _smart_bridge(ctx, mode="INGAME")
    msg = await ctx._warp_to_region("atlantis")
    assert "unknown region" in msg
    assert "cataris" in msg  # lists the known regions


@pytest.mark.asyncio
async def test_warp_region_never_visited_refused(ctx):
    bridge = _smart_bridge(ctx, mode="INGAME")
    ctx._visited_scenarios = set()
    ctx._visited_saves = set()
    msg = await ctx._warp_to_region("dairon")
    assert "haven't been to Dairon" in msg
    assert not any("Game.LoadScenario" in c.args[0]
                   for c in bridge.run_lua.await_args_list)


@pytest.mark.asyncio
async def test_warp_region_visited_but_no_save_station(ctx):
    _smart_bridge(ctx, mode="INGAME")
    ctx._visited_scenarios = {"s030_baselab"}  # been to Dairon...
    ctx._visited_saves = set()                 # ...but not at a station
    msg = await ctx._warp_to_region("dairon")
    assert "haven't seen you at a save, nav, or map station" in msg


@pytest.mark.asyncio
async def test_warp_station_visited_loads_that_scenario(ctx):
    bridge = _smart_bridge(ctx, inventory_before="ITEM_MISSILE_TANKS=3",
                           inventory_after="ITEM_MISSILE_TANKS=3", mode="INGAME")
    ctx._visited_saves = {_DAIRON_EAST}
    msg = await ctx._warp_to_region("Dairon")  # case-insensitive, single visited
    assert "warped" in msg
    src = next(c.args[0] for c in bridge.run_lua.await_args_list
               if "Game.LoadScenario" in c.args[0])
    assert '"s030_baselab"' in src
    assert '"savestation_000_platform"' in src   # the East platform
    assert "Init.sStartingScenario" not in src


@pytest.mark.asyncio
async def test_warp_station_ambiguous_lists_options(ctx):
    bridge = _smart_bridge(ctx, mode="INGAME")
    ctx._visited_saves = {_DAIRON_EAST, _DAIRON_WEST}
    msg = await ctx._warp_to_region("dairon")  # no station -> ambiguous
    assert "multiple" in msg and "East" in msg and "West" in msg
    assert not any("Game.LoadScenario" in c.args[0]
                   for c in bridge.run_lua.await_args_list)


@pytest.mark.asyncio
async def test_warp_station_keyword_disambiguates(ctx):
    bridge = _smart_bridge(ctx, inventory_before="", inventory_after="",
                           mode="INGAME")
    ctx._visited_saves = {_DAIRON_EAST, _DAIRON_WEST}
    msg = await ctx._warp_to_region("dairon", "west")
    assert "warped" in msg
    src = next(c.args[0] for c in bridge.run_lua.await_args_list
               if "Game.LoadScenario" in c.args[0])
    assert '"savestation_001_platform"' in src   # the West platform


@pytest.mark.asyncio
async def test_warp_station_keyword_no_match(ctx):
    _smart_bridge(ctx, mode="INGAME")
    ctx._visited_saves = {_DAIRON_EAST}
    msg = await ctx._warp_to_region("dairon", "north")
    assert "no visited" in msg and "Save East" in msg


# ---- nav + map stations as warp targets -----------------------------------

# Artaria has both a Save North and a Nav North, so it exercises kind ambiguity.
_ARTARIA_SAVE_NORTH = ("s010_cave", "collision_camera_076")
_ARTARIA_NAV_NORTH = ("s010_cave", "collision_camera_065")


@pytest.mark.asyncio
async def test_warp_nav_station_loads_spawn(ctx):
    bridge = _smart_bridge(ctx, mode="INGAME")
    ctx._visited_saves = {_DAIRON_NAV_NORTH}
    msg = await ctx._warp_to_region("dairon", "nav north")
    assert "warped" in msg
    src = next(c.args[0] for c in bridge.run_lua.await_args_list
               if "Game.LoadScenario" in c.args[0])
    assert '"s030_baselab"' in src
    assert '"accesspoint_001_platform"' in src   # Dairon Nav North access point


@pytest.mark.asyncio
async def test_warp_map_station_loads_spawn(ctx):
    bridge = _smart_bridge(ctx, mode="INGAME")
    ctx._visited_saves = {_DAIRON_MAP}
    msg = await ctx._warp_to_region("dairon", "map")  # single token, unambiguous
    assert "warped" in msg
    src = next(c.args[0] for c in bridge.run_lua.await_args_list
               if "Game.LoadScenario" in c.args[0])
    assert '"maproom_000_platform"' in src          # Dairon Map Station platform


@pytest.mark.asyncio
async def test_warp_kind_keyword_disambiguates_save_vs_nav(ctx):
    # Both a Save North and a Nav North are visited in Artaria. A bare "north" is
    # ambiguous; the kind keyword picks one.
    bridge = _smart_bridge(ctx, mode="INGAME")
    ctx._visited_saves = {_ARTARIA_SAVE_NORTH, _ARTARIA_NAV_NORTH}
    msg = await ctx._warp_to_region("artaria", "north")
    assert "ambiguous" in msg and "Save North" in msg and "Nav North" in msg

    msg = await ctx._warp_to_region("artaria", "nav north")
    assert "warped" in msg
    src = next(c.args[0] for c in bridge.run_lua.await_args_list
               if "Game.LoadScenario" in c.args[0])
    assert '"PRP_CV_AccessPoint002_WeightPlate"' in src  # the Nav North platform


@pytest.mark.asyncio
async def test_warp_hanubia_reachable_via_nav_only(ctx):
    # Hanubia has no save station; its nav station is the only /warp target.
    bridge = _smart_bridge(ctx, mode="INGAME")
    ctx._visited_saves = {("s080_shipyard", "collision_camera_003")}
    msg = await ctx._warp_to_region("hanubia")  # single visited -> no keyword needed
    assert "warped" in msg
    src = next(c.args[0] for c in bridge.run_lua.await_args_list
               if "Game.LoadScenario" in c.args[0])
    assert '"s080_shipyard"' in src
    assert '"weightactivatedplatform_access_000"' in src


@pytest.mark.asyncio
async def test_record_nav_and_map_visits(ctx):
    # A poll read at a nav or map camera records it as a warp target.
    bridge = unittest.mock.MagicMock()
    bridge.is_connected.return_value = True
    bridge.run_lua = unittest.mock.AsyncMock(
        return_value=Response(success=True, payload=b"s030_baselab,collision_camera_044"))
    ctx._bridge = bridge
    await ctx._record_room_visit(1.0)
    assert _DAIRON_NAV_NORTH in ctx._visited_saves

    bridge.run_lua = unittest.mock.AsyncMock(
        return_value=Response(success=True, payload=b"s030_baselab,collision_camera_023"))
    await ctx._record_room_visit(1.0)
    assert _DAIRON_MAP in ctx._visited_saves


@pytest.mark.asyncio
async def test_warp_blocked_in_map_room(ctx):
    _stub_bridge(ctx, connected=True, response=Response(success=True, payload=b"in_map"))
    msg = await ctx._warp_to_start()
    assert "blocked" in msg and "map station" in msg


def test_warp_list(ctx):
    ctx._visited_saves = {_DAIRON_EAST, ("s020_magma", "collision_camera_062")}
    msg = ctx._warp_list()
    assert "Cataris Save West" in msg and "Dairon Save East" in msg


def test_warp_list_includes_nav_and_map(ctx):
    # A nav station and a map station the player has stood in show up, labelled.
    ctx._visited_saves = {_DAIRON_NAV_NORTH, _DAIRON_MAP}
    msg = ctx._warp_list()
    assert "Dairon Nav North" in msg and "Dairon Map" in msg


def test_warp_list_empty(ctx):
    ctx._visited_saves = set()
    assert "no stations recorded" in ctx._warp_list()


@pytest.mark.asyncio
async def test_record_save_station_visit(ctx):
    # A poll read reporting the player at the Dairon East save-station camera
    # records it; a non-save subarea does not.
    bridge = unittest.mock.MagicMock()
    bridge.is_connected.return_value = True
    bridge.run_lua = unittest.mock.AsyncMock(
        return_value=Response(success=True, payload=b"s030_baselab,collision_camera_000"))
    ctx._bridge = bridge
    await ctx._record_room_visit(1.0)
    assert _DAIRON_EAST in ctx._visited_saves

    bridge.run_lua = unittest.mock.AsyncMock(
        return_value=Response(success=True, payload=b"s030_baselab,collision_camera_099"))
    await ctx._record_room_visit(1.0)
    assert ("s030_baselab", "collision_camera_099") not in ctx._visited_saves


@pytest.mark.asyncio
async def test_warp_station_keeps_current_location_guards(ctx):
    # A station warp is still blocked from a boss arena (guard is location-based).
    _stub_bridge(ctx, connected=True, response=Response(success=True, payload=b"in_boss"))
    ctx._visited_saves = {_DAIRON_EAST}
    msg = await ctx._warp_to_region("dairon")
    assert "blocked" in msg and "boss arena" in msg


# ---- Nav Station hint registration (visit -> free AP server hint) ----------

from dread.client.protocol import NAV_HINT_STATIONS  # noqa: E402


def _subarea_bridge(ctx, scenario: str, camera: str):
    """A bridge whose subarea read reports the player at (scenario, camera)."""
    bridge = unittest.mock.MagicMock()
    bridge.is_connected.return_value = True
    bridge.run_lua = unittest.mock.AsyncMock(
        return_value=Response(success=True,
                              payload=f"{scenario},{camera}".encode()))
    ctx._bridge = bridge
    return bridge


def _capture_msgs(ctx) -> list:
    sent: list = []
    ctx.send_msgs = unittest.mock.AsyncMock(side_effect=lambda m: sent.extend(m))
    return sent


def test_build_nav_hint_map_aligns_to_stations(ctx):
    ctx._build_nav_hint_map({"nav_hints": [
        {"text": "a", "loc": [1, 5000]},   # plaque 0 -> station 0
        {"text": "b", "loc": [2, 6000]},   # plaque 1 -> station 1
        {"text": "filler"},                # plaque 2 -> no loc -> skipped
    ]})
    assert ctx._nav_hint_by_camera[NAV_HINT_STATIONS[0]] == [(1, 5000)]
    assert ctx._nav_hint_by_camera[NAV_HINT_STATIONS[1]] == [(2, 6000)]
    assert NAV_HINT_STATIONS[2] not in ctx._nav_hint_by_camera


@pytest.mark.asyncio
async def test_nav_visit_registers_own_location_via_scouts(ctx):
    ctx.slot = 1
    ctx._nav_hint_by_camera = {NAV_HINT_STATIONS[0]: [(1, 5000)]}
    sent = _capture_msgs(ctx)
    _subarea_bridge(ctx, *NAV_HINT_STATIONS[0])
    await ctx._record_room_visit(1.0)
    # Own location -> free LocationScouts create_as_hint.
    assert sent == [{"cmd": "LocationScouts", "locations": [5000],
                     "create_as_hint": 2}]
    assert (1, 5000) in ctx._registered_nav_hints
    # Re-visit must not re-send.
    sent.clear()
    await ctx._record_room_visit(1.0)
    assert sent == []


@pytest.mark.asyncio
async def test_nav_visit_registers_cross_world_via_createhints(ctx):
    ctx.slot = 1
    ctx._nav_hint_by_camera = {NAV_HINT_STATIONS[1]: [(2, 6000)]}
    sent = _capture_msgs(ctx)
    _subarea_bridge(ctx, *NAV_HINT_STATIONS[1])
    await ctx._record_room_visit(1.0)
    # Another world's location (our item there) -> free CreateHints.
    assert sent == [{"cmd": "CreateHints", "player": 2, "locations": [6000]}]


@pytest.mark.asyncio
async def test_nav_visit_elsewhere_registers_nothing(ctx):
    ctx.slot = 1
    ctx._nav_hint_by_camera = {NAV_HINT_STATIONS[0]: [(1, 5000)]}
    sent = _capture_msgs(ctx)
    _subarea_bridge(ctx, "s050_forest", "collision_camera_999")  # not a nav plaque
    await ctx._record_room_visit(1.0)
    assert sent == []
    assert not ctx._registered_nav_hints


# ---- visited set persists in AP DataStorage (survives client restart) ------

_KEY = "dread_warp_visited_SEED_0_1"


def _set_for(sent, key):
    """The replace-value of the Set targeting ``key`` in captured msgs, or None."""
    for m in sent:
        if m.get("cmd") == "Set" and m.get("key") == key:
            return m["operations"][0]["value"]
    return None


@pytest.mark.asyncio
async def test_recording_a_visit_persists_to_storage(ctx):
    # With a storage key bound, recording a station writes the full set via Set.
    ctx._warp_visited_key = _KEY
    sent = _capture_msgs(ctx)
    bridge = unittest.mock.MagicMock()
    bridge.is_connected.return_value = True
    bridge.run_lua = unittest.mock.AsyncMock(
        return_value=Response(success=True, payload=b"s030_baselab,collision_camera_000"))
    ctx._bridge = bridge
    await ctx._record_room_visit(1.0)
    value = _set_for(sent, _KEY)
    assert value == [["s030_baselab", "collision_camera_000"]]


@pytest.mark.asyncio
async def test_persist_is_noop_without_key(ctx):
    ctx._warp_visited_key = None
    sent = _capture_msgs(ctx)
    ctx._visited_saves = {_DAIRON_EAST}
    await ctx._persist_visited()
    assert sent == []


def test_retrieved_restores_visited_set(ctx):
    # A restarted client starts empty; the Get response repopulates the set.
    ctx._warp_visited_key = _KEY
    ctx._visited_saves = set()
    pushed = ctx._absorb_visited_storage(
        {"keys": {_KEY: [["s030_baselab", "collision_camera_000"],
                         ["s080_shipyard", "collision_camera_003"]]}})
    assert _DAIRON_EAST in ctx._visited_saves
    assert ("s080_shipyard", "collision_camera_003") in ctx._visited_saves
    # Nothing local-only -> no push-back warranted.
    assert pushed is False


def test_setreply_merges_other_client_visit(ctx):
    ctx._warp_visited_key = _KEY
    ctx._visited_saves = set()
    ctx._absorb_visited_storage(
        {"key": _KEY, "value": [["s030_baselab", "collision_camera_044"]]})
    assert _DAIRON_NAV_NORTH in ctx._visited_saves


def test_absorb_ignores_unrelated_key_and_garbage(ctx):
    ctx._warp_visited_key = _KEY
    ctx._visited_saves = set()
    # Wrong key -> ignored.
    assert ctx._absorb_visited_storage({"keys": {"other": [["s030_baselab", "x"]]}}) is False
    # Unknown / malformed pairs are dropped, not added.
    ctx._absorb_visited_storage(
        {"keys": {_KEY: [["s030_baselab", "collision_camera_999"], ["bad"], 42]}})
    assert ctx._visited_saves == set()


def test_absorb_flags_local_only_entries_for_pushback(ctx):
    # Observed a station while AP was disconnected; on reconnect the stored value
    # lacks it, so a push-back is warranted.
    ctx._warp_visited_key = _KEY
    ctx._visited_saves = {_DAIRON_EAST}
    pushed = ctx._absorb_visited_storage({"keys": {_KEY: []}})
    assert pushed is True


@pytest.mark.asyncio
async def test_restart_roundtrip_then_warp(ctx):
    # End-to-end: persist on visit, then a fresh client restores from storage and
    # can warp there without re-visiting.
    ctx._warp_visited_key = _KEY
    sent = _capture_msgs(ctx)
    bridge = unittest.mock.MagicMock()
    bridge.is_connected.return_value = True
    bridge.run_lua = unittest.mock.AsyncMock(
        return_value=Response(success=True, payload=b"s080_shipyard,collision_camera_003"))
    ctx._bridge = bridge
    await ctx._record_room_visit(1.0)            # Hanubia nav station
    stored = _set_for(sent, _KEY)

    fresh = ctx.__class__.__new__(ctx.__class__)  # a "restarted" context, no shared state
    fresh._visited_saves = set()
    fresh._warp_visited_key = _KEY
    fresh._absorb_visited_storage({"keys": {_KEY: stored}})
    assert ("s080_shipyard", "collision_camera_003") in fresh._visited_saves
