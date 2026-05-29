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
    Header,
    IMPOSSIBLE,
    RDV_ITEM_TO_AP,
    TRIVIAL,
    absorb_or,
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


def test_translate_resource_trick_level_1_is_trivial():
    hdr = _empty_header()
    req = {"type": "resource", "data": {
        "type": "tricks", "name": "IBJ", "amount": 1, "negate": False}}
    assert translate_requirement(req, hdr) == TRIVIAL


def test_translate_resource_trick_level_2_is_impossible():
    hdr = _empty_header()
    req = {"type": "resource", "data": {
        "type": "tricks", "name": "IBJ", "amount": 2, "negate": False}}
    assert translate_requirement(req, hdr) == IMPOSSIBLE


def test_translate_resource_event():
    hdr = _empty_header()
    req = {"type": "resource", "data": {
        "type": "events", "name": "Cool Event", "amount": 1, "negate": False}}
    assert translate_requirement(req, hdr) == {"type": "event", "name": "Cool Event"}


def test_translate_resource_negate_is_impossible():
    """v0.1 doesn't model 'must NOT have X'."""
    hdr = _empty_header()
    req = {"type": "resource", "data": {
        "type": "items", "name": "Morph", "amount": 1, "negate": True}}
    assert translate_requirement(req, hdr) == IMPOSSIBLE


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
    # Trick at level 1 is trivial, so the OR collapses to trivial,
    # which is then absorbed by the AND.
    assert out == {"type": "item", "name": "Morph Ball", "amount": 1}


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
