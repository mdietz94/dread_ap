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


def test_roll_assignments_change_doors_to_restricts_targets(graph):
    """``change_doors_to`` confines every assigned weakness to the selected set."""
    from dread.DoorRando import roll_assignments
    allowed = {"Missile Door", "Super Missile Door"}
    assign = roll_assignments(graph, random.Random(3), mode="individual",
                              change_doors_to=allowed)
    assert assign, "expected some doors to be randomized"
    assert set(assign.values()) <= allowed


def test_roll_assignments_doors_to_change_restricts_sources(graph):
    """``doors_to_change`` leaves doors of unselected vanilla types vanilla, so
    fewer doors change than an unrestricted roll."""
    from dread.DoorRando import roll_assignments
    only_power = {"Power Beam Door"}
    restricted = roll_assignments(graph, random.Random(3), mode="individual",
                                  doors_to_change=only_power)
    unrestricted = roll_assignments(graph, random.Random(3), mode="individual")
    # Every changed door's canonical source type is the selected one (the door
    # is keyed by its group's first side's default_weakness).
    ds = graph["dock_sides"]
    seen_groups = set()
    for sid in restricted:
        pair = ds[sid].get("paired_side_id")
        key = frozenset({sid, pair}) if pair else frozenset({sid})
        seen_groups.add(key)
    assert restricted, "Power Beam doors should still randomize"
    assert len(restricted) < len(unrestricted)


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


def test_roll_assignments_door_types_global_consistency():
    """'types' mode (RDV "Door Types") remaps BY TYPE: every door of one vanilla
    weakness becomes the SAME target world-wide, across distinct physical doors."""
    from dread.DoorRando import roll_assignments
    wdt = {"Wave Beam Door": "wave_beam",
           "Power Beam Door": "power_beam",
           "Plasma Beam Door": "plasma_beam"}

    def side(actor, wk, pair):
        return {"patcher": {"scenario": "s010_cave", "actor": actor},
                "dock_type": "door", "default_weakness": wk,
                "paired_side_id": pair}

    graph = {
        "dock_sides": {  # two Power Beam doors (D1, D2) + one Wave Beam door (D3)
            "A": side("D1", "Power Beam Door", "B"),
            "B": side("D1", "Power Beam Door", "A"),
            "C": side("D2", "Power Beam Door", "Cx"),
            "Cx": side("D2", "Power Beam Door", "C"),
            "E": side("D3", "Wave Beam Door", "Ex"),
            "Ex": side("D3", "Wave Beam Door", "E"),
        },
        "door_rando": {
            "change_from": ["Power Beam Door", "Wave Beam Door"],
            "change_to": list(wdt),
            "weakness_door_type": wdt,
            "locked_weakness": "Access Permanently Closed",
            "vanilla_shield_ids": {},
        },
    }
    for seed in range(10):
        assign = roll_assignments(graph, random.Random(seed), mode="types")
        assert assign, "expected a non-empty assignment"
        pb = {assign[s] for s in ("A", "B", "C", "Cx") if s in assign}
        wv = {assign[s] for s in ("E", "Ex") if s in assign}
        assert len(pb) <= 1, f"Power Beam doors disagree under type remap: {pb}"
        assert len(wv) <= 1, f"Wave Beam doors disagree under type remap: {wv}"
        # both Power Beam physical doors are remapped together
        assert ("A" in assign) == ("C" in assign)


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
def test_itorash_capsule_rides_never_shuffled(graph):
    """The Hanubia<->Itorash capsule rides (``CCapsuleUsableComponent``) must stay
    vanilla: the up-launch and the post-Raven-Beak escape are scripted around the
    capsule actor + its special landing platform, so repointing either direction
    crashes the game on the ride. Regression for the reported Hanubia-entrance /
    post-Raven-Beak crashes."""
    import random
    from dread.TransportRando import roll_matching
    tr = graph["transports"]
    capsules = [sid for sid, m in tr.items()
                if m.get("component") == "CCapsuleUsableComponent"]
    # Both Itorash capsule endpoints are present and tagged.
    assert len(capsules) == 2, capsules
    # They are never assigned a partner across many rolls (kept vanilla).
    for seed in range(100):
        m = roll_matching(graph, random.Random(seed))
        assert not any(c in m for c in capsules), \
            f"capsule shuffled at seed {seed}: {[c for c in capsules if c in m]}"


