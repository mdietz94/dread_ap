"""Universal Tracker /explain rendering.

The renderer (ut_explain.render_ast) is AP-free — it evaluates atoms via
Rules.compile_to_lambda against a tiny fake state — so most coverage needs no
Archipelago install. One gated test drives the real DreadWorld hooks
(explain_path / explain_rule) end to end.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

GRAPH_PATH = ROOT / "data" / "logic_graph.json"

from dread.ut_explain import (  # noqa: E402
    ExplainCtx, build_comp_labels, render_ast, humanize_event,
    _GREEN, _SALMON, _UNKNOWN,
)


class FakeState:
    """Duck-typed CollectionState for compile_to_lambda."""
    def __init__(self, items=None, regions=None):
        self.items = dict(items or {})
        self.regions = set(regions or [])

    def has(self, name, player, count=1):
        return self.items.get(name, 0) >= count

    def count(self, name, player):
        return self.items.get(name, 0)

    def can_reach_region(self, region, player):
        return region in self.regions


def ctx(**kw):
    base = dict(player=1, trick_levels={}, energy_per_tank=100,
                ammo_amounts={}, door_lock_rando=False)
    base.update(kw)
    return ExplainCtx(**base)


def _text(parts):
    return "".join(p["text"] for p in parts)


def _color_of(parts, needle):
    for p in parts:
        if p.get("type") == "color" and needle in p["text"]:
            return p["color"]
    return None


# ---- leaf atoms -----------------------------------------------------------

def test_item_satisfied_is_green():
    ast = {"type": "item", "name": "Grapple Beam"}
    parts = render_ast(ast, ctx(), FakeState({"Grapple Beam": 1}))
    assert _text(parts) == "Grapple Beam"
    assert _color_of(parts, "Grapple Beam") == _GREEN


def test_item_missing_is_salmon():
    ast = {"type": "item", "name": "Grapple Beam"}
    parts = render_ast(ast, ctx(), FakeState({}))
    assert _color_of(parts, "Grapple Beam") == _SALMON


def test_item_amount_shown():
    ast = {"type": "item", "name": "Missile Tank", "amount": 3}
    parts = render_ast(ast, ctx(), FakeState({"Missile Tank": 2}))
    assert _text(parts) == "Missile Tank x3"
    assert _color_of(parts, "Missile Tank") == _SALMON  # has 2, needs 3


def test_state_none_is_unknown_color():
    ast = {"type": "item", "name": "Grapple Beam"}
    parts = render_ast(ast, ctx(), None)
    assert _color_of(parts, "Grapple Beam") == _UNKNOWN


def test_event_renders_reach_and_uses_region_reach():
    ast = {"type": "event", "name": "ArtariaCU"}
    # region key is the RAW name; the label shown is humanized.
    reached = render_ast(ast, ctx(), FakeState(regions={"Event:ArtariaCU"}))
    assert _text(reached) == "reach Artaria Central Unit"
    assert _color_of(reached, "Artaria Central Unit") == _GREEN
    missing = render_ast(ast, ctx(), FakeState(regions=set()))
    assert _color_of(missing, "Artaria Central Unit") == _SALMON


def test_humanize_event_camelcase():
    assert humanize_event("ArtariaEmmiMagnetPlatformLowered") == \
        "Artaria EMMI Magnet Platform Lowered"
    assert humanize_event("ArtariaCU") == "Artaria Central Unit"
    assert humanize_event("CatarisThermalLowerLava") == "Cataris Thermal Lower Lava"


def test_humanize_event_colon_id_maps_region_and_cleans_actor():
    # scenario code -> region; colon id -> region-prefixed cleaned actor.
    assert humanize_event("s010_cave:default:PRP_CV_PlatformCaveWater_B") == \
        "Artaria: PRP CV Platform Cave Water B"
    assert humanize_event("s020_magma:default:grapplepulloff1x2_001") == \
        "Cataris: grapple pull off"
    # trailing numeric suffix stripped; opaque code survives region-grouped.
    assert humanize_event("s010_cave:default:PRP_DB_CV_008") == "Artaria: PRP DB CV"


def test_humanize_event_unknown_scenario_falls_back_to_code():
    assert humanize_event("s999_x:default:foo_001").startswith("s999_x: ")


def test_trick_enabled_by_effective_level():
    ast = {"type": "trick", "name": "Speedbooster", "level": 2}
    on = render_ast(ast, ctx(trick_levels={"Speedbooster": 3}), FakeState())
    assert "Speed Booster Conservation" in _text(on)
    assert "[intermediate]" in _text(on)
    assert _color_of(on, "Speed Booster") == _GREEN
    off = render_ast(ast, ctx(trick_levels={"Speedbooster": 1}), FakeState())
    assert _color_of(off, "Speed Booster") == _SALMON


def test_sum_missiles_label_and_truth():
    ast = {"type": "sum", "base": 15, "threshold": 40,
           "terms": [{"name": "Missile Tank", "per_unit": 2},
                     {"name": "Missile+ Tank", "per_unit": 10}]}
    label = "Missiles ≥ 40"
    enough = render_ast(ast, ctx(), FakeState({"Missile Tank": 13}))  # 15+26=41
    assert _text(enough) == label
    assert _color_of(enough, "Missiles") == _GREEN
    short = render_ast(ast, ctx(), FakeState({"Missile Tank": 1}))  # 17
    assert _color_of(short, "Missiles") == _SALMON


def test_sum_power_bomb_label():
    ast = {"type": "sum", "base": 0, "threshold": 2,
           "terms": [{"name": "Power Bomb", "per_unit": 2},
                     {"name": "Power Bomb Tank", "per_unit": 2}]}
    parts = render_ast(ast, ctx(), FakeState({"Power Bomb": 1}))
    assert _text(parts) == "Power Bombs ≥ 2"
    assert _color_of(parts, "Power Bombs") == _GREEN


def test_damage_threshold_suit_or_energy():
    ast = {"type": "damage_threshold", "hp_needed": 300,
           "suit_options": ["Gravity Suit"]}
    txt = _text(render_ast(ast, ctx(), FakeState({"Gravity Suit": 1})))
    assert txt == "survive 300 dmg (Gravity Suit or energy)"
    # suit present -> green
    assert _color_of(render_ast(ast, ctx(), FakeState({"Gravity Suit": 1})),
                     "survive") == _GREEN
    # no suit, enough tanks (99 + 100*2 = 299 < 300 -> salmon)
    assert _color_of(render_ast(ast, ctx(), FakeState({"Energy Tank": 2})),
                     "survive") == _SALMON
    # 3 tanks = 399 >= 300 -> green
    assert _color_of(render_ast(ast, ctx(), FakeState({"Energy Tank": 3})),
                     "survive") == _GREEN


# ---- composites -----------------------------------------------------------

def test_and_or_nesting_and_separators():
    ast = {"type": "and", "items": [
        {"type": "item", "name": "Morph Ball"},
        {"type": "or", "items": [
            {"type": "item", "name": "Bomb"},
            {"type": "item", "name": "Cross Bomb"}]}]}
    parts = render_ast(ast, ctx(), FakeState({"Morph Ball": 1, "Bomb": 1}))
    assert _text(parts) == "(Morph Ball & (Bomb | Cross Bomb))"


def test_trivial_and_impossible():
    assert _text(render_ast({"type": "trivial"}, ctx(), None)) == "always"
    assert _text(render_ast({"type": "impossible"}, ctx(), None)) == "never"


def test_empty_and_or():
    assert _text(render_ast({"type": "and", "items": []}, ctx(), None)) == "always"
    assert _text(render_ast({"type": "or", "items": []}, ctx(), None)) == "never"


def test_unknown_atom_does_not_crash():
    parts = render_ast({"type": "wat", "x": 1}, ctx(), FakeState())
    assert parts and "wat" in _text(parts)


# ---- component labels -----------------------------------------------------

def test_build_comp_labels_prefers_pickup_then_event_then_start_then_region():
    graph = {
        "pickups": [[5, "Artaria: Charge Beam Room"]],
        "events": [[7, "After Corpius"], [5, "Ignored (has pickup)"]],
        "start_comps": {"Dairon::area::node": {"comp": 9}},
        # comp_regions is indexed by comp id; the region fallback fills the rest.
        "comp_regions": ["Artaria", "Cataris", None, None, None,
                         "Artaria", "Burenia", "Cataris", "Ghavoran", "Dairon"],
    }
    labels = build_comp_labels(graph)
    assert labels[5] == "Artaria: Charge Beam Room"   # pickup wins over region
    assert labels[7] == "After Corpius (event)"       # event wins over region
    assert labels[9] == "Dairon"                       # start wins over region
    assert labels[1] == "Cataris"                      # region-only fallback
    assert labels[6] == "Burenia"
    assert 2 not in labels                             # comp_regions None -> unlabeled


# ---- gated: real world hooks ---------------------------------------------

def _ap() -> bool:
    try:
        import BaseClasses, Options  # noqa: F401
        from worlds.AutoWorld import World  # noqa: F401
    except ImportError:
        return False
    return True


runtime = pytest.mark.skipif(
    not (_ap() and GRAPH_PATH.exists()),
    reason="needs Archipelago runtime + logic_graph.json",
)


@runtime
def test_explain_hooks_on_real_world():
    from test.general import setup_multiworld, gen_steps
    from Fill import distribute_items_restrictive
    from dread.World import DreadWorld

    mw = setup_multiworld(DreadWorld, gen_steps, seed=2, options={})
    distribute_items_restrictive(mw)
    world = mw.worlds[1]
    state = mw.get_all_state(False)

    # explain_path over a real entrance that carries a rule renders a hop
    # ("<src> [reachable] -> <dst> : <requirement>") with no bare "e{i}" name.
    # The separator is ASCII (the tracker font can't render U+2192).
    assert world._explain_asts, "expected non-trivial entrances to be stashed"
    ent_name = next(iter(world._explain_asts))
    ent = mw.get_entrance(ent_name, 1)
    hop = world.explain_path(ent, state)
    hop_text = "".join(p["text"] for p in hop)
    assert "->" in hop_text and ":" in hop_text
    assert "→" not in hop_text               # no non-ASCII arrow
    assert ent_name not in hop_text          # machine name is not surfaced

    # explain_rule resolves a location and lists its gating entrances.
    loc = next(iter(mw.get_locations(1)))
    parts = world.explain_rule(loc.name, state)
    assert parts is not None
    text = "".join(p["text"] for p in parts)
    assert loc.name in text
    assert "reachable" in text

    # unresolved target -> None (UT falls back to its default error path).
    assert world.explain_rule("definitely not a real location", state) is None


@runtime
def test_explain_rule_recurses_into_blocked_sources():
    """A NOT-reachable target expands the blocked chain upstream: at least one
    unreachable location's explanation nests deeper than the single first-level
    indent, so a bare "Burenia [blocked]" source is drilled into, not dead-ended.
    A reachable target stays one level deep."""
    from BaseClasses import CollectionState
    from test.general import setup_multiworld, gen_steps
    from Fill import distribute_items_restrictive
    from dread.World import DreadWorld

    mw = setup_multiworld(DreadWorld, gen_steps, seed=2, options={})
    distribute_items_restrictive(mw)
    world = mw.worlds[1]
    empty = CollectionState(mw)  # nothing collected -> deep spots are blocked

    # Find a location that is unreachable from an empty state and whose chain
    # goes at least two regions deep (a blocked source with a blocked source).
    deep = None
    for loc in mw.get_locations(1):
        if loc.can_reach(empty):
            continue
        parts = world.explain_rule(loc.name, empty)
        text = "".join(p["text"] for p in parts)
        if "\n    " in text:   # a second-level (depth>=1) indent appeared
            deep = text
            break
    assert deep is not None, "expected at least one deeply-blocked location"
    assert "blocked" in deep
    assert "→" not in deep     # still ASCII throughout the tree

    # A reachable target does not fan out the blocked-chain recursion.
    full = mw.get_all_state(False)
    reachable = next(l for l in mw.get_locations(1) if l.can_reach(full))
    flat = "".join(p["text"] for p in world.explain_rule(reachable.name, full))
    assert "\n    " not in flat
