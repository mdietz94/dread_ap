"""Unit tests for the upstream-data → AST compiler in
scripts/extract_dread_rules.py.

These cover the bits that are easy to test without the full Randovania logic
cache: the small pure helpers (translate_damage, _translate_ammo, the AMMO
set membership) and the schema sentinel on the emitted artifacts.
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
    ast = edr.translate_damage("Cold", 80)
    assert ast["type"] == "damage_threshold"
    assert ast["hp_needed"] == 80
    assert "Gravity Suit" in ast["suit_options"]
    assert "Varia Suit" in ast["suit_options"]


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


# ---- Schema sentinel ----

DATA = Path(__file__).resolve().parents[1] / "data"


@pytest.mark.parametrize("fn", [
    "compiled_rules.json", "compiled_rules_l2.json", "compiled_rules_l3.json",
])
def test_compiled_artifact_carries_schema_version(fn):
    raw = json.loads((DATA / fn).read_text())
    assert raw.get("schema_version") == edr.SCHEMA_VERSION


# ---- Region E-Tank floors (option 1) ----

def test_location_easiest_hp_zero_when_disjunct_has_no_damage():
    """An OR with a damage-free disjunct returns 0 — that disjunct is the
    easiest path."""
    ast = {"type": "or", "items": [
        {"type": "item", "name": "Charge Beam", "amount": 1},  # no damage
        {"type": "and", "items": [
            {"type": "item", "name": "Morph Ball", "amount": 1},
            {"type": "damage_threshold", "suit_options": [], "hp_needed": 349},
        ]},
    ]}
    assert edr._location_easiest_hp(ast) == 0


def test_location_easiest_hp_max_within_and_min_across_or():
    """AND takes max (you face the worst hit on a path); OR takes min
    (cheapest path wins)."""
    ast = {"type": "or", "items": [
        {"type": "and", "items": [
            {"type": "damage_threshold", "suit_options": [], "hp_needed": 200},
            {"type": "damage_threshold", "suit_options": [], "hp_needed": 100},
        ]},  # this path max = 200
        {"type": "damage_threshold", "suit_options": [], "hp_needed": 150},
    ]}
    # Easiest: the second disjunct (150) vs first disjunct (200) → 150
    assert edr._location_easiest_hp(ast) == 150


def test_location_easiest_hp_ignores_suit_typed():
    """Suit-typed damage_threshold short-circuits on Varia/Gravity, so it
    contributes 0 to the no-suit-HP floor — a normal player has the suit by
    that point."""
    ast = {"type": "damage_threshold",
           "suit_options": ["Varia Suit"], "hp_needed": 500}
    assert edr._location_easiest_hp(ast) == 0


def test_compute_region_etank_floors_p75():
    """One-outlier region (24 zero, 1 hard) → P75=0 → no gate. All-hard
    region → P75=hard → gate."""
    rules = {}
    # Cataris: 24 trivial + 1 hard at 349 → P75 should be 0
    for i in range(24):
        rules[f"Cataris: easy_{i}"] = {"type": "trivial"}
    rules["Cataris: kraid"] = {"type": "damage_threshold",
                               "suit_options": [], "hp_needed": 349}
    # Hanubia: 4 hard locations all at 349 → P75 = 349 → gate
    for i in range(4):
        rules[f"Hanubia: hard_{i}"] = {"type": "damage_threshold",
                                       "suit_options": [], "hp_needed": 349}
    regions = ["Cataris", "Hanubia"]
    floors = edr.compute_region_etank_floors(rules, regions)
    assert floors["Cataris"] == 0
    assert floors["Hanubia"] == 3       # ceil((349-99)/100) = 3


def test_strip_no_suit_damage_thresholds():
    """Strip empties suit_options → TRIVIAL; preserves suit-typed nodes."""
    ast = {"type": "and", "items": [
        {"type": "damage_threshold", "suit_options": [], "hp_needed": 100},
        {"type": "damage_threshold", "suit_options": ["Varia Suit"],
         "hp_needed": 200},
        {"type": "item", "name": "Morph Ball", "amount": 1},
    ]}
    stripped = edr._strip_no_suit_damage_thresholds(ast)
    # The no-suit dthr is now TRIVIAL and folded out of the AND; the
    # suit-typed one survives, alongside Morph.
    assert stripped["type"] == "and"
    types = [c["type"] for c in stripped["items"]]
    assert "damage_threshold" in types
    assert "item" in types
    # And the surviving damage_threshold is the suit-typed one.
    dthr = next(c for c in stripped["items"] if c["type"] == "damage_threshold")
    assert dthr["suit_options"] == ["Varia Suit"]


def test_strip_replaces_pure_no_suit_with_trivial():
    """A bare no-suit damage_threshold becomes TRIVIAL outright."""
    ast = {"type": "damage_threshold", "suit_options": [], "hp_needed": 300}
    assert edr._strip_no_suit_damage_thresholds(ast) == edr.TRIVIAL


def test_compiled_artifacts_have_no_no_suit_damage_thresholds():
    """Post-strip, the on-disk artifacts must never contain a
    damage_threshold with empty suit_options. That node is the over-strict
    per-location HP gate; it survives compile_forward only as a transient
    used to derive region floors, then gets stripped."""
    import json
    for fn in ("compiled_rules.json", "compiled_rules_l2.json",
               "compiled_rules_l3.json"):
        raw = json.loads((DATA / fn).read_text())
        def walk(a):
            yield a
            for c in a.get("items", []):
                yield from walk(c)
        for loc, rule in raw["rules"].items():
            for n in walk(rule):
                if n.get("type") == "damage_threshold":
                    assert n.get("suit_options"), \
                        f"{fn} {loc}: no-suit damage_threshold survived strip"


def test_loader_refuses_mismatched_schema(monkeypatch):
    """A stale artifact (wrong schema_version) must fail loud — not silently
    route through the now-extinct `damage` branch (we removed the defensive
    `_const_true` fallthrough)."""
    sys.path.insert(0, str(ROOT))
    from dread.Rules import load_compiled_rules, EXPECTED_SCHEMA_VERSION

    real = json.loads((DATA / "compiled_rules.json").read_text())
    real["schema_version"] = EXPECTED_SCHEMA_VERSION + 99

    def fake_load_json(name):
        return real
    monkeypatch.setattr("dread.Rules.load_json", fake_load_json)
    with pytest.raises(RuntimeError, match="schema_version"):
        load_compiled_rules(1)
