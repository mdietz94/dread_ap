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

def test_compiled_elun_energy_tank_rule_has_expected_items():
    """Sanity that the compiled JSON loads and produces a predicate
    that respects the wiki: requires Morph Ball + Plasma Beam."""
    import json
    raw = json.loads((ROOT / "data" / "compiled_rules.json").read_text())
    rules = raw["rules"]
    assert "Elun: energytank_000" in rules
    pred = compile_to_lambda(rules["Elun: energytank_000"], player=1)
    # Empty inventory: not reachable
    assert pred(StubState({})) is False
    # Without Plasma Beam: not reachable (only entry to AR Station)
    assert pred(StubState({"Morph Ball": 1, "Bomb": 1})) is False
    # With Plasma + Morph + Bomb: reachable
    state = StubState({"Morph Ball": 1, "Bomb": 1, "Plasma Beam": 1})
    assert pred(state) is True
