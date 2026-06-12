"""Unit tests for the AST → lambda compiler in Rules.py.

These run without Archipelago — compile_to_lambda is kept duck-typed
(no CollectionState import) so tests can pass in a stub state object.

The compiler primitives are in Rules.py; the upstream-data → AST
compiler lives in scripts/extract_dread_rules.py and is exercised in
test_extract_dread_rules.py."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


# Pull the apworld onto sys.path (mirrors conftest.py setup).
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.Rules import (  # noqa: E402
    compile_to_lambda,
)


class StubState:
    """Minimal stand-in for AP's CollectionState — exposes only the
    ``has(name, player, count=1)`` shape compile_to_lambda needs.

    Items the player owns are stored as a counted multiset; player arg
    is checked so tests would catch a hardcoded-player-id regression.
    """

    def __init__(self, inventory: dict[str, int], expected_player: int = 1):
        self.inventory = inventory
        self.expected_player = expected_player

    def has(self, name: str, player: int, count: int = 1) -> bool:
        assert player == self.expected_player, \
            f"compile_to_lambda passed wrong player {player}"
        return self.inventory.get(name, 0) >= count

    def count(self, name: str, player: int) -> int:
        assert player == self.expected_player, \
            f"compile_to_lambda passed wrong player {player}"
        return self.inventory.get(name, 0)


# ---- primitive nodes ----

def test_trivial_is_always_true():
    pred = compile_to_lambda({"type": "trivial"}, player=1)
    assert pred(StubState({})) is True
    assert pred(StubState({"Morph Ball": 1})) is True


def test_impossible_is_always_false():
    pred = compile_to_lambda({"type": "impossible"}, player=1)
    assert pred(StubState({})) is False
    assert pred(StubState({"Morph Ball": 1})) is False


def test_item_amount_1():
    ast = {"type": "item", "name": "Morph Ball", "amount": 1}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({})) is False
    assert pred(StubState({"Morph Ball": 1})) is True
    assert pred(StubState({"Morph Ball": 5})) is True


def test_item_amount_n():
    ast = {"type": "item", "name": "Energy Tank", "amount": 3}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({"Energy Tank": 2})) is False
    assert pred(StubState({"Energy Tank": 3})) is True
    assert pred(StubState({"Energy Tank": 7})) is True


def test_event_graph_mode_uses_can_reach_region():
    """In graph_mode, an event atom compiles to can_reach_region instead of
    state.has — events are pure-logic regions in the native graph model."""
    ast = {"type": "event", "name": "ShipPickup"}

    class RegionState:
        def __init__(self, reachable):
            self._reachable = reachable

        def can_reach_region(self, name, player):
            return name in self._reachable

    pred = compile_to_lambda(ast, player=1, graph_mode=True)
    assert pred(RegionState(set())) is False
    assert pred(RegionState({"Event:ShipPickup"})) is True


def test_trick_assumed_when_effective_level_meets_requirement():
    # v3: tricks are symbolic; the effective level map decides. IBJ@1 is
    # satisfied when the slot runs IBJ at level >= 1.
    ast = {"type": "trick", "name": "IBJ", "level": 1}
    assert compile_to_lambda(ast, player=1, trick_levels={"IBJ": 1})(StubState({})) is True
    assert compile_to_lambda(ast, player=1, trick_levels={"IBJ": 3})(StubState({})) is True


def test_trick_rejected_when_below_effective_level_or_disabled():
    ast = {"type": "trick", "name": "IBJ", "level": 2}
    # IBJ only at level 1 → a level-2 requirement is not assumed.
    assert compile_to_lambda(ast, player=1, trick_levels={"IBJ": 1})(StubState({})) is False
    # Disabled / absent from the map → never assumed.
    assert compile_to_lambda(ast, player=1, trick_levels={"IBJ": 0})(StubState({})) is False
    assert compile_to_lambda(ast, player=1)(StubState({})) is False


# ---- composites ----

def test_and_requires_all():
    ast = {"type": "and", "items": [
        {"type": "item", "name": "Morph Ball", "amount": 1},
        {"type": "item", "name": "Bomb", "amount": 1},
    ]}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({})) is False
    assert pred(StubState({"Morph Ball": 1})) is False
    assert pred(StubState({"Bomb": 1})) is False
    assert pred(StubState({"Morph Ball": 1, "Bomb": 1})) is True


def test_or_requires_any():
    ast = {"type": "or", "items": [
        {"type": "item", "name": "Bomb", "amount": 1},
        {"type": "item", "name": "Cross Bomb", "amount": 1},
        {"type": "item", "name": "Power Bomb", "amount": 1},
    ]}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({})) is False
    assert pred(StubState({"Bomb": 1})) is True
    assert pred(StubState({"Cross Bomb": 1})) is True
    assert pred(StubState({"Power Bomb": 1})) is True


def test_empty_and_is_trivial():
    pred = compile_to_lambda({"type": "and", "items": []}, player=1)
    assert pred(StubState({})) is True


def test_empty_or_is_impossible():
    pred = compile_to_lambda({"type": "or", "items": []}, player=1)
    assert pred(StubState({})) is False


def test_nested_and_or():
    """Power-Bomb-Tank-style: Morph AND bomb-access — exercises the
    closure-capture path (children list iterated)."""
    ast = {"type": "and", "items": [
        {"type": "item", "name": "Morph Ball", "amount": 1},
        {"type": "or", "items": [
            {"type": "item", "name": "Bomb", "amount": 1},
            {"type": "item", "name": "Cross Bomb", "amount": 1},
        ]},
    ]}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({"Morph Ball": 1})) is False
    assert pred(StubState({"Bomb": 1})) is False
    assert pred(StubState({"Morph Ball": 1, "Bomb": 1})) is True
    assert pred(StubState({"Morph Ball": 1, "Cross Bomb": 1})) is True


def test_player_isolation():
    """Predicate must call state.has with the player it was compiled
    for — guards against hardcoded player=1 etc."""
    ast = {"type": "item", "name": "Morph Ball", "amount": 1}
    pred = compile_to_lambda(ast, player=7)
    state = StubState({"Morph Ball": 1}, expected_player=7)
    assert pred(state) is True
    # Wrong-player state should AssertionError inside StubState.has
    wrong = StubState({"Morph Ball": 1}, expected_player=1)
    with pytest.raises(AssertionError):
        pred(wrong)


def test_loop_late_binding_safety():
    """If compile_to_lambda used a naked `for c in cs` capture, every
    lambda would close over the LAST c and this test would fail.
    Re-evaluate a list of 5 single-item rules and assert each acts
    independently."""
    items = ["Morph Ball", "Bomb", "Plasma Beam", "Missile Tank", "Power Bomb"]
    preds = [
        compile_to_lambda({"type": "item", "name": name, "amount": 1}, player=1)
        for name in items
    ]
    for name, pred in zip(items, preds):
        assert pred(StubState({name: 1})) is True, f"{name} should be True"
        assert pred(StubState({"Unrelated": 1})) is False, f"{name} should be False"


# ---- sum (v0.3 ammo counting) ----

def test_sum_below_threshold_is_false():
    """15 starting + 2*5=10 = 25 missiles, threshold 30 — fails."""
    ast = {"type": "sum",
           "terms": [{"name": "Missile Tank", "per_unit": 2},
                     {"name": "Missile+ Tank", "per_unit": 10}],
           "base": 15, "threshold": 30}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({"Missile Tank": 5})) is False


def test_sum_at_threshold_is_true():
    """15 + 2*7 + 10*0 = 29 missiles below 30; 15 + 2*8 = 31 above. Boundary
    crossed by adding one tank — exactly the boundary the AP solver gates on."""
    ast = {"type": "sum",
           "terms": [{"name": "Missile Tank", "per_unit": 2},
                     {"name": "Missile+ Tank", "per_unit": 10}],
           "base": 15, "threshold": 30}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({"Missile Tank": 7})) is False     # 15 + 14 = 29
    assert pred(StubState({"Missile Tank": 8})) is True      # 15 + 16 = 31


def test_sum_above_threshold_is_true():
    """Late game: 30 missile tanks + 5 missile+ = 15 + 60 + 50 = 125."""
    ast = {"type": "sum",
           "terms": [{"name": "Missile Tank", "per_unit": 2},
                     {"name": "Missile+ Tank", "per_unit": 10}],
           "base": 15, "threshold": 75}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({"Missile Tank": 30, "Missile+ Tank": 5})) is True


def test_sum_mixed_terms():
    """Missile+ Tanks pay 5x — mix them in and the threshold falls fast."""
    ast = {"type": "sum",
           "terms": [{"name": "Missile Tank", "per_unit": 2},
                     {"name": "Missile+ Tank", "per_unit": 10}],
           "base": 15, "threshold": 75}
    pred = compile_to_lambda(ast, player=1)
    # 15 + 2*0 + 10*6 = 75 — exactly threshold
    assert pred(StubState({"Missile+ Tank": 6})) is True
    # 15 + 2*0 + 10*5 = 65 — below
    assert pred(StubState({"Missile+ Tank": 5})) is False
    # 15 + 2*30 + 10*0 = 75 — exactly threshold via base term
    assert pred(StubState({"Missile Tank": 30})) is True


def test_sum_base_alone_satisfies():
    """A threshold ≤ base never needs collecting — fires immediately. Catches
    early-exit short-circuit (the loop returns True as soon as total ≥ thr)."""
    ast = {"type": "sum",
           "terms": [{"name": "Missile Tank", "per_unit": 2}],
           "base": 15, "threshold": 15}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({})) is True


def test_sum_power_bomb_shape():
    """Power bombs start at 0; need the launcher + tanks. The compiler wraps
    PBAmmo in an AND with the launcher item — this test exercises the inner
    sum only (launcher AND lives a level up). 0 + 2*3 = 6 PBs covers thr=5."""
    ast = {"type": "sum",
           "terms": [{"name": "Power Bomb", "per_unit": 2},
                     {"name": "Power Bomb Tank", "per_unit": 2}],
           "base": 0, "threshold": 5}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({})) is False
    assert pred(StubState({"Power Bomb Tank": 2})) is False   # 4 < 5
    assert pred(StubState({"Power Bomb": 1, "Power Bomb Tank": 2})) is True  # 6


# ---- sum option scaling (faithful v0.3 ammo) ----

def test_sum_ammo_amounts_override_base():
    """ammo_amounts['missile_base'] replaces the baked missile base (15).
    Lowering starting_missiles to 0 makes a thr=15 gate need real tanks."""
    ast = {"type": "sum",
           "terms": [{"name": "Missile Tank", "per_unit": 2}],
           "base": 15, "threshold": 15}
    # vanilla: base 15 alone meets thr 15.
    assert compile_to_lambda(ast, player=1)(StubState({})) is True
    # starting_missiles=0: base 0 → need 8 tanks at 2 each (16 >= 15).
    pred = compile_to_lambda(ast, player=1, ammo_amounts={"missile_base": 0})
    assert pred(StubState({})) is False
    assert pred(StubState({"Missile Tank": 7})) is False   # 14 < 15
    assert pred(StubState({"Missile Tank": 8})) is True    # 16 >= 15


def test_sum_ammo_amounts_override_per_unit():
    """ammo_amounts per-unit keys replace each term's baked yield. With
    missile_tank_ammo=5, one tank now adds 5 not 2."""
    ast = {"type": "sum",
           "terms": [{"name": "Missile Tank", "per_unit": 2},
                     {"name": "Missile+ Tank", "per_unit": 10}],
           "base": 15, "threshold": 30}
    pred = compile_to_lambda(ast, player=1, ammo_amounts={
        "missile_base": 0,
        "missile_tank_per_unit": 5,
        "missile_plus_tank_per_unit": 20,
    })
    assert pred(StubState({"Missile Tank": 5})) is False   # 25 < 30
    assert pred(StubState({"Missile Tank": 6})) is True    # 30
    assert pred(StubState({"Missile+ Tank": 2})) is True   # 40


def test_sum_ammo_amounts_zero_per_unit_makes_tanks_inert():
    """missile_tank_ammo=0 → tanks add nothing; only base satisfies the gate.
    Mirrors the faithful 'tanks give no capacity' difficulty config."""
    ast = {"type": "sum",
           "terms": [{"name": "Missile Tank", "per_unit": 2}],
           "base": 15, "threshold": 20}
    pred = compile_to_lambda(ast, player=1, ammo_amounts={
        "missile_base": 15, "missile_tank_per_unit": 0})
    assert pred(StubState({"Missile Tank": 99})) is False   # base 15 only


def test_sum_ammo_amounts_pb_launcher_per_unit():
    """Power Bomb launcher grant rides power_bomb_per_unit (=starting_power_bombs)
    and PB tanks ride power_bomb_tank_per_unit; base stays 0 so a tank without
    the launcher reads only tank capacity (the launcher AND lives a level up)."""
    ast = {"type": "sum",
           "terms": [{"name": "Power Bomb", "per_unit": 2},
                     {"name": "Power Bomb Tank", "per_unit": 2}],
           "base": 0, "threshold": 2}
    pred = compile_to_lambda(ast, player=1, ammo_amounts={
        "pb_base": 0, "power_bomb_per_unit": 2, "power_bomb_tank_per_unit": 1})
    assert pred(StubState({"Power Bomb": 1})) is True       # launcher grants 2
    assert pred(StubState({"Power Bomb Tank": 1})) is False  # 1 < 2
    assert pred(StubState({"Power Bomb Tank": 2})) is True   # 2


def test_sum_threshold_never_rescaled_by_amounts():
    """The route's raw demand (threshold) is invariant under ammo_amounts —
    only base/per_unit move. A thr=40 gate stays thr=40."""
    ast = {"type": "sum",
           "terms": [{"name": "Missile Tank", "per_unit": 2}],
           "base": 15, "threshold": 40}
    pred = compile_to_lambda(ast, player=1, ammo_amounts={
        "missile_base": 15, "missile_tank_per_unit": 2})
    # 15 + 12*2 = 39 < 40 ; 13 tanks = 41 >= 40
    assert pred(StubState({"Missile Tank": 12})) is False
    assert pred(StubState({"Missile Tank": 13})) is True


# ---- damage_threshold (v0.3 HP budget) ----

def test_damage_threshold_suit_shortcircuits():
    """Any listed suit satisfies the predicate regardless of HP — heat/cold
    rooms are gated by Varia/Gravity in the Randovania database, so a suited
    player should pass even with zero E-Tanks."""
    ast = {"type": "damage_threshold",
           "suit_options": ["Varia Suit", "Gravity Suit"],
           "hp_needed": 500}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({"Varia Suit": 1})) is True
    assert pred(StubState({"Gravity Suit": 1})) is True
    # Both suits = also true.
    assert pred(StubState({"Varia Suit": 1, "Gravity Suit": 1})) is True


def test_damage_threshold_hp_budget_path():
    """Without suits: 99 base + 100*ETank + 25*EPart must cover hp_needed.
    Boundary at hp=200: 1 ETank = 99 + 100 = 199 fails; 2 ETank = 299 passes."""
    ast = {"type": "damage_threshold",
           "suit_options": ["Varia Suit"],
           "hp_needed": 200}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({})) is False                       # 99 < 200
    assert pred(StubState({"Energy Tank": 1})) is False       # 199 < 200
    assert pred(StubState({"Energy Tank": 2})) is True        # 299 >= 200
    # Energy Parts mix in at 25 each — 99 + 100 + 25*4 = 199 + 100 = 299; check
    # the lower-density path that uses Parts to finish.
    assert pred(StubState({"Energy Tank": 1, "Energy Part": 4})) is True  # 299


def test_damage_threshold_empty_suit_options():
    """Generic Damage requirements emit damage_threshold with empty suits —
    only the HP budget can satisfy. No suit shortcut."""
    ast = {"type": "damage_threshold",
           "suit_options": [],
           "hp_needed": 150}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({"Varia Suit": 1, "Gravity Suit": 1})) is False  # suits ignored
    assert pred(StubState({"Energy Tank": 1})) is True        # 199 >= 150


def test_damage_threshold_zero_hp_always_true():
    """Edge case: hp_needed=0 means the rule trivially holds (no damage to
    survive). Starting HP 99 ≥ 0."""
    ast = {"type": "damage_threshold",
           "suit_options": [],
           "hp_needed": 0}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({})) is True


def test_damage_threshold_honors_energy_per_tank():
    """Faithful v0.3: the HP budget scales with energy_per_tank. At a lower
    per-tank value the same gate needs more tanks; at a higher value, fewer.
    Base 99 is fixed. A part is worth 1/4 of a tank."""
    ast = {"type": "damage_threshold", "suit_options": [], "hp_needed": 400}
    # Default 100/tank: 99 + 300 = 399 < 400 fails; +1 part (25) = 424 passes.
    p100 = compile_to_lambda(ast, player=1)
    assert p100(StubState({"Energy Tank": 3})) is False
    assert p100(StubState({"Energy Tank": 3, "Energy Part": 1})) is True
    # 50/tank: each tank worth 50, each part 12.5. 99 + 50*6 = 399 < 400;
    # 7 tanks = 99 + 350 = 449 passes.
    p50 = compile_to_lambda(ast, player=1, energy_per_tank=50)
    assert p50(StubState({"Energy Tank": 6})) is False
    assert p50(StubState({"Energy Tank": 7})) is True
    # 200/tank: 99 + 200*2 = 499 >= 400 with just 2 tanks.
    p200 = compile_to_lambda(ast, player=1, energy_per_tank=200)
    assert p200(StubState({"Energy Tank": 2})) is True
    assert p200(StubState({"Energy Tank": 1})) is False  # 299 < 400


def test_damage_threshold_part_fraction_with_low_per_tank():
    """A part is exactly energy_per_tank/4 — verify fractional accumulation
    (12.5 at 50/tank) sums correctly rather than truncating per-part."""
    ast = {"type": "damage_threshold", "suit_options": [], "hp_needed": 124}
    pred = compile_to_lambda(ast, player=1, energy_per_tank=50)
    # 99 + 12.5*2 = 124.0 >= 124 (would fail if parts truncated to 12 each: 123)
    assert pred(StubState({"Energy Part": 2})) is True
    assert pred(StubState({"Energy Part": 1})) is False  # 111.5 < 124


def test_damage_threshold_in_graph():
    """Live check: logic_graph.json contains damage_threshold and sum nodes —
    the graph emitter is actually writing them."""
    import json
    path = ROOT / "data" / "logic_graph.json"
    if not path.exists():
        import pytest
        pytest.skip("logic_graph.json not yet generated (run extract_dread_rules.py)")
    text = path.read_text()
    assert '"damage_threshold"' in text, "no damage_threshold nodes in graph"
    assert '"sum"' in text, "no sum nodes in graph"


def test_misc_door_lock_off_no_door_rando():
    """misc DoorLocks negate=True ('NOT DoorLocks') is True when door rando is off."""
    ast = {"type": "misc", "name": "DoorLocks", "negate": True}
    pred = compile_to_lambda(ast, player=1, door_lock_rando=False)
    assert pred(StubState({})) is True


def test_misc_door_lock_off_with_door_rando():
    """misc DoorLocks negate=True ('NOT DoorLocks') is False when door rando is on."""
    ast = {"type": "misc", "name": "DoorLocks", "negate": True}
    pred = compile_to_lambda(ast, player=1, door_lock_rando=True)
    assert pred(StubState({})) is False


def test_misc_door_lock_on_with_door_rando():
    """misc DoorLocks negate=False ('DoorLocks') is True when door rando is on."""
    ast = {"type": "misc", "name": "DoorLocks", "negate": False}
    pred = compile_to_lambda(ast, player=1, door_lock_rando=True)
    assert pred(StubState({})) is True


def test_misc_propagates_through_and():
    """door_lock_rando propagates through AND nodes."""
    ast = {"type": "and", "items": [
        {"type": "item", "name": "Slide", "amount": 1},
        {"type": "misc", "name": "DoorLocks", "negate": True},
    ]}
    with_rando = compile_to_lambda(ast, player=1, door_lock_rando=True)
    without_rando = compile_to_lambda(ast, player=1, door_lock_rando=False)
    s = StubState({"Slide": 1})
    assert without_rando(s) is True   # Slide + NOT DoorLocks holds
    assert with_rando(s) is False     # NOT DoorLocks fails when door rando is on


def test_thermal_device_rule_requires_morph_with_door_rando():
    """Artaria Thermal Device: Start Point → upper door requires Morph Ball when
    door rando is active (NOT DoorLocks is False).

    Modelled from the raw RDV node: Missiles AND (Morph OR (NOT DoorLocks AND ...))
    With door rando ON the second disjunct collapses to impossible, leaving just
    Missiles AND Morph Ball — so the fill cannot place Morph Ball at that location
    (it would be a circular dependency)."""
    missiles_and_morph_or_no_door_lock = {
        "type": "and", "items": [
            {"type": "item", "name": "Missile Tank", "amount": 1},
            {"type": "or", "items": [
                {"type": "item", "name": "Morph Ball", "amount": 1},
                {"type": "and", "items": [
                    {"type": "misc", "name": "DoorLocks", "negate": True},
                    {"type": "item", "name": "Missile Tank", "amount": 2},
                ]},
            ]},
        ],
    }
    with_rando = compile_to_lambda(missiles_and_morph_or_no_door_lock,
                                   player=1, door_lock_rando=True)
    without_rando = compile_to_lambda(missiles_and_morph_or_no_door_lock,
                                      player=1, door_lock_rando=False)
    missiles = StubState({"Missile Tank": 15})
    missiles_and_morph = StubState({"Missile Tank": 15, "Morph Ball": 1})

    # Without door rando: just missiles suffice (NOT DoorLocks path is open).
    assert without_rando(missiles) is True
    # With door rando ON: Morph Ball required.
    assert with_rando(missiles) is False
    assert with_rando(missiles_and_morph) is True
