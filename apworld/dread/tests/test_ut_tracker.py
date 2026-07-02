"""Universal Tracker (UT) integration tests.

UT recomputes a slot's reachable-location set by re-running generation from the
server's slot_data (a single-player "fake" regen with a DIFFERENT RNG stream).
Our world rolls a custom per-seed region graph (door-lock rando, transport rando,
start-area gating) from ``self.random`` in ``generate_early``; without restoring
those decisions UT re-rolls a degenerate graph and reachability collapses to a
near-empty set (the reported bug). ``World.interpret_slot_data`` +
``fill_slot_data``'s ``ut_state`` payload + the ``generate_early`` restore branch
fix this — these tests prove the round-trip.

All real-generation tests are AP-runtime + logic-graph gated (see
[[running-ap-gated-dread-tests]]).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

GRAPH_PATH = ROOT / "data" / "logic_graph.json"


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

# The options the failing seed used: door rando (Door Types) + a non-Artaria
# spawn + transport rando — i.e. every per-seed-random graph knob on at once.
RANDO_OPTIONS = {
    "door_lock_rando": 2,      # Door Types
    "transport_rando": 1,
    "starting_area": 3,        # Burenia (deep spawn → bootstrap kit)
}


def _setup_with_passthrough(world_type, options, seed, passthrough=None):
    """Like test.general.setup_multiworld for a single world, but injects UT's
    ``re_gen_passthrough`` / ``generation_is_fake`` BEFORE the gen steps run, so
    ``generate_early`` sees them (the real UT entry point)."""
    from argparse import Namespace
    from BaseClasses import CollectionState, MultiWorld
    from worlds.AutoWorld import call_all
    from test.general import gen_steps

    mw = MultiWorld(1)
    mw.game = {1: world_type.game}
    mw.player_name = {1: "Tester1"}
    mw.set_seed(seed)
    args = Namespace()
    for key, option in world_type.options_dataclass.type_hints.items():
        setattr(args, key, {1: option.from_any(options.get(key, option.default))})
    mw.set_options(args)
    mw.state = CollectionState(mw)
    if passthrough is not None:
        mw.re_gen_passthrough = {world_type.game: passthrough}
        mw.generation_is_fake = True
    for step in gen_steps:
        call_all(mw, step)
    return mw


def _reachable_locations(mw, kit):
    """Names of the player-1 locations reachable from the precollected starting
    items plus ``kit`` (an item-name -> count map), each forced progression so it
    registers in state."""
    from BaseClasses import CollectionState, ItemClassification
    world = mw.worlds[1]
    state = CollectionState(mw)
    for item in mw.precollected_items[1]:
        state.collect(item, prevent_sweep=True)
    for name, count in kit.items():
        for _ in range(count):
            state.collect(
                world.create_item(name, ItemClassification.progression),
                prevent_sweep=True,
            )
    state.sweep_for_advancements()
    return {loc.name for loc in mw.get_locations(1) if loc.can_reach(state)}


# Door+transport options on the VANILLA (Artaria) spawn — the failing seed's
# shape. With the rich partial kit below this reaches a large, door-sensitive
# chunk of the world (the door roll moves ~40 locations in/out of logic between
# seeds), so a regen that re-rolled the doors would compute a visibly different
# set — exactly the reported bug.
DOOR_OPTIONS = {"door_lock_rando": 2, "transport_rando": 1}

# A rich partial inventory (like the reported player's kit) — enough to open a
# big chunk of the map but not everything, so reachability is sensitive to the
# door graph rather than saturated.
_DOOR_SENSITIVE_KIT = {
    "Morph Ball": 1, "Bomb": 1, "Charge Beam": 1, "Spider Magnet": 1,
    "Spin Boost": 1, "Varia Suit": 1, "Speed Booster": 1, "Power Bomb": 1,
    "Grapple Beam": 1, "Progressive Beam": 3, "Flash Shift": 1,
}


@runtime
def test_interpret_slot_data_returns_passthrough():
    """The UT hook returns its input truthily so UT triggers a regen pass."""
    from dread.World import DreadWorld
    sd = {"ut_state": {"options": {}}}
    assert DreadWorld.interpret_slot_data(sd) is sd


@runtime
def test_ut_state_round_trip_restores_per_seed_graph():
    """Generate a door+transport+start seed, export its slot_data, then simulate
    a UT regen (DIFFERENT seed, DEFAULT options — i.e. no player YAML) and assert
    the regen restores the exact per-seed decisions and the SAME reachable set —
    instead of re-rolling a degenerate graph."""
    from test.general import setup_multiworld, gen_steps
    from dread.World import DreadWorld

    orig = setup_multiworld(DreadWorld, gen_steps, seed=2, options=RANDO_OPTIONS)
    ow = orig.worlds[1]
    slot_data = ow.fill_slot_data()
    assert "ut_state" in slot_data
    assert ow._dock_assignments, "door rando should have rolled assignments"
    assert ow._transport_matching, "transport rando should have rolled a matching"
    assert ow._start_comp is not None, "non-Artaria spawn should pick a comp"

    # UT regen: different seed (so a re-roll would diverge) and DEFAULT options
    # (door/transport/start all default-OFF) — only the restore can recover them.
    regen = _setup_with_passthrough(
        DreadWorld, options={}, seed=99999, passthrough=slot_data)
    rw = regen.worlds[1]

    assert rw._ut is True
    # Options restored from slot_data despite the regen starting from defaults.
    assert int(rw.options.door_lock_rando.value) == RANDO_OPTIONS["door_lock_rando"]
    assert int(rw.options.transport_rando.value) == RANDO_OPTIONS["transport_rando"]
    assert int(rw.options.starting_area.value) == RANDO_OPTIONS["starting_area"]
    # Per-seed rolled decisions restored byte-for-byte (not re-rolled).
    assert rw._dock_assignments == ow._dock_assignments
    assert rw._transport_matching == ow._transport_matching
    assert rw._start_comp == ow._start_comp
    assert rw._start_extra_items == ow._start_extra_items


@runtime
def test_ut_regen_reachable_set_matches_seed_not_a_reroll():
    """The behavioral payoff and the reported-bug regression: a UT regen computes
    the SEED'S reachable set, not the near-empty/different set a re-roll produces.

    Generate seed A (door+transport), capture its reachable set for a partial
    kit. Generate seed B with the SAME options — its door roll moves a large,
    door-sensitive chunk of locations, so B's reachable set DIFFERS from A's
    (proving the kit is genuinely door-sensitive). Then UT-regen A's slot_data
    while seeded like B: the regen must reproduce A's set exactly — i.e. the
    restore overrode B's RNG."""
    from test.general import setup_multiworld, gen_steps
    from dread.World import DreadWorld

    a = setup_multiworld(DreadWorld, gen_steps, seed=2, options=DOOR_OPTIONS)
    b = setup_multiworld(DreadWorld, gen_steps, seed=12345, options=DOOR_OPTIONS)
    reach_a = _reachable_locations(a, _DOOR_SENSITIVE_KIT)
    reach_b = _reachable_locations(b, _DOOR_SENSITIVE_KIT)

    # The kit must reach a substantial, door-sensitive slice (not a near-empty
    # set), or the equality below would be vacuous.
    assert len(reach_a) > 40, f"kit should reach a big chunk, got {len(reach_a)}"
    assert reach_a != reach_b, "door roll should move the reachable set per seed"

    slot_data = a.worlds[1].fill_slot_data()
    regen = _setup_with_passthrough(
        DreadWorld, options={}, seed=12345, passthrough=slot_data)
    assert regen.worlds[1]._dock_assignments == a.worlds[1]._dock_assignments
    reach_u = _reachable_locations(regen, _DOOR_SENSITIVE_KIT)
    assert reach_u == reach_a, "UT regen must reproduce the seed's reachable set"
    assert reach_u != reach_b, "UT regen must NOT collapse to the re-roll's set"


