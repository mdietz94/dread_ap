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


def test_events_sorted_by_name(compiled):
    """Sorted ordering is a stability invariant for the diagnostic events
    list (so re-bakes produce byte-stable artifacts)."""
    names = [ev["name"] for ev in compiled["events"]]
    assert names == sorted(names), "events list is not sorted by name"


def test_events_are_not_ap_items(compiled, items):
    """Events are inlined into the per-pickup rules; they are NOT AP items.
    A leftover `Event: ...` item entry would mean events leak into the pool
    (the pre-Option-A bug: events placed as locked items at locked locations
    that AP's fill couldn't reach, breaking accessibility checks)."""
    leaked = [it["name"] for it in items if it["name"].startswith("Event: ")]
    assert not leaked, f"event items leaked into items.json: {leaked[:5]}"


def test_events_are_not_ap_locations(compiled, locations):
    """Events are inlined into the per-pickup rules; they are NOT AP
    locations. Anything with name `Event: ...` or pickup_type=event is a
    regression — see test_events_are_not_ap_items for the failure mode."""
    by_name = [l["name"] for l in locations if l["name"].startswith("Event: ")]
    by_type = [l["name"] for l in locations if l.get("pickup_type") == "event"]
    assert not by_name, f"event locations leaked into locations.json: {by_name[:5]}"
    assert not by_type, f"locations tagged pickup_type=event: {by_type[:5]}"


def test_events_carry_no_ap_ids(compiled):
    """The compiled events list documents what the forward resolver inlined.
    Event entries used to carry item_ap_id / location_ap_id used by the
    removed scripts/append_event_data.py — they are dead bookkeeping now
    and must not return (else they invite re-introducing the leak)."""
    for ev in compiled["events"]:
        assert "item_ap_id" not in ev, \
            f"event {ev['name']!r} carries stale item_ap_id"
        assert "location_ap_id" not in ev, \
            f"event {ev['name']!r} carries stale location_ap_id"


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
    """Events are inlined, so Burenia: Main Hub Tower Middle - Missile+ Tank is
    an item-only rule (no event atoms), gated (not trivially reachable), and
    satisfied by a full loadout. (Pre-inlining this pickup had an event-gated
    disjunct; that cost is now folded into its item requirements.)"""
    raw = json.loads((ROOT / "data" / "compiled_rules.json").read_text())
    ast = raw["rules"]["Burenia: Main Hub Tower Middle - Missile+ Tank"]

    def has_event(a):
        if a.get("type") == "event":
            return True
        return any(has_event(c) for c in a.get("items", []))
    assert not has_event(ast), "rule must be item-only after inlining"

    pred = compile_to_lambda(ast, player=1)
    assert pred(State({})) is False, "pickup must not be trivially reachable"
    full = {i["name"]: 99 for i in json.loads((ROOT / "data" / "items.json").read_text())}
    assert pred(State(full)) is True, "pickup must be reachable with a full loadout"
