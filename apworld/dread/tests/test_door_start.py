"""Door-lock rando + more-starting-areas tests.

Unit tests (no AP runtime) on the per-seed assignment / spawn logic, plus gated
real-generation tests that the features produce solvable seeds via the REAL
DreadWorld path (and compose). Both ride the native graph; see graph_logic.py.
"""
from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

GRAPH_PATH = ROOT / "data" / "logic_graph.json"
graph_required = pytest.mark.skipif(
    not GRAPH_PATH.exists(),
    reason="logic_graph.json not materialized",
)


@pytest.fixture(scope="module")
def graph():
    return json.loads(GRAPH_PATH.read_text())


class _RNG:
    """Deterministic stand-in for world.random (just needs .choice)."""
    def __init__(self, seq=0):
        self.seq = seq

    def choice(self, seq):
        self.seq = (self.seq + 1) % len(seq)
        return seq[self.seq]


# ---- door rando unit tests -----------------------------------------------

def test_assignments_to_door_patches_skips_null_actor():
    """Dock sides whose patcher actor is None must be silently skipped."""
    from dread.DoorRando import assignments_to_door_patches
    graph = {
        "dock_sides": {
            "A": {"patcher": {"scenario": "s010_cave", "actor": "Door001"},
                  "dock_type": "door", "default_weakness": "Power Beam Door"},
            "B": {"patcher": {"scenario": "s010_cave", "actor": None},
                  "dock_type": "door", "default_weakness": "Power Beam Door"},
        },
        "door_rando": {
            "weakness_door_type": {"Wave Beam Door": "doorWave"},
        },
    }
    assign = {"A": "Wave Beam Door", "B": "Wave Beam Door"}
    patches = assignments_to_door_patches(assign, graph)
    assert len(patches) == 1
    assert patches[0]["actor"]["actor"] == "Door001"


def test_roll_assignments_drops_exotic_door_types():
    """The pool is restricted to BASIC_DOOR_TYPES; exotic shields never roll."""
    from dread.DoorRando import roll_assignments, BASIC_DOOR_TYPES
    wdt = {"Wave Beam Door": "wave_beam",      # basic (shielded)
           "Power Beam Door": "power_beam",    # basic (unshielded)
           "Bomb Door": "bomb",                # exotic — must be excluded
           "Power Bomb Door": "power_bomb"}    # exotic — must be excluded
    graph = {
        "dock_sides": {
            "A": {"patcher": {"scenario": "s010_cave", "actor": "Door001"},
                  "dock_type": "door", "default_weakness": "Power Beam Door",
                  "paired_side_id": "B"},
            "B": {"patcher": {"scenario": "s010_cave", "actor": "Door001"},
                  "dock_type": "door", "default_weakness": "Power Beam Door",
                  "paired_side_id": "A"},
        },
        "door_rando": {
            "change_to": list(wdt),
            "weakness_door_type": wdt,
            "locked_weakness": "Access Permanently Closed",
            "vanilla_shield_ids": {},
        },
    }
    for seed in range(20):
        assign = roll_assignments(graph, random.Random(seed), mode="randomized")
        for w in assign.values():
            assert wdt[w] in BASIC_DOOR_TYPES, f"exotic type leaked: {w}"


class _FirstRNG:
    """choice() always returns the first option — surfaces a filtering failure
    (a shielded target placed first would be picked if the budget didn't drop it)."""
    def choice(self, seq):
        return seq[0]


def test_roll_assignments_respects_shield_budget():
    """A scenario whose vanilla shield baseline sits at the cap gets only
    unshielded targets for its unshielded doors (never a new shield)."""
    from dread.DoorRando import (roll_assignments, SHIELD_IDS_PER_SCENARIO,
                                 SHIELD_BUDGET_MARGIN)
    wdt = {"Wave Beam Door": "wave_beam",    # shielded (first in change_to)
           "Power Beam Door": "power_beam"}  # unshielded fallback
    cap_doors = (SHIELD_IDS_PER_SCENARIO - SHIELD_BUDGET_MARGIN) // 2  # baseline at cap
    graph = {
        "dock_sides": {
            "A": {"patcher": {"scenario": "s010_cave", "actor": "Door001"},
                  "dock_type": "door", "default_weakness": "Power Beam Door",
                  "paired_side_id": "B"},
            "B": {"patcher": {"scenario": "s010_cave", "actor": "Door001"},
                  "dock_type": "door", "default_weakness": "Power Beam Door",
                  "paired_side_id": "A"},
        },
        "door_rando": {
            "change_to": list(wdt),
            "weakness_door_type": wdt,
            "locked_weakness": "Access Permanently Closed",
            "vanilla_shield_ids": {"s010_cave": cap_doors},
        },
    }
    assign = roll_assignments(graph, _FirstRNG(), mode="randomized")
    assert assign["A"] == "Power Beam Door", "budget should refuse a new shield"


