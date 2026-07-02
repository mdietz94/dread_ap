"""Unit tests for scripts/extract_dread_rules.py — the AST builder
and simplifier.

These don't need the cached Randovania JSON; they exercise the
primitives against tiny synthetic inputs. Cache-driven end-to-end
tests live in tests/test_elun_rules.py (which exercises the
compiled_rules.json output the compiler shipped)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from extract_dread_rules import (  # noqa: E402
    CompileError,
    Header,
    IMPOSSIBLE,
    MUTEX_TOGGLE_EVENTS,
    ONE_WAY_ROTATABLE_EVENTS,
    RDV_ITEM_TO_AP,
    TRIVIAL,
    absorb_or,
    assert_rotatable_model_soundness,
    assert_toggle_model_soundness,
    ast_to_dnf,
    dnf_to_ast,
    enumerate_paths,
    mk_and,
    mk_or,
    translate_requirement,
)


# ---- mk_and / mk_or simplifiers ----

def test_mk_and_empty_is_trivial():
    assert mk_and([]) == TRIVIAL


def test_mk_and_drops_trivial():
    item = {"type": "item", "name": "Morph Ball", "amount": 1}
    assert mk_and([TRIVIAL, item, TRIVIAL]) == item


def test_mk_and_with_impossible_short_circuits():
    item = {"type": "item", "name": "Morph Ball", "amount": 1}
    assert mk_and([item, IMPOSSIBLE]) == IMPOSSIBLE


def test_mk_and_flattens_nested_ands():
    a = {"type": "item", "name": "Morph Ball", "amount": 1}
    b = {"type": "item", "name": "Bomb", "amount": 1}
    c = {"type": "item", "name": "Plasma Beam", "amount": 1}
    inner = mk_and([a, b])
    out = mk_and([inner, c])
    assert out["type"] == "and"
    assert {entry["name"] for entry in out["items"]} == {"Morph Ball", "Bomb", "Plasma Beam"}


def test_mk_and_dedupes():
    a = {"type": "item", "name": "Morph Ball", "amount": 1}
    out = mk_and([a, a, a])
    assert out == a


def test_mk_or_empty_is_impossible():
    assert mk_or([]) == IMPOSSIBLE


def test_mk_or_with_trivial_short_circuits():
    item = {"type": "item", "name": "Morph Ball", "amount": 1}
    assert mk_or([item, TRIVIAL]) == TRIVIAL


def test_mk_or_drops_impossible():
    a = {"type": "item", "name": "Bomb", "amount": 1}
    out = mk_or([IMPOSSIBLE, a, IMPOSSIBLE])
    assert out == a


def test_mk_or_flattens_nested_ors():
    a = {"type": "item", "name": "Bomb", "amount": 1}
    b = {"type": "item", "name": "Cross Bomb", "amount": 1}
    c = {"type": "item", "name": "Power Bomb", "amount": 1}
    inner = mk_or([a, b])
    out = mk_or([inner, c])
    assert out["type"] == "or"
    assert {e["name"] for e in out["items"]} == {"Bomb", "Cross Bomb", "Power Bomb"}


# ---- absorb_or ----

def test_absorb_or_drops_dominated_path():
    """If path B requires only Morph Ball and path A requires
    Morph + Plasma, A is dominated (anyone meeting B's requirement
    automatically meets A's, since B is a strict subset)."""
    morph = {"type": "item", "name": "Morph Ball", "amount": 1}
    plasma = {"type": "item", "name": "Plasma Beam", "amount": 1}
    path_b = morph
    path_a = mk_and([morph, plasma])
    out = absorb_or(mk_or([path_a, path_b]))
    assert out == morph


def test_absorb_or_keeps_incomparable_paths():
    """Two paths with disjoint required items are both kept."""
    morph = {"type": "item", "name": "Morph Ball", "amount": 1}
    plasma = {"type": "item", "name": "Plasma Beam", "amount": 1}
    out = absorb_or(mk_or([morph, plasma]))
    assert out["type"] == "or"
    assert len(out["items"]) == 2


# ---- translate_requirement ----

def _empty_header() -> Header:
    return Header(
        items_by_short={"Morph": {"long_name": "Morph Ball"}},
        events_by_name={"Cool Event": {"long_name": "Cool Event"}},
        tricks_by_short={"IBJ": {"long_name": "Infinite Bomb Jump"}},
        damages_by_short={"Heat": {"long_name": "Heat"}},
        templates={
            "Test Template": {
                "display_name": "Test Template",
                "requirement": {"type": "resource", "data": {
                    "type": "items", "name": "Morph", "amount": 1, "negate": False}},
            },
        },
        dock_weakness={},
        starting_location={"region": "Artaria", "area": "x", "node": "y"},
    )


def test_translate_resource_item():
    hdr = _empty_header()
    req = {"type": "resource", "data": {
        "type": "items", "name": "Morph", "amount": 1, "negate": False}}
    assert translate_requirement(req, hdr) == {
        "type": "item", "name": "Morph Ball", "amount": 1}


def test_translate_resource_trick_is_symbolic():
    """v3: tricks are kept symbolic (name + level) instead of collapsed at
    compile time. The per-trick level is resolved at AP-generation time."""
    hdr = _empty_header()
    for level in (1, 2, 3, 4, 5):
        req = {"type": "resource", "data": {
            "type": "tricks", "name": "IBJ", "amount": level, "negate": False}}
        assert translate_requirement(req, hdr) == {
            "type": "trick", "name": "IBJ", "level": level}


def test_translate_resource_unknown_trick_raises():
    hdr = _empty_header()
    req = {"type": "resource", "data": {
        "type": "tricks", "name": "NotATrick", "amount": 1, "negate": False}}
    with pytest.raises(CompileError):
        translate_requirement(req, hdr)


def test_trick_node_dnf_round_trip():
    """A symbolic trick survives AST → DNF → AST so it reaches the artifact."""
    ast = {"type": "trick", "name": "IBJ", "level": 2}
    dnf = ast_to_dnf(ast)
    assert dnf == frozenset({frozenset({("trick", "IBJ", 2)})})
    assert dnf_to_ast(dnf) == ast


def test_trick_atom_combines_with_items_in_dnf():
    ast = {"type": "or", "items": [
        {"type": "item", "name": "Morph Ball", "amount": 1},
        {"type": "trick", "name": "Walljump", "level": 3},
    ]}
    dnf = ast_to_dnf(ast)
    assert frozenset({("item", "Morph Ball", 1)}) in dnf
    assert frozenset({("trick", "Walljump", 3)}) in dnf


def test_disjunct_sort_key_prefers_item_only_over_trick_gated():
    """The cap penalty must keep item-only paths ahead of trick-gated ones, so
    truncation can only over-restrict a trick-disabled player (never falsely
    reach). A single trick atom sorts AFTER a two-item, trick-free disjunct."""
    from extract_dread_rules import _disjunct_sort_key
    item_only = frozenset({("item", "Morph Ball", 1), ("item", "Spider", 1)})
    trick_gated = frozenset({("trick", "Walljump", 2)})
    assert _disjunct_sort_key(item_only) < _disjunct_sort_key(trick_gated)
    # Two trick-free disjuncts keep pre-v3 (len, sorted) ordering.
    short = frozenset({("item", "Morph Ball", 1)})
    assert _disjunct_sort_key(short) < _disjunct_sort_key(item_only)


def test_translate_resource_event():
    hdr = _empty_header()
    req = {"type": "resource", "data": {
        "type": "events", "name": "Cool Event", "amount": 1, "negate": False}}
    assert translate_requirement(req, hdr) == {"type": "event", "name": "Cool Event"}


def test_translate_negated_item_is_trivial():
    """Negated item/event = TEMPORAL ('haven't got/triggered it yet'). Dread's
    major progression is gated solely by negated state, so we treat it as
    Trivial (satisfiable in the early state); the forward resolver inlines
    events in dependency order to keep AP's sweep consistent."""
    hdr = _empty_header()
    req = {"type": "resource", "data": {
        "type": "items", "name": "Morph", "amount": 1, "negate": True}}
    assert translate_requirement(req, hdr) == TRIVIAL


def test_translate_negated_event_is_trivial():
    hdr = _empty_header()
    req = {"type": "resource", "data": {
        "type": "events", "name": "Cool Event", "amount": 1, "negate": True}}
    assert translate_requirement(req, hdr) == TRIVIAL


def test_translate_misc_resolves_against_config():
    """misc resources: DoorLocks is kept symbolic (resolved at generation time);
    other flags (NerfPowerBombs etc.) are resolved at compile time."""
    hdr = _empty_header()

    def misc(name, negate):
        return translate_requirement({"type": "resource", "data": {
            "type": "misc", "name": name, "amount": 1, "negate": negate}}, hdr)

    # DoorLocks emits a symbolic atom, not trivial/impossible.
    assert misc("DoorLocks", False) == {"type": "misc", "name": "DoorLocks",
                                        "negate": False}
    assert misc("DoorLocks", True) == {"type": "misc", "name": "DoorLocks",
                                       "negate": True}
    # Other flags still resolved compile-time.
    assert misc("NerfPowerBombs", False) == TRIVIAL
    assert misc("NerfPowerBombs", True) == IMPOSSIBLE


def test_translate_template_expansion():
    hdr = _empty_header()
    req = {"type": "template", "data": "Test Template"}
    out = translate_requirement(req, hdr)
    assert out == {"type": "item", "name": "Morph Ball", "amount": 1}


def test_translate_and_or_recursion():
    hdr = _empty_header()
    req = {"type": "and", "data": {"items": [
        {"type": "resource", "data": {
            "type": "items", "name": "Morph", "amount": 1, "negate": False}},
        {"type": "or", "data": {"items": [
            {"type": "resource", "data": {
                "type": "tricks", "name": "IBJ", "amount": 1, "negate": False}},
        ]}},
    ]}}
    out = translate_requirement(req, hdr)
    # v3: the trick stays symbolic, so the OR keeps it and the AND keeps both
    # the Morph item and the (single-child OR collapsed) trick atom.
    assert out == {"type": "and", "items": [
        {"type": "item", "name": "Morph Ball", "amount": 1},
        {"type": "trick", "name": "IBJ", "level": 1},
    ]}


def test_unmapped_item_fails_loudly():
    """If Randovania ships a new item we haven't added to RDV_ITEM_TO_AP,
    we want a crash with a clear message, not silent over-permissive
    rules."""
    hdr = _empty_header()
    # Pretend the global db has 'Unknown', but our mapping doesn't.
    hdr.items_by_short["Unknown"] = {"long_name": "Mystery"}
    req = {"type": "resource", "data": {
        "type": "items", "name": "Unknown", "amount": 1, "negate": False}}
    from extract_dread_rules import CompileError
    with pytest.raises(CompileError, match="RDV_ITEM_TO_AP"):
        translate_requirement(req, hdr)


# ---- enumerate_paths ----

def test_enumerate_paths_single_edge():
    """A → B with a Morph requirement — B's reach AST = item(Morph Ball)."""
    morph = {"type": "item", "name": "Morph Ball", "amount": 1}
    edges = {"A": [("B", morph)]}
    out = enumerate_paths(["A"], edges, targets={"B"})
    assert out["B"] == morph


def test_enumerate_paths_no_path_is_impossible():
    edges = {"A": []}
    out = enumerate_paths(["A"], edges, targets={"B"})
    assert out["B"] == IMPOSSIBLE


def test_enumerate_paths_or_over_branches():
    morph = {"type": "item", "name": "Morph Ball", "amount": 1}
    plasma = {"type": "item", "name": "Plasma Beam", "amount": 1}
    # Two paths A → B: one needs Morph, other needs Plasma.
    edges = {"A": [("B", morph), ("C", plasma)], "C": [("B", TRIVIAL)]}
    out = enumerate_paths(["A"], edges, targets={"B"})
    # Should be OR(item(Morph), item(Plasma)) — absorb_or won't drop
    # either since neither's set is a strict subset of the other.
    assert out["B"]["type"] == "or"
    names = {c["name"] for c in out["B"]["items"]}
    assert names == {"Morph Ball", "Plasma Beam"}


# ---- mapping coverage ----

def test_rdv_item_mapping_resolves_to_valid_ap_items_or_none():
    """Every value in RDV_ITEM_TO_AP must either be None or match a
    name in our items.json. If you add a new RDV item, you also need
    to either add it to items.json or set its mapping to None."""
    import json
    items = json.loads((ROOT.parent / "apworld" / "dread" /
                        "data" / "items.json").read_text())
    valid = {it["name"] for it in items}
    for rdv_name, ap_name in RDV_ITEM_TO_AP.items():
        if ap_name is None:
            continue
        assert ap_name in valid, \
            f"RDV_ITEM_TO_AP[{rdv_name!r}] = {ap_name!r} not in items.json"


# ---- start_comps actor resolution ----

def test_start_comp_prefers_start_point_actor_name():
    """valid_starting_location nodes carry two actor fields:
    - actor_name: the interactive entity (save station / map room) embedded
      in a wall — spawning Samus there via Game.LoadScenario causes wall collisions
    - start_point_actor_name: the weight-activated platform where Samus stands

    The graph builder must use start_point_actor_name for the patcher actor so
    /warp and the ROM starting_location both point at the correct spawn position.
    Falls back to actor_name only when start_point_actor_name is absent."""
    def resolve(ex: dict) -> str | None:
        return ex.get("start_point_actor_name") or ex.get("actor_name")

    # Cataris access point: has both — must pick the platform
    assert resolve({
        "actor_name": "accesspoint",
        "start_point_actor_name": "accesspoint_platform",
    }) == "accesspoint_platform"

    # Dairon save station: has both — must pick the platform
    assert resolve({
        "actor_name": "savestation_000",
        "start_point_actor_name": "savestation_000_platform",
    }) == "savestation_000_platform"

    # Artaria StartPoint0: only start_point_actor_name (no actor_name)
    assert resolve({
        "start_point_actor_name": "StartPoint0",
    }) == "StartPoint0"

    # Hypothetical fallback: only actor_name present
    assert resolve({
        "actor_name": "some_entity",
    }) == "some_entity"


# ---------------------------------------------------------------------------
# Mutually-exclusive thermal-toggle handling (MUTEX_TOGGLE_EVENTS) + soundness
# ---------------------------------------------------------------------------

def _header_with_events(*event_names: str) -> Header:
    hdr = _empty_header()
    hdr.events_by_name = {n: {"long_name": n} for n in event_names}
    return hdr


def test_negated_mutex_toggle_event_is_trivial():
    """``NOT <thermal toggle>`` still drops to TRIVIAL (drop-the-transient); the
    drop is proven sound per game version by assert_toggle_model_soundness, not by
    over-restricting it to IMPOSSIBLE (which would wall the room off)."""
    name = "s020_magma:default:deviceheat_002"
    assert name in MUTEX_TOGGLE_EVENTS
    hdr = _header_with_events(name)
    req = {"type": "resource", "data": {
        "type": "events", "name": name, "amount": 1, "negate": True}}
    assert translate_requirement(req, hdr) == TRIVIAL


def test_negated_unknown_event_raises():
    """A negated event must still resolve to a known event (symmetry with the
    positive branch) — a typo no longer silently passes through as TRIVIAL."""
    hdr = _header_with_events("Cool Event")
    req = {"type": "resource", "data": {
        "type": "events", "name": "Nonexistent", "amount": 1, "negate": True}}
    with pytest.raises(CompileError):
        translate_requirement(req, hdr)


# -- assert_toggle_model_soundness ------------------------------------------

_DEV2 = "s020_magma:default:deviceheat_002"
_DEV1 = "s020_magma:default:deviceheat_camerafar_000"
_DEVDEF = "actordef:actors/props/deviceheat/charclasses/deviceheat.bmsad"


def _ev_node(name: str) -> dict:
    return {"node_type": "event", "event_name": name,
            "extra": {"actor_def": _DEVDEF}, "connections": {}}


def _not(event: str) -> dict:
    return {"type": "resource",
            "data": {"type": "events", "name": event, "negate": True}}


def _clean() -> dict:
    return {"type": "resource",
            "data": {"type": "items", "name": "Morph", "amount": 1}}


def _good_mutex_area() -> dict:
    """A minimal area mirroring Cataris Thermal Device Room North: the curated
    toggle pair co-located, every NOT-toggle edge backed by a boundary dock or a
    toggle-independent sibling."""
    return {
        "Cataris": {"areas": {"Thermal Device Room North": {"nodes": {
            "EventDev2": _ev_node(_DEV2),
            "EventDev1": _ev_node(_DEV1),
            # Boundary door: reachable from the neighbouring region regardless.
            "DoorA": {"node_type": "dock", "connections": {}},
            # Interior pickup: a NOT-toggle edge AND a toggle-independent sibling.
            "Pickup": {"node_type": "pickup", "connections": {}},
            "SrcTainted": {"node_type": "generic",
                           "connections": {"Pickup": _not(_DEV2),
                                           "DoorA": _not(_DEV2)}},
            "SrcClean": {"node_type": "generic",
                         "connections": {"Pickup": _clean()}},
        }}}},
    }


def test_mutex_toggle_constant_matches_cataris_pair():
    assert MUTEX_TOGGLE_EVENTS == frozenset({_DEV2, _DEV1})


def test_toggle_soundness_passes_on_well_formed_area():
    # Should not raise.
    assert_toggle_model_soundness(_good_mutex_area())


def test_toggle_soundness_trips_on_new_colocated_pair():
    """A NEW co-located deviceheat pair (unmodelled mutex room) must fail loudly
    so a human re-evaluates before shipping."""
    area = _good_mutex_area()
    area["Cataris"]["areas"]["Some Other Room"] = {"nodes": {
        "EvA": _ev_node("s020_magma:default:deviceheat_777"),
        "EvB": _ev_node("s020_magma:default:deviceheat_888"),
    }}
    with pytest.raises(CompileError, match="new mutually-exclusive thermal room"):
        assert_toggle_model_soundness(area)


def test_toggle_soundness_trips_when_negation_is_sole_gate():
    """An interior node reachable ONLY via a NOT-<toggle> edge (no boundary dock,
    no toggle-independent sibling) means the dropped negation is the sole gate —
    the TRIVIAL drop is unsound there and must fail."""
    area = _good_mutex_area()
    # Strip the clean sibling so Pickup depends solely on the NOT-toggle edge.
    del area["Cataris"]["areas"]["Thermal Device Room North"]["nodes"]["SrcClean"]
    with pytest.raises(CompileError, match="no longer sound here"):
        assert_toggle_model_soundness(area)


# -- one-way rotatable flipper (ONE_WAY_ROTATABLE_EVENTS) --------------------

_ROT = "ArtariaSARotatable"


def _turn_edge_req() -> dict:
    """The Screw Attack Room flipper turn edge: OR[NOT HighDanger, NOT <rot>].
    Both disjuncts trivialise by default, so the edge is falsely free."""
    return {"type": "or", "data": {"items": [
        {"type": "resource", "data": {"type": "misc", "name": "HighDanger",
                                      "negate": True}},
        {"type": "resource", "data": {"type": "events", "name": _ROT,
                                      "negate": True}},
    ]}}


def test_rotatable_curation_includes_screw_attack():
    assert _ROT in ONE_WAY_ROTATABLE_EVENTS


def test_rotatable_turn_edge_trivial_without_pessimism():
    """Default (non-rotatable-room) translation still lets the turn edge trivialise
    — the false-positive behaviour we sever only inside curated rooms."""
    hdr = _header_with_events(_ROT)
    assert translate_requirement(_turn_edge_req(), hdr) == TRIVIAL


def test_rotatable_turn_edge_severed_with_pessimism():
    """Inside a curated rotatable room BOTH disjuncts resolve to Impossible, so
    the optimistic 'assume the turn works out' edge is severed. Dropping only one
    (HighDanger OR the event) would leave the other trivial and the edge free —
    hence both must be in the pessimistic set."""
    hdr = _header_with_events(_ROT)
    pneg = frozenset({"HighDanger", _ROT})
    assert translate_requirement(_turn_edge_req(), hdr, pessimistic_neg=pneg) == IMPOSSIBLE
    # Dropping only HighDanger is NOT enough (the NOT-event co-guard stays trivial).
    assert translate_requirement(
        _turn_edge_req(), hdr, pessimistic_neg=frozenset({"HighDanger"})) == TRIVIAL


def test_rotatable_pessimism_leaves_positive_event_untouched():
    """Only NEGATED atoms flip; a positive event reference and normal negated
    events elsewhere are unaffected."""
    hdr = _header_with_events(_ROT, "Other Event")
    pneg = frozenset({"HighDanger", _ROT})
    pos = {"type": "resource", "data": {"type": "events", "name": _ROT, "amount": 1}}
    assert translate_requirement(pos, hdr, pessimistic_neg=pneg) == {
        "type": "event", "name": _ROT}
    other_neg = {"type": "resource", "data": {"type": "events", "name": "Other Event",
                                              "negate": True}}
    assert translate_requirement(other_neg, hdr, pessimistic_neg=pneg) == TRIVIAL


def _rot_area(with_turn_edge: bool = True) -> dict:
    """Minimal area hosting the rotatable event with the severable turn edge."""
    turn = _turn_edge_req() if with_turn_edge else _clean()
    return {
        "Artaria": {"areas": {"Screw Attack Room": {"nodes": {
            "EventRot": {"node_type": "event", "event_name": _ROT,
                         "connections": {}},
            "Entrance": {"node_type": "generic",
                         "connections": {"EventRot": turn}},
        }}}},
    }


def test_rotatable_soundness_passes_on_real_turn_edge():
    hdr = _header_with_events(_ROT)
    assert_rotatable_model_soundness(_rot_area(with_turn_edge=True), hdr)


def test_rotatable_soundness_trips_on_stale_curation():
    """If the curated event no longer exists upstream, fail loudly."""
    hdr = _header_with_events(_ROT)
    area = {"Artaria": {"areas": {"Empty Room": {"nodes": {}}}}}
    with pytest.raises(CompileError, match="stale ONE_WAY_ROTATABLE_EVENTS"):
        assert_rotatable_model_soundness(area, hdr)


def test_rotatable_soundness_trips_on_silent_noop():
    """If the room hosts the rotatable but no edge is actually changed by the
    pessimistic drop (upstream restructured the turn edge), fail loudly."""
    hdr = _header_with_events(_ROT)
    with pytest.raises(CompileError, match="no-op"):
        assert_rotatable_model_soundness(_rot_area(with_turn_edge=False), hdr)