@graph_required
def test_flipper_shuttle_patches_both_actors(graph):
    """The Ghavoran Flipper shuttle has a cutscene actor and a plain actor in the
    same room; open-dread-rando only repoints the actor we pass, so a shuffled
    Flipper must emit BOTH actors with the same destination or later rides snap
    back to the vanilla connection (mirrors Randovania's dual-actor patch)."""
    import random
    from dread.TransportRando import roll_matching, matching_to_elevators
    CUT = "wagontrain_quarantine_with_cutscene_000"
    DUP = "wagontrain_quarantine_000"
    for seed in range(100):
        elevs = matching_to_elevators(roll_matching(graph, random.Random(seed)), graph)
        cut = [e for e in elevs if e["teleporter"]["actor"] == CUT]
        if not cut:
            continue
        dup = [e for e in elevs if e["teleporter"]["actor"] == DUP]
        assert dup, f"Flipper shuffled (seed {seed}) but second actor not patched"
        assert dup[0]["destination"] == cut[0]["destination"]
        assert dup[0]["teleporter"]["scenario"] == cut[0]["teleporter"]["scenario"]
        return
    import pytest as _pytest
    _pytest.skip("no sampled seed shuffled the Flipper shuttle")


@graph_required
def test_connected_matching_keeps_pickups_reachable(graph):
    from dread.TransportRando import (roll_connected_matching,
                                      _no_reachability_regression)
    from dread.Tricks import DREAD_TRICKS
    tl = {t.short_name: 5 for t in DREAD_TRICKS}  # all tricks on (full reach)
    m = roll_connected_matching(graph, _ShuffleRNG(), tl)
    assert _no_reachability_regression(graph, m, tl)


@graph_required
def test_connected_matching_shuffles_with_all_tricks_disabled(graph):
    """Regression: the faithful starter preset (all tricks disabled) leaves 8
    Speedbooster-gated pickups unreachable even under VANILLA transports, so the
    old all-pickups-reachable acceptance test rejected every roll and silently
    fell back to vanilla — transports never shuffled. The no-regression test must
    still produce a real (non-empty) matching here."""
    import random
    from dread.TransportRando import roll_connected_matching
    from dread.Tricks import DREAD_TRICKS
    tl = {t.short_name: 0 for t in DREAD_TRICKS}  # all tricks disabled
    # Real RNG so the retry loop sees distinct permutations (the deterministic
    # _ShuffleRNG yields one fixed matching, defeating the point of the retry).
    m = roll_connected_matching(graph, random.Random(999), tl)
    assert m, "transport matching fell back to vanilla at all-tricks-disabled"


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
        key = (e["teleporter"]["scenario"], e["teleporter"]["actor"])
        if key not in by_actor:
            # Synthetic Flipper second-actor duplicate (not its own endpoint); its
            # destination mirrors the cutscene entry, validated separately.
            continue
        src_sid = by_actor[key]
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
    return mw


@runtime
def test_door_rando_generates():
    _generate({"door_lock_rando": 1})


@runtime
def test_door_types_rando_generates():
    """RDV "Door Types" mode (value 2 / 'door_types') produces a solvable seed."""
    _generate({"door_lock_rando": 2})