@graph_required
def test_roll_assignments_two_sided(graph):
    """Paired door sides always receive the same weakness."""
    from dread.DoorRando import roll_assignments, _physical_doors
    assign = roll_assignments(graph, _RNG(),
                              starting_items={"Slide": 1, "Pulse Radar": 1,
                                              "Missile Tank": 1},
                              trick_levels={})
    assert assign, "expected a non-empty assignment"
    sides = graph["dock_sides"]
    for group in _physical_doors(sides):
        ws = {assign[s] for s in group if s in assign}
        assert len(ws) <= 1, f"sides of one door disagree: {group} -> {ws}"


@graph_required
def test_start_guard_protects_early_doors(graph):
    """Doors reachable from spawn with the starting kit stay vanilla."""
    from dread.DoorRando import roll_assignments, early_reachable
    start = {"Slide": 1, "Pulse Radar": 1, "Missile Tank": 1}
    reach, side_comp = early_reachable(graph, start, {})
    protected = {s for s, (c0, c1) in side_comp.items()
                 if c0 in reach or c1 in reach}
    assert protected, "guard should protect some early doors"
    assign = roll_assignments(graph, _RNG(), starting_items=start, trick_levels={})
    assert not (protected & set(assign)), "guarded early doors must stay vanilla"


@graph_required
def test_assignments_to_door_patches(graph):
    """One door_patch per physical door, with a valid door_type string."""
    from dread.DoorRando import roll_assignments, assignments_to_door_patches
    assign = roll_assignments(graph, _RNG(),
                              starting_items={"Slide": 1, "Pulse Radar": 1,
                                              "Missile Tank": 1}, trick_levels={})
    patches = assignments_to_door_patches(assign, graph)
    valid = set(graph["door_rando"]["weakness_door_type"].values())
    keys = set()
    for p in patches:
        assert p["door_type"] in valid
        k = (p["actor"]["scenario"], p["actor"]["actor"])
        assert k not in keys, "duplicate door_patch for one physical door"
        keys.add(k)


# ---- starting-area unit tests --------------------------------------------

@graph_required
def test_start_node_selection(graph):
    from dread.StartArea import start_node_for, REGION_BY_OPTION
    assert start_node_for(graph, 0) is None  # Artaria => default spawn
    for opt, region in REGION_BY_OPTION.items():
        if opt == 0:
            continue
        key, comp, patcher = start_node_for(graph, opt)
        assert key.split("::")[0] == region
        assert patcher.get("actor"), "non-Artaria spawn needs a patcher actor"


@graph_required
def test_minimal_start_items_bootstraps(graph):
    """The computed kit lifts the spawn's item-only early sphere to the target."""
    from dread.StartArea import (start_node_for, minimal_start_items,
                                 _reach_pickup_count, _FOOTHOLD_TARGET)
    base = {"Slide": 1, "Pulse Radar": 1, "Missile Tank": 1}
    _key, comp, _patcher = start_node_for(graph, 3, {}, base)  # Burenia
    extra = minimal_start_items(graph, comp, base, {})
    items = dict(base, **{n: 1 for n in extra})
    assert _reach_pickup_count(graph, items, {}, comp) >= _FOOTHOLD_TARGET


# ---- transport rando unit tests ------------------------------------------

class _ShuffleRNG:
    def shuffle(self, x):
        x.reverse()  # deterministic non-identity permutation

    def choice(self, seq):
        return seq[0]


@graph_required
def test_transport_matching_two_way_within_type(graph):
    from dread.TransportRando import roll_matching
    m = roll_matching(graph, _ShuffleRNG())
    tr = graph["transports"]
    for a, b in m.items():
        assert m[b] == a, "matching must be symmetric (two-way)"
        assert tr[a]["type"] == tr[b]["type"], "must match within transport type"


@graph_required
def test_connected_matching_keeps_pickups_reachable(graph):
    from dread.TransportRando import roll_connected_matching, _full_reachable_ok
    from dread.Tricks import DREAD_TRICKS
    tl = {t.short_name: 5 for t in DREAD_TRICKS}  # all tricks on (full reach)
    m = roll_connected_matching(graph, _ShuffleRNG(), tl)
    assert _full_reachable_ok(graph, m, tl)


