"""Door-lock rando + more-starting-areas tests.

Unit tests (no AP runtime) on the per-seed assignment / spawn logic, plus gated
real-generation tests that the features produce solvable seeds via the REAL
DreadWorld path (and compose). Both ride the native graph; see graph_logic.py.
"""
from __future__ import annotations

import json
import os
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
