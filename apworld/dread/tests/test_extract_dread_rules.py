"""Unit tests for the AST helpers in scripts/extract_dread_rules.py.

Covers the pure helpers (translate_damage, _translate_ammo, DNF round-trip,
AMMO set membership) that require no Randovania logic cache.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import extract_dread_rules as edr  # noqa: E402


# ---- AMMO collapse set ----

def test_ammo_or_tank_items_excludes_counted_resources():
    """ETank / EFragment route through state.has(name, p, amount); MissileAmmo
    / PBAmmo route through _translate_ammo. The collapse set must contain
    only the unlock-flag items left — a regression that re-added a counted
    resource here would silently strip per-tank counting."""
    assert "MissileAmmo" not in edr._AMMO_OR_TANK_ITEMS
    assert "PBAmmo" not in edr._AMMO_OR_TANK_ITEMS
    assert "ETank" not in edr._AMMO_OR_TANK_ITEMS
    assert "EFragment" not in edr._AMMO_OR_TANK_ITEMS
    # Unlock flags stay in the set.
    assert "Supers" in edr._AMMO_OR_TANK_ITEMS
    assert "MissileLauncher" in edr._AMMO_OR_TANK_ITEMS
    assert "MainPB" in edr._AMMO_OR_TANK_ITEMS


# ---- translate_damage ----

def test_translate_damage_heat_emits_threshold():
    ast = edr.translate_damage("Heat", 120)
    assert ast["type"] == "damage_threshold"
    assert ast["hp_needed"] == 120
    assert "Varia Suit" in ast["suit_options"]
    assert "Gravity Suit" in ast["suit_options"]


def test_translate_damage_lava_emits_threshold():
    ast = edr.translate_damage("Lava", 300)
    assert ast["type"] == "damage_threshold"
    assert ast["hp_needed"] == 300
    assert "Varia Suit" in ast["suit_options"]


def test_translate_damage_cold_emits_threshold():
    # In Dread only Gravity Suit negates cold; Varia does NOT. Randovania's
    # logic DB never offers Varia as a cold-damage suit pass (only Gravity, or
    # a Suitless-trick + HP route), so Varia must not appear here.
    ast = edr.translate_damage("Cold", 80)
    assert ast["type"] == "damage_threshold"
    assert ast["hp_needed"] == 80
    assert ast["suit_options"] == ["Gravity Suit"]
    assert "Varia Suit" not in ast["suit_options"]


def test_translate_damage_generic_emits_no_suit_threshold():
    """Plain `Damage` (non-suit-typed: spike rooms, contact, falls) emits
    damage_threshold with empty suit_options carrying the HP amount.
    Downstream, compile_forward extracts amounts to derive per-region E-Tank
    floors via ``compute_region_etank_floors`` and then strips these no-suit
    nodes from per-location rules via ``_strip_no_suit_damage_thresholds``.
    Per-location HP gates deadlock AP's fill (Randovania pre-orders pickups
    for HP accumulation; AP's fill doesn't); per-region gating at the floor
    level is the right grain."""
    ast = edr.translate_damage("Damage", 60)
    assert ast["type"] == "damage_threshold"
    assert ast["suit_options"] == []
    assert ast["hp_needed"] == 60


def test_translate_damage_oob_still_impossible():
    """OOB stays IMPOSSIBLE — out-of-bounds damage implies an unintended
    route, and we don't want to silently encode tricks via HP budgets."""
    assert edr.translate_damage("OOB", 999) == edr.IMPOSSIBLE


def test_translate_damage_unknown_raises():
    with pytest.raises(edr.CompileError):
        edr.translate_damage("Acid", 100)


# ---- _translate_ammo ----

def test_translate_ammo_missile_emits_sum_with_starting_base():
    ast = edr._translate_ammo("MissileAmmo", 75)
    assert ast["type"] == "sum"
    assert ast["threshold"] == 75
    assert ast["base"] == edr._MISSILE_BASE_CAPACITY == 15
    term_names = {t["name"] for t in ast["terms"]}
    assert term_names == {"Missile Tank", "Missile+ Tank"}
    per_unit_by_name = {t["name"]: t["per_unit"] for t in ast["terms"]}
    assert per_unit_by_name["Missile Tank"] == 2
    assert per_unit_by_name["Missile+ Tank"] == 10


def test_translate_ammo_pb_ands_launcher_with_sum():
    """PBAmmo requires the launcher (Power Bomb item) AND a capacity sum.
    Without the AND, AP could route through "have 3 PB tanks in inventory
    but no launcher" — capacity without the ability to fire."""
    ast = edr._translate_ammo("PBAmmo", 5)
    assert ast["type"] == "and"
    kinds = [c["type"] for c in ast["items"]]
    assert "item" in kinds and "sum" in kinds
    launcher = next(c for c in ast["items"] if c["type"] == "item")
    assert launcher["name"] == "Power Bomb"
    sum_node = next(c for c in ast["items"] if c["type"] == "sum")
    assert sum_node["threshold"] == 5
    assert sum_node["base"] == 0
    per_unit_by_name = {t["name"]: t["per_unit"] for t in sum_node["terms"]}
    assert per_unit_by_name["Power Bomb"] == 2
    assert per_unit_by_name["Power Bomb Tank"] == 2


def test_translate_ammo_rejects_non_ammo():
    with pytest.raises(edr.CompileError):
        edr._translate_ammo("Morph", 1)


# ---- DNF round-trip for new atoms ----

def test_dnf_roundtrip_sum_atom():
    """sum nodes survive ast_to_dnf → dnf_to_ast unchanged. The DNF treats
    them as opaque single atoms, so the round-trip is a single-disjunct."""
    src = {"type": "sum",
           "terms": [{"name": "Missile Tank", "per_unit": 2}],
           "base": 15, "threshold": 30}
    dnf = edr.ast_to_dnf(src)
    out = edr.dnf_to_ast(dnf)
    assert out == src


def test_dnf_roundtrip_damage_threshold_atom():
    src = {"type": "damage_threshold",
           "suit_options": ["Varia Suit", "Gravity Suit"],
           "hp_needed": 200}
    dnf = edr.ast_to_dnf(src)
    out = edr.dnf_to_ast(dnf)
    assert out == src


def test_ast_to_dnf_rejects_stale_damage_node():
    """v1 schema's bare `damage` node must NOT silently pass — it would route
    through the old defensive TRIVIAL collapse and over-permit. The compiler
    raises so stale artifacts get caught at regen time."""
    with pytest.raises(ValueError, match="stale 'damage'"):
        edr.ast_to_dnf({"type": "damage", "kind": "Heat"})