@graph_required
def test_matching_to_elevators_shape(graph):
    from dread.TransportRando import roll_matching, matching_to_elevators
    m = roll_matching(graph, _ShuffleRNG())
    elevs = matching_to_elevators(m, graph)
    for e in elevs:
        assert set(e) >= {"teleporter", "destination", "connection_name"}
        assert e["teleporter"]["scenario"] and e["teleporter"]["actor"]
        assert e["destination"]["scenario"] and e["destination"]["actor"]


@graph_required
def test_matching_to_elevators_lands_at_dest_room(graph):
    """Regression: a shuffled ride must land at the DESTINATION endpoint's own
    landing platform (``start_point``), which physically lives in the destination
    scenario. The old code emitted the destination's ``target_spawn_point`` — an
    actor in the destination's *vanilla*-destination scenario — so the engine
    loaded the right room but couldn't find the spawn and crashed on the ride."""
    from dread.TransportRando import roll_matching, matching_to_elevators
    tr = graph["transports"]
    m = roll_matching(graph, _ShuffleRNG())
    # Map each emitted entry (keyed by source actor) back to its src side, so we
    # can identify the destination side via the matching and check the spawn.
    by_actor = {(meta["scenario"], meta["actor"]): sid for sid, meta in tr.items()}
    changed = 0
    for e in matching_to_elevators(m, graph):
        src_sid = by_actor[(e["teleporter"]["scenario"], e["teleporter"]["actor"])]
        dest_sid = m[src_sid]
        dmeta = tr[dest_sid]
        # Land in the destination endpoint's room, at that room's own platform.
        assert e["destination"]["scenario"] == dmeta["scenario"]
        assert e["destination"]["actor"] == dmeta["start_point"]
        changed += 1
    assert changed, "shuffle should have produced at least one changed ride"


# ---- gated real-generation -----------------------------------------------

def _ap() -> bool:
    try:
        import BaseClasses, Options  # noqa: F401
        from worlds.AutoWorld import World  # noqa: F401
    except ImportError:
        return False
    return True


runtime = pytest.mark.skipif(
    not (_ap() and GRAPH_PATH.exists()),
    reason="needs Archipelago runtime + logic_graph.json",
)


def _generate(opts):
    from test.general import setup_multiworld, gen_steps
    from Fill import distribute_items_restrictive
    mw = setup_multiworld(__import__("dread.World", fromlist=["DreadWorld"]).DreadWorld,
                          gen_steps, seed=2, options=opts)
    distribute_items_restrictive(mw)
    assert mw.has_beaten_game(mw.get_all_state(False), 1)
    assert mw.fulfills_accessibility()


@runtime
def test_door_rando_generates():
    _generate({"door_lock_rando": 1})


@runtime
@pytest.mark.parametrize("area", [1, 2, 3, 4, 5, 6])  # all non-Artaria spawns
def test_starting_area_generates(area):
    _generate({"starting_area": area})


@runtime
def test_door_rando_plus_start_compose():
    _generate({"door_lock_rando": 1, "starting_area": 2})


@runtime
def test_transport_rando_generates():
    _generate({"transport_rando": 1})


@runtime
def test_all_rando_compose():
    _generate({"door_lock_rando": 1, "transport_rando": 1, "starting_area": 2})


@runtime
@pytest.mark.parametrize("area", [1, 2, 3, 4, 5, 6])  # all non-Artaria spawns
def test_start_kit_baked_into_patcher(area):
    """The per-spawn bootstrap kit precollected into AP logic MUST also be
    granted by the ROM. A non-Artaria spawn opens different opening rooms, so
    minimal_start_items grants different unlocks per spawn; if those don't reach
    the patcher's starting_items the player spawns deep without the items logic
    assumed and is stuck. Regression for the kit being precollected but never
    baked (only the empty EXTRA_STARTING_ITEMS was)."""
    from test.general import setup_multiworld, gen_steps
    from dread.World import DreadWorld, item_name_to_item
    mw = setup_multiworld(DreadWorld, gen_steps, seed=2,
                          options={"starting_area": area})
    world = mw.worlds[1]
    kit = list(world._start_extra_items)
    assert kit, f"area {area} should compute a non-empty bootstrap kit"
    starting_items = world._build_placements_payload()["starting_items"]
    for name in kit:
        pid = item_name_to_item[name].patcher_item_id
        assert pid in starting_items, (
            f"area {area}: kit item {name!r} ({pid}) precollected into logic "
            f"but missing from patcher starting_items")