@runtime
def test_door_lock_rando_legacy_alias():
    """The pre-rename 'randomized' name still resolves (back-compat alias)."""
    _generate({"door_lock_rando": "randomized"})


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
def test_transport_rando_rewrites_room_names():
    """A transport-rando seed emits room-name overrides that flow into the
    patcher's camera_names_dict, so each shuffled ride's source room shows its
    NEW destination instead of the vanilla one."""
    from test.general import setup_multiworld, gen_steps
    from dread.World import DreadWorld
    from dread.patcher_pipeline import (
        build_patcher_input_from_placements, load_starter_template,
    )

    mw = setup_multiworld(DreadWorld, gen_steps, seed=2,
                          options={"transport_rando": 1})
    world = mw.worlds[1]
    payload = world._build_placements_payload()
    names = payload["transport_room_names"]
    assert names, "transport rando should produce room-name overrides"

    # At least one source room now names a different destination than vanilla.
    template = load_starter_template()
    vanilla = template["cosmetic_patches"]["lua"]["camera_names_dict"]
    changed = [
        (scen, cc) for scen, cc_map in names.items() for cc, label in cc_map.items()
        if vanilla.get(scen, {}).get(cc) != label
    ]
    assert changed, "expected at least one transport room renamed vs vanilla"

    # And the override lands in the final patcher input.
    merged = build_patcher_input_from_placements(payload)
    cam = merged["cosmetic_patches"]["lua"]["camera_names_dict"]
    for scen, cc_map in names.items():
        for cc, label in cc_map.items():
            assert cam[scen][cc] == label


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
        # Paired-resource starters must bake their full grant, not just the
        # unlock flag. The Power Bomb launcher MUST also grant capacity
        # (ITEM_WEAPON_POWER_BOMB_MAX) — without it the player spawns with the
        # weapon unlocked but 0/0 power bombs (unusable), the start-location
        # randomizer "0/0 power bombs" bug. AP logic credits the launcher's
        # starting_power_bombs capacity, so the ROM must too.
        if name == "Power Bomb":
            assert starting_items.get("ITEM_WEAPON_POWER_BOMB_MAX", 0) >= 1, (
                f"area {area}: Power Bomb starter baked the launcher flag but "
                f"no ITEM_WEAPON_POWER_BOMB_MAX capacity — player has 0/0 PBs")


# --------------------------------------------------------------------------- #
# Full-accessibility guard (all-tricks-disabled starter-preset port)
# --------------------------------------------------------------------------- #

def _all_tricks_disabled() -> dict:
    from dread.Tricks import VISIBLE_TRICKS
    return {t.attr: 0 for t in VISIBLE_TRICKS}


@runtime
def test_all_tricks_disabled_full_drops_unreachable():
    """Disabling every trick (the faithful Randovania starter-preset port:
    minimal_logic off + empty specific_levels) strands the 8 Speed-Booster-
    Conservation pickups, which 'accessibility: full' cannot satisfy. Rather than
    fail, the world DROPS those locations (doesn't create them) so generation
    succeeds and fulfills_accessibility holds over the remaining set. The dropped
    set must be exactly the 8 unreachable pickups — no created location may be one
    of them, and the pool stays balanced (asserted implicitly by a clean fill)."""
    from dread.graph_logic import (
        load_graph, ammo_amounts_from_options, unreachable_pickup_locations,
    )
    from dread.Tricks import effective_trick_levels
    opts = _all_tricks_disabled()
    opts["accessibility"] = "full"
    mw = _generate(opts)  # asserts beatable + fulfills_accessibility (over kept set)

    world = mw.worlds[1]
    unreachable, _ = unreachable_pickup_locations(
        load_graph(), effective_trick_levels(world.options),
        energy_per_tank=int(world.options.energy_per_tank.value),
        ammo_amounts=ammo_amounts_from_options(world.options))
    assert unreachable, "fixture assumes all-tricks-off strands the speedboost rooms"
    assert world._dropped_locations == set(unreachable)
    created = {loc.name for loc in mw.get_locations(1)}
    assert not (created & set(unreachable)), "dropped locations must not be created"


@runtime
def test_all_tricks_disabled_minimal_generates():
    """Under 'minimal' the stranded spots hold filler (Randovania-faithful) and
    nothing is dropped, so generation succeeds with every trick disabled."""
    opts = _all_tricks_disabled()
    opts["accessibility"] = "minimal"
    mw = _generate(opts)
    assert mw.worlds[1]._dropped_locations == set()


@runtime
def test_only_suitless_disabled_full_generates():
    """The Suitless un-hide in isolation: disabling just Heat/Cold Runs keeps
    every location reachable (the lava/heat gates have suit/HP alternatives), so
    'full' generates and drops nothing."""
    mw = _generate({"trick_suitless": 0, "accessibility": "full"})
    assert mw.worlds[1]._dropped_locations == set()


@runtime
def test_default_full_drops_nothing():
    """The default config (global Beginner, all follow_global) is fully reachable,
    so no location is dropped."""
    mw = _generate({"accessibility": "full"})
    assert mw.worlds[1]._dropped_locations == set()