@runtime
def test_ut_regen_reproduces_dropped_locations():
    """When ``accessibility: full`` + all-tricks-off drops the unreachable
    speedboost pickups (World._compute_dropped_locations), the created-location
    set becomes per-seed-variable. UT's fake-regen must reproduce the SAME drop
    set — otherwise its created locations wouldn't match the multidata and
    reachability would be miscomputed. The drop is a pure function of the
    (restored) options + graph state, so a regen from slot_data restoring those
    reproduces it exactly, even seeded differently and starting from defaults."""
    from test.general import setup_multiworld, gen_steps
    from dread.Tricks import VISIBLE_TRICKS
    from dread.World import DreadWorld

    opts = {t.attr: 0 for t in VISIBLE_TRICKS}
    opts["accessibility"] = "full"

    orig = setup_multiworld(DreadWorld, gen_steps, seed=2, options=opts)
    ow = orig.worlds[1]
    assert ow._dropped_locations, "all-tricks-off + full should drop some pickups"

    slot_data = ow.fill_slot_data()
    regen = _setup_with_passthrough(
        DreadWorld, options={}, seed=99999, passthrough=slot_data)
    rw = regen.worlds[1]

    assert rw._ut is True
    assert rw._dropped_locations == ow._dropped_locations
    # The created-location sets must match too (the drop is what makes them
    # per-seed-variable, so this is the property UT actually relies on).
    assert {loc.name for loc in regen.get_locations(1)} == \
        {loc.name for loc in orig.get_locations(1)}
