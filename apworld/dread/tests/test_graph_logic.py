"""Tests for the native region-graph logic model (graph_logic.py + the
``logic_graph.json`` emit).

Two tiers:
  * Unit tests on the artifact + dock substitution — no Archipelago runtime.
  * A gated real-generation test that builds the graph via the REAL
    DreadWorld.create_regions/set_rules path (DREAD_GRAPH_LOGIC=1) and asserts
    solvable + accessibility across configs. Skips without the AP runtime /
    artifact (same gate as the other runtime tests).
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


def _graph_available() -> bool:
    return GRAPH_PATH.exists()


graph_required = pytest.mark.skipif(
    not _graph_available(),
    reason="logic_graph.json not materialized (run "
           "`python scripts/extract_dread_rules.py --all --graph-only`)",
)


# ---- artifact structure (no AP runtime) ----------------------------------

@pytest.fixture(scope="module")
def graph():
    return json.loads(GRAPH_PATH.read_text())


@graph_required
def test_graph_schema_and_shape(graph):
    assert graph["graph_schema_version"] == 3
    assert graph["n_regions"] > 0
    assert len(graph["pickups"]) == 149
    assert graph["entrances"]
    # Victory may reference event atoms (compiled in graph_mode to
    # can_reach_region("Event:X")) — no item-only constraint needed here.


@graph_required
def test_no_generic_no_suit_damage_thresholds(graph):
    """Regression: the emitter must strip generic (no-suit) damage_threshold
    atoms to TRIVIAL. Leaving them in per-edge rules over-restricts — the path
    to the ship needs ~500-800 HP that AP's advancement sweep can't supply
    (Energy Part is non-progression), so seeds go unbeatable at low trick levels
    even with a full loadout. Suit-based thresholds (non-empty suit_options) are
    fine (the suit is reachable). See scripts/extract_dread_rules.strip_no_suit_damage."""
    def walk(ast, found):
        if ast.get("type") == "damage_threshold" and not ast.get("suit_options"):
            found.append(ast)
        for c in ast.get("items", ()):
            walk(c, found)
    found: list = []
    for _src, _dst, ast in graph["entrances"]:
        walk(ast, found)
    walk(graph["victory_condition"], found)
    assert not found, f"{len(found)} no-suit damage_threshold atoms leaked into rules"


@graph_required
def test_every_pickup_maps_to_an_ap_location(graph):
    # Read names straight from locations.json so this runs without the AP runtime
    # (importing dread.Locations pulls BaseClasses, absent in CI).
    locs = json.loads((ROOT / "data" / "locations.json").read_text())
    names = {l["name"] for l in locs}
    for _comp, name in graph["pickups"]:
        assert name in names, f"pickup {name!r} not in AP locations"


@graph_required
def test_dock_atoms_resolve(graph):
    """Every symbolic dock atom references a side in dock_sides whose
    (dock_type, default_weakness) has an entry in weakness_requirements."""
    sides = graph["dock_sides"]
    wreq = graph["weakness_requirements"]

    def walk(ast):
        t = ast.get("type")
        if t == "dock":
            sid = ast["side_id"]
            assert sid in sides, f"dock atom side {sid!r} missing from dock_sides"
            s = sides[sid]
            key = f"{s['dock_type']}::{s['default_weakness']}"
            assert key in wreq, f"weakness {key!r} missing from table"
        elif t in ("and", "or"):
            for c in ast["items"]:
                walk(c)

    n_dock_atoms = 0
    for _src, _dst, ast in graph["entrances"]:
        walk(ast)
        n_dock_atoms += json.dumps(ast).count('"type": "dock"')
    assert n_dock_atoms > 0, "expected symbolic dock atoms for door rando"


@graph_required
def test_dock_sides_are_two_sided(graph):
    """Each rando-eligible dock's paired side is itself a known node id (so door
    rando can keep both sides' weakness in sync)."""
    sides = graph["dock_sides"]
    for sid, meta in sides.items():
        assert meta["paired_side_id"], f"{sid} missing paired side"
        assert meta["patcher"]["scenario"], f"{sid} missing patcher scenario"
        assert meta["patcher"]["actor"], f"{sid} missing patcher actor"


def test_resolve_docks_substitutes_assignment():
    """_resolve_docks swaps a dock atom for its assigned weakness requirement."""
    from dread.graph_logic import _resolve_docks
    dock_sides = {"S": {"dock_type": "door", "default_weakness": "Power Beam Door"}}
    wreq = {
        "door::Power Beam Door": {"type": "trivial"},
        "door::Wave Beam Door": {"type": "item", "name": "Wave Beam", "amount": 1},
    }
    ast = {"type": "dock", "side_id": "S"}
    # default (no assignment) -> vanilla weakness
    assert _resolve_docks(ast, {}, dock_sides, wreq) == {"type": "trivial"}
    # explicit assignment -> that weakness's requirement
    out = _resolve_docks(ast, {"S": "Wave Beam Door"}, dock_sides, wreq)
    assert out == {"type": "item", "name": "Wave Beam", "amount": 1}
    # nested
    nested = {"type": "and", "items": [ast, {"type": "trivial"}]}
    res = _resolve_docks(nested, {"S": "Wave Beam Door"}, dock_sides, wreq)
    assert res["items"][0] == {"type": "item", "name": "Wave Beam", "amount": 1}


# ---- real-generation (gated on AP runtime) -------------------------------

def _ap_runtime_available() -> bool:
    try:
        import BaseClasses  # noqa: F401
        import Options  # noqa: F401
        from worlds.AutoWorld import World  # noqa: F401
    except ImportError:
        return False
    return True


runtime = pytest.mark.skipif(
    not (_ap_runtime_available() and _graph_available()),
    reason="needs Archipelago runtime + logic_graph.json",
)


@runtime
@pytest.mark.parametrize("opts", [
    {},
    {"accessibility": "items"},
    {"accessibility": "minimal"},
    {"trick_level": 5},
    {"required_artifacts": 12},
    # The DreadfulJim report: door rando + full accessibility at the beginner
    # baseline. Used to fail "Game appears as unbeatable" because no-suit
    # damage_threshold atoms leaked into the path to the ship.
    {"accessibility": "full", "door_lock_rando": "randomized",
     "progressive_charge_beam": True, "progressive_bomb": True,
     "required_artifacts": 6, "artifact_placement": "anywhere",
     "start_with_pulse_radar": False},
])
def test_graph_generation_is_solvable(opts):
    from test.general import setup_multiworld, gen_steps
    from Fill import distribute_items_restrictive
    from dread.World import DreadWorld

    os.environ["DREAD_GRAPH_LOGIC"] = "1"
    try:
        mw = setup_multiworld(DreadWorld, gen_steps, seed=1, options=opts)
        distribute_items_restrictive(mw)
        state = mw.get_all_state(False)
        assert mw.has_beaten_game(state, 1), f"not beatable for {opts}"
        assert mw.fulfills_accessibility(), f"accessibility failed for {opts}"
    finally:
        os.environ.pop("DREAD_GRAPH_LOGIC", None)
