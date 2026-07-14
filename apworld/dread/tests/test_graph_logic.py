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
    assert graph["graph_schema_version"] == 7
    assert graph["n_regions"] > 0
    assert len(graph["comp_regions"]) == graph["n_regions"]
    assert all(r for r in graph["comp_regions"])   # every component has a region
    assert len(graph["pickups"]) == 149
    assert graph["entrances"]
    # Victory may reference event atoms (compiled in graph_mode to
    # can_reach_region("Event:X")) — no item-only constraint needed here.


@graph_required
def test_transports_carry_source_cc(graph):
    """v4 schema: every transport endpoint records its source room's collision
    camera, so transport rando can rewrite the room-name-display label."""
    tr = graph["transports"]
    assert tr, "expected transport endpoints in the graph"
    for sid, meta in tr.items():
        cc = meta.get("source_cc")
        assert cc and cc.startswith("collision_camera"), (sid, cc)
        assert meta.get("transporter_name"), sid


@graph_required
def test_matching_to_room_names(graph):
    """A shuffled matching renames every transport's source room to point at its
    new destination; an empty matching (rando off) yields no overrides."""
    import random
    from dread.TransportRando import roll_matching, matching_to_room_names

    assert matching_to_room_names({}, graph) == {}

    m = roll_matching(graph, random.Random(7), "randomized")
    names = matching_to_room_names(m, graph)
    # One entry per transport endpoint, all "Transport to <dest>".
    total = sum(len(v) for v in names.values())
    assert total == len(graph["transports"])
    for scen, cc_map in names.items():
        for cc, label in cc_map.items():
            assert label.startswith("Transport to ")
            # The label names the NEW destination's transporter, matching the
            # elevators config's connection_name for the same source.
    # Cross-check one moved ride: its label is its resolved dest's name.
    tr = graph["transports"]
    for sid, dest in m.items():
        src = tr[sid]
        expected = f"Transport to {tr[dest]['transporter_name']}"
        assert names[src["scenario"]][src["source_cc"]] == expected
        break


@graph_required
def test_unreachable_pickup_locations_all_tricks_disabled(graph):
    """The full-accessibility guard's oracle: with every trick disabled, exactly
    the 8 Speed-Booster-Conservation pickups are unreachable even with a full
    loadout, and the helper pinpoints Speedbooster as the recovering trick. With
    all tricks at Beginner (the default), nothing is stranded."""
    from dread.graph_logic import unreachable_pickup_locations
    from dread.Tricks import DREAD_TRICKS

    all_off = {t.short_name: 0 for t in DREAD_TRICKS}
    unreachable, gating = unreachable_pickup_locations(graph, all_off)
    assert set(unreachable) == {
        "Burenia: Early Gravity Speedboost Room 1 - Missile+ Tank",
        "Cataris: Dairon Transport Access",
        "Dairon: Freezer - Missile Tank - Lower",
        "Dairon: Storm Missile Gate Room",
        "Dairon: Energy Recharge Station West",
        "Elun: Fan Room",
        "Ferenia: Speedboost Slopes Maze",
        "Ferenia: Space Jump Room - Missile+ Tank",
    }
    # Narrowed to the single real gate, not the whole frontier trick set.
    assert gating == {"Speedbooster"}

    beginner = {t.short_name: 1 for t in DREAD_TRICKS}
    assert unreachable_pickup_locations(graph, beginner) == ([], set())


