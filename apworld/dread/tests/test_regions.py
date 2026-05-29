"""Gate B regression coverage for cross-region access (region_access).

region_access gates Menu→region on the AP side. It MUST be item-only (no
event/trick/damage atoms) so it never deadlocks the goal, the start region
must be Trivial, and every region must be reachable with a full inventory.
These run without an Archipelago install.
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
def compiled():
    return json.loads((DATA / "compiled_rules.json").read_text())


@pytest.fixture(scope="module")
def region_names():
    return [e["name"] for e in json.loads((DATA / "regions.json").read_text())]


class State:
    def __init__(self, inventory):
        self.inventory = inventory

    def has(self, name, _player, count=1):
        return self.inventory.get(name, 0) >= count


def test_region_access_present_for_every_region(compiled, region_names):
    ra = compiled["region_access"]
    assert set(ra.keys()) == set(region_names), (
        f"region_access keys {set(ra)} != regions {set(region_names)}"
    )


def test_start_region_is_trivial(compiled):
    assert compiled["region_access"]["Artaria"] == {"type": "trivial"}


def test_region_access_is_item_only(compiled):
    """No event/trick/damage atoms — region gating must bootstrap from items
    alone (events are locked progression that would deadlock the goal)."""
    def walk(ast):
        t = ast.get("type")
        assert t not in ("event", "trick", "damage"), f"region_access has {t} node: {ast}"
        for c in ast.get("items", []):
            walk(c)
    for rule in compiled["region_access"].values():
        walk(rule)


def test_region_access_is_a_star(compiled):
    """The forward resolver inlines cross-region cost into each per-pickup
    rule, so region_access is intentionally a plain star (every region Trivial
    from Menu). The real gating lives in the per-pickup rules below."""
    assert all(rule == {"type": "trivial"}
               for rule in compiled["region_access"].values())


def test_pickup_rules_carry_real_item_gating(compiled):
    """Per-pickup rules are global + item-only: many require cross-region items
    (e.g. deep pickups need Charge Beam / suits), so they're not all trivial."""
    def items_in(ast, acc):
        if ast.get("type") == "item":
            acc.add(ast["name"])
        for c in ast.get("items", []):
            items_in(c, acc)
    non_trivial = [loc for loc, r in compiled["rules"].items()
                   if r != {"type": "trivial"}]
    assert len(non_trivial) >= 100, "expected most pickups to be item-gated"
    # No event/trick/damage atoms — rules are pure item logic now.
    for r in compiled["rules"].values():
        def walk(a):
            assert a.get("type") not in ("event", "trick", "damage")
            for c in a.get("items", []):
                walk(c)
        walk(r)


def test_logic_required_items_are_progression(compiled):
    """Every item the rules / victory reference MUST be collected by AP's logic
    sweep — i.e. classified progression(_skip_balancing). A logic item left as
    'filler'/'useful' (the Missile Tank bug) makes its gated locations
    unreachable and breaks accessibility=items/full."""
    cls = {i["name"]: i["classification"]
           for i in json.loads((DATA / "items.json").read_text())}
    refs: set = set()

    def walk(a):
        if a.get("type") == "item":
            refs.add(a["name"])
        for c in a.get("items", []):
            walk(c)
    for r in compiled["rules"].values():
        walk(r)
    walk(compiled["victory_condition"])
    bad = [n for n in sorted(refs)
           if cls.get(n) not in ("progression", "progression_skip_balancing")]
    assert not bad, f"logic-required items not progression: {bad}"


def test_all_regions_reachable_with_full_inventory(compiled):
    items = json.loads((DATA / "items.json").read_text())
    inv = {it["name"]: 99 for it in items}
    state = State(inv)
    for region, rule in compiled["region_access"].items():
        pred = compile_to_lambda(rule, 1)
        assert pred(state), f"{region} unreachable even with everything"
