"""Hand-verified Elun rule assertions (Milestone 1 acceptance).

Each assertion is sourced from Randovania's human-readable
``Elun.txt`` (`.dread-cache/randovania-logic/Elun.txt`) plus the
in-game layout described on the Metroid Dread wiki
(https://metroid.wiki.gg/wiki/Quiet_Robe). These are the rules a
returning Dread player would expect — if the compiler's output starts
disagreeing with one of these, regression-bisect before assuming the
wiki is wrong.

The general shape of an assertion:
    * Pick a known item-loadout the player would (or wouldn't) have
    * Assert the predicate matches what vanilla Dread / the wiki says
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.Rules import compile_to_lambda  # noqa: E402


# ---- fixtures ----

@pytest.fixture(scope="module")
def rules():
    raw = json.loads((ROOT / "data" / "compiled_rules.json").read_text())
    return raw["rules"]


class State:
    """Minimal stand-in for AP CollectionState. Items map to counts."""

    def __init__(self, items: dict[str, int]):
        self.items = items

    def has(self, name: str, _player: int, count: int = 1) -> bool:
        return self.items.get(name, 0) >= count


def _all_event_items() -> dict[str, int]:
    raw = json.loads((ROOT / "data" / "compiled_rules.json").read_text())
    return {f"Event: {e['name']}": 1 for e in raw.get("events", [])}


VANILLA_LATE_GAME = {
    # A player who has cleared most of Dread by the standard route.
    # Used to confirm "fully-equipped player CAN reach everything."
    # Post-M2 also includes every event item, since events are real
    # AP items consulted via state.has("Event: <name>").
    "Morph Ball": 1, "Bomb": 1, "Cross Bomb": 1, "Power Bomb": 1,
    "Charge Beam": 1, "Wide Beam": 1, "Plasma Beam": 1, "Wave Beam": 1,
    "Grapple Beam": 1, "Diffusion Beam": 1, "Ice Missile": 1, "Storm Missile": 1,
    "Missile Tank": 5, "Power Bomb Tank": 2, "Energy Tank": 5,
    "Varia Suit": 1, "Gravity Suit": 1, "Phantom Cloak": 1,
    "Flash Shift": 1, "Pulse Radar": 1, "Speed Booster": 1,
    "Spider Magnet": 1, "Spin Boost": 1, "Space Jump": 1, "Screw Attack": 1,
    "Slide": 1, "Missile+ Tank": 1,
    **_all_event_items(),
}


# ---- Acceptance: each Elun pickup has a non-trivial rule ----

ELUN_LOCATIONS = (
    "Elun: energytank_000",
    "Elun: plasmabeam_000",
    "Elun: powerbombtank_000",
    "Elun: missiletank_002",
    "Elun: missiletank_000",
)


def test_all_elun_pickups_have_rules(rules):
    """All 5 Elun pickups must appear in compiled_rules.json. If one
    drops out, the compiler's actor-name → AP-location lookup broke."""
    for name in ELUN_LOCATIONS:
        assert name in rules, f"missing rule for {name}"


def test_no_elun_pickup_is_trivially_reachable(rules):
    """Every Elun pickup requires at least Morph Ball (entry routes
    go through morph-tunnels). A `trivial` rule means the compiler
    failed to find any constraint — almost certainly a bug."""
    for name in ELUN_LOCATIONS:
        ast = rules[name]
        assert ast.get("type") != "trivial", \
            f"{name} compiled to trivial — compiler likely missed constraints"


def test_no_elun_pickup_is_impossible(rules):
    """A `impossible` rule means the BFS found no path. Every vanilla
    Dread pickup IS reachable; if one shows impossible, the graph or
    the trick set are misconfigured."""
    for name in ELUN_LOCATIONS:
        ast = rules[name]
        assert ast.get("type") != "impossible", \
            f"{name} compiled to impossible — fixed-point/DFS missed a path"


def test_late_game_loadout_reaches_every_elun_pickup(rules):
    """A player with the full v0.1 progression item set should be able
    to reach every Elun pickup."""
    state = State(VANILLA_LATE_GAME)
    for name in ELUN_LOCATIONS:
        pred = compile_to_lambda(rules[name], player=1)
        assert pred(state), f"late-game loadout cannot reach {name}"


# ---- Hand-verified per-pickup assertions ----