@graph_required
def test_doorlocks_faithful_resolution_under_door_rando(graph):
    """Regression for the AP-00908778 false positive: ``misc:DoorLocks`` is
    Randovania's "Door Lock Randomizer" resource, so ``NOT DoorLocks``
    maneuvers (door-crossing shinesparks etc.) must be DEAD in a door-rando
    seed. The report's shape: a player with everything except Grapple, Bombs
    and Gravity Suit; the only exit from the flooded Waterfall bottom is the
    Map Station shinespark (event ``ArtariaMapSpeed``, gated on NOT DoorLocks),
    whose sprint physically crosses the Map Station<->Waterfall door — rolled
    to Grapple in that seed. The old always-passable resolution kept both
    Waterfall pickups "in logic" while the player could not reach them."""
    from dread.DoorRando import early_reachable
    from dread.TransportRando import _ALL_ITEMS
    from dread.Tricks import DREAD_TRICKS

    # (a) The sprint-prep event edge is exactly a bare NOT-DoorLocks guard in
    # the graph — the shape the faithful resolution exists for.
    ev_comps = {c for c, n in graph["events"] if n == "ArtariaMapSpeed"}
    assert ev_comps, "ArtariaMapSpeed event vanished from the graph"
    sprint_edges = [ast for src, dst, ast in graph["entrances"]
                    if dst in ev_comps]
    assert any(a.get("type") == "misc" and a.get("name") == "DoorLocks"
               and a.get("negate") for a in sprint_edges), sprint_edges

    # (b) Seed shape: full kit minus Grapple/Bombs/Gravity at the report's
    # intermediate tricks; the door pairs that guarded every non-sprint entry
    # to the Waterfall top rolled Grapple. Lenient evaluation credits the
    # pickups (the historical false positive); faithful evaluation does not.
    items = {nm: 99 for nm in _ALL_ITEMS}
    for gone in ("Grapple Beam", "Bomb", "Cross Bomb", "Power Bomb",
                 "Gravity Suit"):
        items[gone] = 0
    tl = {t.short_name: 2 for t in DREAD_TRICKS}
    grapple_pairs = [
        "Artaria::Save Station West::Door to Waterfall",
        "Artaria::Waterfall::Door to Save Station West",
        "Artaria::Teleport to Cataris::Door to Save Station West",
        "Artaria::Save Station West::Door to Teleport to Cataris",
        "Artaria::Map Station::Door to Waterfall",
        "Artaria::Waterfall::Door to Map Station",
        "Artaria::Behind Waterfall::Door to Water Reservoir",
        "Artaria::Water Reservoir::Door to Behind Waterfall",
        "Artaria::Waterfall::Door to Shortcut to Screw Attack",
        "Artaria::Shortcut to Screw Attack::Door to Waterfall",
    ]
    for sid in grapple_pairs:
        assert sid in graph["dock_sides"], f"stale dock side id: {sid}"
    assign = {sid: "Grapple Beam Door" for sid in grapple_pairs}
    comp_by_name = {name: comp for comp, name in graph["pickups"]}
    targets = ("Artaria: Waterfall - Energy Part",
               "Artaria: Waterfall - Missile Tank")

    lenient, _ = early_reachable(graph, items, tl, use_events=True,
                                 dock_assignments=assign, is_door_rando=False)
    assert all(comp_by_name[t] in lenient for t in targets)
    faithful, _ = early_reachable(graph, items, tl, use_events=True,
                                  dock_assignments=assign, is_door_rando=True)
    assert all(comp_by_name[t] not in faithful for t in targets)


@graph_required
def test_doorlocks_drop_oracle_under_door_rando(graph):
    """With door rando active, the faithful resolution strands exactly one
    full-loadout pocket at normal trick levels — Cataris: Underlava Puzzle
    Room 2, whose only entry is a vanilla-locks-only maneuver. That single
    location is what World's full/items auto-drop absorbs (the #124 FillError
    is gone). Vanilla door locks keep the historical zero-drop behavior."""
    from dread.graph_logic import unreachable_pickup_locations
    from dread.Tricks import DREAD_TRICKS

    beginner = {t.short_name: 1 for t in DREAD_TRICKS}
    unreachable, _ = unreachable_pickup_locations(
        graph, beginner, door_lock_rando=True)
    assert unreachable == ["Cataris: Underlava Puzzle Room 2"]
    assert unreachable_pickup_locations(
        graph, beginner, door_lock_rando=False) == ([], set())


@graph_required
def test_max_no_suit_threshold_matches_world_constant(graph):
    """The faithful HP model in World.py sizes the energy progression pool from
    MAX_NO_SUIT_HP (the largest no-suit damage_threshold in logic). If upstream
    logic raises that ceiling past the constant, the default pool may no longer
    provision enough advancement energy — catch it here so the constant (and the
    test_energy_is_progression_visible_to_logic budget) stay honest."""
    import re
    world_src = (ROOT / "World.py").read_text()
    m = re.search(r"MAX_NO_SUIT_HP\s*=\s*(\d+)", world_src)
    assert m, "MAX_NO_SUIT_HP constant not found in World.py"
    declared = int(m.group(1))

    def walk(ast, acc):
        if not isinstance(ast, dict):
            return
        if ast.get("type") == "damage_threshold" and not ast.get("suit_options"):
            acc.append(ast["hp_needed"])
        for c in ast.get("items", []):
            walk(c, acc)

    acc = []
    for _src, _dst, ast in graph["entrances"]:
        walk(ast, acc)
    walk(graph["victory_condition"], acc)
    actual_max = max(acc) if acc else 0
    assert actual_max <= declared, (
        f"graph has a no-suit threshold {actual_max} > MAX_NO_SUIT_HP "
        f"{declared}; bump the constant in World.py and re-verify the default "
        f"energy pool still covers it."
    )


