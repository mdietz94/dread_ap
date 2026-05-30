"""Sanity tests on rules across all 9 compiled regions.

Per-region hand-verified assertions are scoped tight on purpose — the
compiler operates on each area in isolation (cross-region access is
M2), so deep-in-an-area pickups can have rules that include items
from elsewhere as part of the BFS path. We only assert what's
locally true: (a) every actor pickup has SOME rule, (b) a
fully-equipped player can reach everything, (c) a handful of
near-spawn pickups behave as expected per the Randovania ``*.txt``
files."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.Rules import compile_to_lambda  # noqa: E402


@pytest.fixture(scope="module")
def rules():
    raw = json.loads((ROOT / "data" / "compiled_rules.json").read_text())
    return raw["rules"]


@pytest.fixture(scope="module")
def locations():
    return json.loads((ROOT / "data" / "locations.json").read_text())


class State:
    """Minimal CollectionState stand-in."""
    def __init__(self, items: dict[str, int]):
        self.items = items

    def has(self, name: str, _player: int, count: int = 1) -> bool:
        return self.items.get(name, 0) >= count

    def count(self, name: str, _player: int) -> int:
        return self.items.get(name, 0)


# Late-game state: every progression + useful item the v0.1 pool ships.
# Post-M2 it also has to include every event item, since events are now
# real AP items the predicate consults via state.has("Event: <name>").
# Without them the rules that gate on events stay un-satisfied and the
# "late-game loadout reaches everything" invariant fails. Event names
# are derived from compiled_rules.json so this stays in sync with the
# compiler.
def _all_event_items() -> dict[str, int]:
    raw = json.loads(
        (ROOT / "data" / "compiled_rules.json").read_text()
    )
    return {f"Event: {e['name']}": 1 for e in raw.get("events", [])}


LATE_GAME = {
    "Morph Ball": 1, "Bomb": 1, "Cross Bomb": 1, "Power Bomb": 1,
    "Charge Beam": 1, "Wide Beam": 1, "Plasma Beam": 1, "Wave Beam": 1,
    "Grapple Beam": 1, "Diffusion Beam": 1, "Ice Missile": 1, "Storm Missile": 1,
    "Missile Tank": 5, "Power Bomb Tank": 2, "Energy Tank": 5,
    "Varia Suit": 1, "Gravity Suit": 1, "Phantom Cloak": 1,
    "Flash Shift": 1, "Pulse Radar": 1, "Speed Booster": 1,
    "Spider Magnet": 1, "Spin Boost": 1, "Space Jump": 1, "Screw Attack": 1,
    "Slide": 1, "Missile+ Tank": 1,
    "Flash Shift Upgrade": 1, "Speed Booster Upgrade": 1, "Energy Part": 1,
    **_all_event_items(),
}

ALL_REGIONS = ("Artaria", "Burenia", "Cataris", "Dairon",
               "Elun", "Ferenia", "Ghavoran", "Hanubia")


# ---- coverage ----

def test_every_actor_pickup_has_a_rule(rules, locations):
    """All 137 actor pickups must appear in compiled_rules.json.
    Non-actor pickups (12 boss/EMMI/cutscene) intentionally don't —
    those handle progression via their own callback hooks."""
    actor_locs = [l for l in locations if l["pickup_type"] == "actor"]
    missing = [l["name"] for l in actor_locs if l["name"] not in rules]
    assert not missing, f"actor pickups without rules: {missing}"


@pytest.mark.parametrize("region", ALL_REGIONS)
def test_no_pickup_is_impossible(rules, region):
    """A `impossible` rule means the compiler found zero paths from
    the area's entries to the pickup. Every vanilla Dread pickup IS
    reachable from somewhere — if a region shows impossible, either
    the DFS gave up early or a trick gate stuck."""
    region_rules = [(k, v) for k, v in rules.items() if k.startswith(f"{region}:")]
    impossible = [k for k, v in region_rules if v.get("type") == "impossible"]
    assert not impossible, f"{region} pickups marked impossible: {impossible}"


@pytest.mark.parametrize("region", ALL_REGIONS)
def test_late_game_loadout_reaches_every_pickup_in_region(rules, region):
    """Sanity: a fully-equipped player should be able to reach
    everything in every region. If this fails for a pickup, the
    compiler over-constrained (or one of our late-game items is named
    differently than the pickup rule expects)."""
    state = State(LATE_GAME)
    region_rules = [(k, v) for k, v in rules.items() if k.startswith(f"{region}:")]
    unreachable = []
    for name, ast in region_rules:
        pred = compile_to_lambda(ast, player=1)
        if not pred(state):
            unreachable.append(name)
    assert not unreachable, \
        f"{region}: late-game loadout cannot reach {unreachable}"


# ---- hand-verified per-region assertions ----

def test_artaria_invisible_corpius_missile_reachable_with_late_game(rules):
    """item_missiletank_000 in 'Invisible Corpius Room'. Under the global
    item-only (forward-resolver) rules it carries the real cross-region cost
    rather than being trivial, but a full loadout must reach it."""
    ast = rules["Artaria: missiletank_000"]
    pred = compile_to_lambda(ast, player=1)
    assert pred(State(LATE_GAME)), \
        "Invisible Corpius missile should be reachable with a full loadout"


def test_artaria_varia_suit_requires_varia_or_workaround(rules):
    """The Varia Suit pickup is itself in a heated area, so the
    canonical path requires Varia Suit (self-loop, harmless to AP).
    Tested as 'late-game reaches it'. Don't assert specific blockers
    since the area-isolated compile may pick odd alternate paths."""
    ast = rules["Artaria: VARIA_GEN_001"]
    pred = compile_to_lambda(ast, player=1)
    assert pred(State(LATE_GAME)), "late-game must reach Varia pickup"
    # Should NOT be trivially reachable without anything
    assert not pred(State({})), \
        "Varia Suit pickup must not compile as trivially reachable"


def test_dairon_bomb_pickup_is_item_gated(rules):
    """Dairon's Bomb pickup is in 'Bomb Room'. Events are inlined into the
    item-only rule (their cost folded into items), so it's no longer trivially
    reachable and a full loadout reaches it."""
    ast = rules["Dairon: bomb"]
    pred = compile_to_lambda(ast, player=1)
    assert not pred(State({})), \
        "Dairon Bomb pickup should NOT be trivially reachable"
    assert pred(State(LATE_GAME)), \
        "Dairon Bomb pickup should be reachable with a full loadout"


def test_event_gated_rules_still_satisfy_with_late_game(rules):
    """Post-M2: event references in rules go through state.has(
    'Event: <name>', player). The LATE_GAME fixture now contains every
    event item, so a "fully equipped" player still reaches every
    event-gated pickup. If this test starts failing it means either a
    new event entered compiled_rules.json without LATE_GAME picking it
    up, or the per-event reach rule itself doesn't accept the late-game
    loadout — both are real bugs."""
    ast = rules["Burenia: gravitysuit"]
    pred = compile_to_lambda(ast, player=1)
    assert pred(State(LATE_GAME))


def test_artaria_charge_beam_requires_some_traversal(rules):
    """Charge Beam in Artaria's Charge Beam Room can be reached with
    Morph + Missile from the canonical route. An empty inventory must
    not satisfy the rule, and removing all of (Morph, Slide, Flash
    Shift, Space Jump) blocks every disjunct (each path requires at
    least one of those traversal items). Charge Beam itself is
    excluded to defeat the harmless self-loop disjunct."""
    ast = rules["Artaria: ChargeBeam"]
    pred = compile_to_lambda(ast, player=1)
    assert not pred(State({})), "ChargeBeam must not be trivially reachable"
    no_traversal = {k: v for k, v in LATE_GAME.items()
                    if k not in ("Charge Beam", "Morph Ball", "Slide",
                                 "Flash Shift", "Space Jump")}
    assert not pred(State(no_traversal)), \
        "ChargeBeam should require at least one traversal item"


def test_total_compiled_rule_count_is_149(rules):
    """137 actor pickups + 12 boss/EMMI/cutscene/corex pickups = 149. The
    forward resolver gates the bosses too (via pickup_index), so they now
    carry rules."""
    assert len(rules) == 149
