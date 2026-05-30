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


def test_event_branch_consults_state():
    """M2 semantics: events resolve via ``state.has('Event: <name>',
    player)`` against the locked event item. M1 returned _const_true;
    that behavior is now wrong — under-constraining 62% of compiled
    rules. This test pins the M2 behavior so a future refactor can't
    silently undo the wiring."""
    ast = {"type": "event", "name": "ShipPickup"}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({})) is False
    assert pred(StubState({"Event: ShipPickup": 1})) is True
    # The bare event-name (without the "Event: " prefix) must NOT
    # satisfy the predicate — that would mean the wrong item is being
    # consulted.
    assert pred(StubState({"ShipPickup": 1})) is False


def test_trick_level_1_is_trivially_true():
    ast = {"type": "trick", "name": "IBJ", "level": 1}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({})) is True


def test_trick_level_2_is_impossible():
    ast = {"type": "trick", "name": "IBJ", "level": 2}
    pred = compile_to_lambda(ast, player=1)
    assert pred(StubState({})) is False


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


# ---- complete-rule sanity: integrates AST building ----

def test_compiled_elun_energy_tank_rule_is_item_gated():
    """Sanity that the compiled JSON loads and produces an item-only predicate
    that is gated (not trivial) and satisfied by a full loadout. (The global
    forward-resolver rule carries the cross-region cost, so the old tight
    Morph+Plasma-only assertion no longer applies.)"""
    import json
    raw = json.loads((ROOT / "data" / "compiled_rules.json").read_text())
    rules = raw["rules"]
    assert "Elun: energytank_000" in rules
    pred = compile_to_lambda(rules["Elun: energytank_000"], player=1)
    assert pred(StubState({})) is False  # gated, not trivial
    full = {n: 99 for n in (
        "Morph Ball", "Bomb", "Cross Bomb", "Charge Beam", "Wide Beam",
        "Plasma Beam", "Wave Beam", "Diffusion Beam", "Grapple Beam",
        "Spider Magnet", "Speed Booster", "Space Jump", "Spin Boost",
        "Screw Attack", "Varia Suit", "Gravity Suit", "Flash Shift",
        "Phantom Cloak", "Power Bomb", "Missile Tank", "Missile+ Tank",
        "Storm Missile", "Ice Missile", "Slide", "Pulse Radar",
        "Flash Shift Upgrade", "Speed Booster Upgrade")}
    assert pred(StubState(full)) is True


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


def test_damage_threshold_in_real_rules():
    """Live check: at least one location's compiled rule contains a
    damage_threshold node — the compiler is actually emitting them."""
    import json
    raw = json.loads((ROOT / "data" / "compiled_rules.json").read_text())
    text = json.dumps(raw)
    assert '"damage_threshold"' in text, "no damage_threshold nodes in compiled output"
    assert '"sum"' in text, "no sum nodes in compiled output"