@graph_required
def test_max_binding_ammo_cap_matches_world_constants(graph):
    """The faithful ammo model in World.py sizes the Missile/Power-Bomb tank
    progression pool from MAX_BINDING_MISSILE_CAP / MAX_BINDING_PB_CAP — the
    largest ammo capacity that is the SOLE gate to 100% reachability (every
    higher ``sum`` threshold is OR'd with a reachable alternative). Re-derive the
    caps straight from the graph and assert the constants still cover them; if
    upstream logic introduces a higher sole-binding threshold this catches it."""
    import re
    sys.path.insert(0, str(ROOT.parent.parent / "scripts"))
    from analyze_ammo_binding import binding_caps  # noqa: E402

    mcap, pbcap = binding_caps(graph)
    world_src = (ROOT / "World.py").read_text()
    dm = int(re.search(r"MAX_BINDING_MISSILE_CAP\s*=\s*(\d+)", world_src).group(1))
    dp = int(re.search(r"MAX_BINDING_PB_CAP\s*=\s*(\d+)", world_src).group(1))
    assert mcap <= dm, (
        f"graph's sole-binding missile cap {mcap} > MAX_BINDING_MISSILE_CAP {dm}; "
        f"bump the constant in World.py and re-verify the tank progression pool."
    )
    assert pbcap <= dp, (
        f"graph's sole-binding PB cap {pbcap} > MAX_BINDING_PB_CAP {dp}; "
        f"bump the constant in World.py and re-verify the PB tank pool."
    )


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
    # Faithful HP model: the real damage thresholds must be satisfiable across
    # trick levels and accessibility. Beginner (trick_level 1) is the binding
    # case (fewest suitless-trick shortcuts, so the no-suit HP gates bind).
    {"trick_level": 1, "accessibility": "full"},
    {"trick_level": 1, "accessibility": "minimal"},
    # Energy extremes. Low per-tank (each tank worth less HP, so each damage gate
    # needs more energy). The worst no-suit HP gates (up to 1150) are always
    # OR'd with a Combat/Knowledge-trick or Power Bomb alternative, so a thin
    # energy budget doesn't strand a location — verified solvable at full
    # accessibility even at 50/tank and a reduced tank count.
    {"energy_per_tank": 50},
    {"energy_per_tank": 50, "trick_level": 1},
    {"energy_tank_count": 4},
    # NOTE: energy_tank_count: 0 is a known-unsupported extreme. With no tanks
    # the 16 parts (499 HP max) are ALL progression and can self-gate (a damage
    # route needing many parts may sit behind that route), so neither `full` nor
    # `minimal` reliably generates. Documented in the v0.3 report; not asserted
    # here. Users wanting low energy should lower energy_per_tank, not zero tanks.
    # High counts: larger advancement energy pool — checks pool pressure /
    # trim behavior under full accessibility.
    {"energy_tank_count": 12, "energy_part_count": 24},
    # Faithful AMMO model: the missile / power-bomb sum gates must stay
    # satisfiable as the ammo knobs feed logic. At vanilla the binding caps
    # (15 missiles / 2 PB) are met by base + the precollected Missile Tank, so
    # no findable tank is required; lowering the starting / per-tank ammo
    # promotes more tanks to progression (_ammo_progression_counts).
    {"starting_missiles": 0},                       # base 0 -> need ~7 tanks
    {"starting_missiles": 0, "missile_tank_ammo": 1},  # +1/tank -> ~14 tanks
    {"missile_tank_ammo": 0},                       # tanks inert; base 15 carries
    {"starting_power_bombs": 0, "power_bomb_tank_ammo": 1},  # PB cap via 2 tanks
    {"starting_missiles": 0, "trick_level": 1, "accessibility": "full"},
    {"starting_missiles": 0, "trick_level": 5, "accessibility": "minimal"},
    # High per-tank ammo (each tank worth more) — fewer tanks needed, trivially
    # solvable; guards the credit math doesn't over-promote.
    {"missile_tank_ammo": 50, "power_bomb_tank_ammo": 5},
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