def test_energy_tank_requires_plasma_beam(rules):
    """Energy Tank (114) sits in Ammo Recharge Station, which is gated
    by the Plasma Beam Door from Purple Drapes. Without Plasma Beam
    the pickup is unreachable. Source: Elun.txt §Ammo Recharge Station
    "Plasma Beam Door to Purple Drapes/Door to Ammo Recharge Station".
    """
    pred = compile_to_lambda(rules["Elun: energytank_000"], player=1)
    fully_equipped_except_plasma = {
        k: v for k, v in VANILLA_LATE_GAME.items() if k != "Plasma Beam"
    }
    assert not pred(State(fully_equipped_except_plasma)), \
        "Energy Tank should be unreachable without Plasma Beam"


def test_energy_tank_requires_morph_ball(rules):
    """Even with Plasma Beam, the pickup-adjacent connection requires
    'Lay Any Bomb' (template) which needs Morph Ball. Source:
    Elun.txt §Ammo Recharge Station ›Door to Chozo Soldier Arena›
    ›Pickup (Energy Tank): Lay Any Bomb."""
    pred = compile_to_lambda(rules["Elun: energytank_000"], player=1)
    fully_equipped_except_morph = {
        k: v for k, v in VANILLA_LATE_GAME.items() if k != "Morph Ball"
    }
    assert not pred(State(fully_equipped_except_morph)), \
        "Energy Tank should be unreachable without Morph Ball"


def test_plasma_beam_pickup_requires_morph_ball(rules):
    """Plasma Beam pickup needs Morph Ball regardless of which door
    you enter through. The cheap path uses the Lower Missile Door from
    Ammo Recharge Station; the navigation through Purple Drapes to
    reach AR Station's morph-launcher-dock requires morph tunnels.
    Source: Elun.txt §Purple Drapes ›Tunnel to Ammo Recharge Station
    (Morph Ball Launcher), §Plasma Beam Room ›Door from Ammo Recharge
    Station (Missile Door)."""
    pred = compile_to_lambda(rules["Elun: plasmabeam_000"], player=1)
    fully_equipped_except_morph = {
        k: v for k, v in VANILLA_LATE_GAME.items() if k != "Morph Ball"
    }
    assert not pred(State(fully_equipped_except_morph)), \
        "Plasma Beam pickup should be unreachable without Morph Ball"


def test_plasma_beam_pickup_does_not_require_plasma_beam(rules):
    """The Lower (Missile) Door entrance into Plasma Beam Room lets you
    skip the Plasma Beam Door entirely. A player with Morph + Bomb +
    Missile Tank + (Slide or Plasma) can grab Plasma Beam without
    already having Plasma Beam. Source: Elun.txt §Plasma Beam Room
    ›Door from Ammo Recharge Station (Missile Door from AR side).
    """
    pred = compile_to_lambda(rules["Elun: plasmabeam_000"], player=1)
    loadout_no_plasma = {
        "Morph Ball": 1, "Bomb": 1, "Missile Tank": 1, "Slide": 1,
    }
    assert pred(State(loadout_no_plasma)), \
        ("Plasma Beam pickup must be reachable without already having "
         "Plasma Beam — the Lower Missile Door makes it accessible.")


def test_power_bomb_tank_requires_morph_ball(rules):
    """Power Bomb Tank sits in Vertical Bomb Maze, deep in a
    morph-tunnel-only sub-region. No way in without Morph. Source:
    Elun.txt §Vertical Bomb Maze ›Pickup (Power Bomb Tank): Morph Ball
    (from Grapple Block Alcove)."""
    pred = compile_to_lambda(rules["Elun: powerbombtank_000"], player=1)
    fully_equipped_except_morph = {
        k: v for k, v in VANILLA_LATE_GAME.items() if k != "Morph Ball"
    }
    assert not pred(State(fully_equipped_except_morph)), \
        "Power Bomb Tank should be unreachable without Morph Ball"


def test_horizontal_bomb_maze_missile_requires_power_bomb(rules):
    """Missile Tank in Horizontal Bomb Maze (item_missiletank_000) is
    reached via Vertical Bomb Maze through Power-Bomb-gated tunnels.
    Source: Elun.txt §Vertical Bomb Maze ›Tunnel to Horizontal Bomb
    Maze (Upper): 'Power Bombs ≥ 2 and Lay Power Bomb'."""
    pred = compile_to_lambda(rules["Elun: missiletank_000"], player=1)
    no_pb = {k: v for k, v in VANILLA_LATE_GAME.items() if k != "Power Bomb"}
    assert not pred(State(no_pb)), \
        "Horizontal Bomb Maze missile should require Power Bomb"
