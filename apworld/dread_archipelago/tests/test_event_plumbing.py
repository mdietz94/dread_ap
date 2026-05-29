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

from dread_archipelago.Rules import compile_to_lambda  # noqa: E402


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

def test_victory_condition_is_event_ship(compiled):
    """The canonical Dread goal is reaching the ship event. If this
    changes, generation will accept incomplete seeds — surface loudly."""
    vc = compiled["victory_condition"]
    assert vc.get("type") == "event"
    assert vc.get("name") == "Ship"


def test_victory_condition_predicate_requires_event_ship_item():
    """Confirm the wire: compiled victory_condition + compile_to_lambda
    + state.has should mean "completion requires Event: Ship"."""
    vc = {"type": "event", "name": "Ship"}
    pred = compile_to_lambda(vc, player=1)
    assert pred(State({})) is False
    assert pred(State({"Event: Ship": 1})) is True


# ---- M1 under-constraint regression ----

def test_burenia_event_gated_rule_still_requires_speed_booster():
    """Under M1, a rule like Burenia: missiletankplus_001 had a
    disjunct ``[BureniaHubMagnetPlatform AND BureniaPrepareSpeedSave
    AND ... AND Speed Booster]`` that always passed because events
    were trivial. The disjunct then over-permissively suggested the
    pickup was reachable without Speed Booster, as long as *some*
    other disjunct's items were absent too. Under M2 the event items
    are real, so even with the events held, removing Speed Booster
    from that disjunct prevents it from satisfying.

    This locks the M2 wire: a state holding every event item but
    missing Speed Booster (and the items used by every other disjunct)
    must NOT satisfy the rule. If it does, the event branch silently
    short-circuited to True somewhere."""
    raw = json.loads((ROOT / "data" / "compiled_rules.json").read_text())
    ast = raw["rules"]["Burenia: missiletankplus_001"]
    pred = compile_to_lambda(ast, player=1)

    # Pull in every event item so only the "remove Speed Booster"
    # constraint is what blocks satisfaction.
    only_events = {f"Event: {e['name']}": 1 for e in raw["events"]}
    # Plasma + every event but NOT the other singletons (Grapple,
    # Gravity, Space Jump, Spider Magnet, Spin Boost) and NOT
    # Bomb+Morph (the alternate disjunct), and NOT Speed Booster.
    state = State({**only_events, "Plasma Beam": 1})
    assert not pred(state), \
        ("rule must require Speed Booster on the event-gated branch "
         "after M2 — events alone shouldn't suffice")
    # Adding Speed Booster lets the event-gated disjunct satisfy.
    state_with_speed = State({**only_events, "Plasma Beam": 1, "Speed Booster": 1})
    assert pred(state_with_speed), \
        "event-gated disjunct should satisfy with all events + Plasma + Speed"
