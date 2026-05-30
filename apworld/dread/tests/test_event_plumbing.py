"""Gate A regression coverage for the M2 event-as-item plumbing.

These tests pin the contract between the compiler (which emits the
``events`` list in ``compiled_rules.json``) and the apworld's
generation-time consumers (Rules.py + World.py). If any of these
fail, generation will silently produce under-constrained or
unsolvable seeds.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.Rules import compile_to_lambda  # noqa: E402


@pytest.fixture(scope="module")
def compiled():
    return json.loads((ROOT / "data" / "compiled_rules.json").read_text())


@pytest.fixture(scope="module")
def items():
    return json.loads((ROOT / "data" / "items.json").read_text())


@pytest.fixture(scope="module")
def locations():
    return json.loads((ROOT / "data" / "locations.json").read_text())


class State:
    """Minimal CollectionState stand-in."""

    def __init__(self, inventory: dict[str, int]):
        self.inventory = inventory

    def has(self, name: str, _player: int, count: int = 1) -> bool:
        return self.inventory.get(name, 0) >= count

    def count(self, name: str, _player: int) -> int:
        return self.inventory.get(name, 0)


# ---- structural invariants ----

def test_events_list_is_non_empty(compiled):
    """The whole point of M2 plumbing is that events become real
    items — an empty events list means the compiler regressed or
    we're reading the wrong artifact."""
    assert compiled["events"], "compiled_rules.json has no events"


def test_every_event_has_name_region_and_rule(compiled):
    for ev in compiled["events"]:
        assert ev["name"], f"event missing name: {ev}"
        assert "region" in ev, f"event missing region: {ev}"
        assert "rule" in ev and ev["rule"].get("type"), \
            f"event missing or malformed rule: {ev}"


def test_event_ap_ids_disjoint_from_existing_ranges(compiled, items, locations):
    """Append-only AP IDs: event item IDs must not collide with the
    existing pickup item IDs, event location IDs likewise. This pins
    the M2 promise that adding events doesn't shift existing seeds."""
    item_ids = {it["ap_id"] for it in items
                if not it["name"].startswith("Event: ")}
    loc_ids = {l["ap_id"] for l in locations
               if l.get("pickup_type") != "event"}
    event_item_ids = {ev["item_ap_id"] for ev in compiled["events"]}
    event_loc_ids = {ev["location_ap_id"] for ev in compiled["events"]}
    assert event_item_ids.isdisjoint(item_ids), \
        "event item IDs collide with pickup item IDs"
    assert event_loc_ids.isdisjoint(loc_ids), \
        "event location IDs collide with pickup location IDs"
    assert event_item_ids.isdisjoint(event_loc_ids), \
        "event item IDs collide with event location IDs"


def test_event_ap_ids_unique(compiled):
    item_ids = [ev["item_ap_id"] for ev in compiled["events"]]
    loc_ids = [ev["location_ap_id"] for ev in compiled["events"]]
    assert len(item_ids) == len(set(item_ids)), "duplicate event item IDs"
    assert len(loc_ids) == len(set(loc_ids)), "duplicate event location IDs"


def test_events_sorted_by_name(compiled):
    """Sorted ordering is a stability invariant: appending or renaming
    an event shouldn't reshuffle the AP IDs of unrelated events."""
    names = [ev["name"] for ev in compiled["events"]]
    assert names == sorted(names), \
        "events list is not sorted by name (breaks AP ID stability)"


def test_every_event_has_a_pool_item(compiled, items):
    """Each compiled event must have a matching item entry in
    items.json — World.create_items relies on the name lookup."""
    item_names = {it["name"] for it in items}
    missing = [
        f"Event: {ev['name']}" for ev in compiled["events"]
        if f"Event: {ev['name']}" not in item_names
    ]
    assert not missing, f"events without item entries: {missing[:5]}"


def test_every_event_has_a_pool_location(compiled, locations):
    loc_names = {l["name"] for l in locations}
    missing = [
        f"Event: {ev['name']}" for ev in compiled["events"]
        if f"Event: {ev['name']}" not in loc_names
    ]
    assert not missing, f"events without location entries: {missing[:5]}"


# ---- victory condition ----

def test_victory_condition_is_item_only(compiled):
    """The goal is reaching the ship. Events are inlined into item-only rules,
    so victory_condition is now an item-only reach rule (the Ship event's cost
    folded into items) — no event atoms. That's what lets AP's item sweep
    verify the goal under accessibility=items/full."""
    def has_event(ast):
        if ast.get("type") == "event":
            return True
        return any(has_event(c) for c in ast.get("items", []))
    assert not has_event(compiled["victory_condition"])


def test_victory_condition_predicate_requires_event_ship_item():
    """Confirm the wire: compiled victory_condition + compile_to_lambda
    + state.has should mean "completion requires Event: Ship"."""
    vc = {"type": "event", "name": "Ship"}
    pred = compile_to_lambda(vc, player=1)
    assert pred(State({})) is False
    assert pred(State({"Event: Ship": 1})) is True


# ---- item-only inlined rules ----

def test_burenia_pickup_is_item_only_and_gated():
    """Events are inlined, so Burenia: missiletankplus_001 is an item-only rule
    (no event atoms), gated (not trivially reachable), and satisfied by a full
    loadout. (Pre-inlining this pickup had an event-gated disjunct; that cost is
    now folded into its item requirements.)"""
    raw = json.loads((ROOT / "data" / "compiled_rules.json").read_text())
    ast = raw["rules"]["Burenia: missiletankplus_001"]

    def has_event(a):
        if a.get("type") == "event":
            return True
        return any(has_event(c) for c in a.get("items", []))
    assert not has_event(ast), "rule must be item-only after inlining"

    pred = compile_to_lambda(ast, player=1)
    assert pred(State({})) is False, "pickup must not be trivially reachable"
    full = {i["name"]: 99 for i in json.loads((ROOT / "data" / "items.json").read_text())}
    assert pred(State(full)) is True, "pickup must be reachable with a full loadout"
