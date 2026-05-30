"""Regression coverage for the Metroid DNA collection goal.

Pins the data contract (12 distinct DNA items mapping to
ITEM_RANDO_ARTIFACT_k, with append-only IDs disjoint from base + event
ranges) and the goal-predicate composition (base victory AND has each of
the N required DNA). Runs without an Archipelago install.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.Rules import compile_to_lambda  # noqa: E402

DATA = ROOT / "data"


@pytest.fixture(scope="module")
def items():
    return json.loads((DATA / "items.json").read_text())


@pytest.fixture(scope="module")
def compiled():
    return json.loads((DATA / "compiled_rules.json").read_text())


class State:
    def __init__(self, inventory):
        self.inventory = inventory

    def has(self, name, _player, count=1):
        return self.inventory.get(name, 0) >= count

    def count(self, name, _player):
        return self.inventory.get(name, 0)


def test_twelve_dna_items_exist(items):
    names = {it["name"] for it in items}
    for k in range(1, 13):
        assert f"Metroid DNA {k}" in names, f"missing Metroid DNA {k}"


def test_dna_items_map_to_distinct_artifacts(items):
    by_name = {it["name"]: it for it in items}
    for k in range(1, 13):
        it = by_name[f"Metroid DNA {k}"]
        assert it["patcher_item_id"] == f"ITEM_RANDO_ARTIFACT_{k}"
        assert it["classification"] == "progression"
        assert it["quantity"] == 1


def test_dna_ids_disjoint_and_after_events(items, compiled):
    by_name = {it["name"]: it for it in items}
    dna_ids = {by_name[f"Metroid DNA {k}"]["ap_id"] for k in range(1, 13)}
    base_ids = {it["ap_id"] for it in items
                if not it["name"].startswith("Event: ")
                and not it["name"].startswith("Metroid DNA")}
    event_ids = {it["ap_id"] for it in items if it["name"].startswith("Event: ")}
    assert not (dna_ids & base_ids)
    assert not (dna_ids & event_ids)
    if event_ids:
        assert min(dna_ids) > max(event_ids), "DNA must be appended AFTER events"


def _goal(base_ast, n_dna, player=1):
    """Replicate Rules.set_rules' completion wiring for N>0."""
    base = compile_to_lambda(base_ast, player)
    if n_dna <= 0:
        return base
    names = tuple(f"Metroid DNA {k}" for k in range(1, n_dna + 1))
    return lambda state, b=base, ns=names: b(state) and all(state.has(x, player) for x in ns)


def test_goal_requires_all_n_dna():
    # Isolate the DNA-gating: completion = base AND has each of the N DNA.
    # (The real base victory is now an item-only Ship-reach rule — events are
    # inlined — so test the AND-ing with a trivial base.)
    pred = _goal({"type": "trivial"}, 3)
    assert pred(State({"Metroid DNA 1": 1, "Metroid DNA 2": 1})) is False
    assert pred(State({"Metroid DNA 1": 1, "Metroid DNA 2": 1, "Metroid DNA 3": 1})) is True


def test_goal_n0_is_bare_victory():
    # N=0 → completion is exactly the base victory (no DNA gate).
    pred = _goal({"type": "trivial"}, 0)
    assert pred(State({})) is True
    # A non-trivial base is honored.
    pred2 = _goal({"type": "item", "name": "Charge Beam", "amount": 1}, 0)
    assert pred2(State({})) is False
    assert pred2(State({"Charge Beam": 1})) is True


def test_victory_condition_is_item_only(compiled):
    """Events are inlined, so the goal is an item-only reach rule (no event
    atoms) — that's what lets AP's item sweep verify the goal."""
    def has_event(ast):
        if ast.get("type") == "event":
            return True
        return any(has_event(c) for c in ast.get("items", []))
    assert not has_event(compiled["victory_condition"])
