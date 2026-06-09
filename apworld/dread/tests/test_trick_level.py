"""Coverage for the per-trick configuration (v3 symbolic-trick model).

Since v3 there is a SINGLE compiled artifact that keeps trick requirements
symbolic (``{"type":"trick","name","level"}``); the effective per-trick level is
resolved at AP-generation time from the global ``Trick Level`` baseline plus any
per-trick overrides. These tests pin that contract and run without an
Archipelago install (no ``Options`` import — a tiny fake options object stands in
for the dataclass).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread import Tricks  # noqa: E402
from dread.Rules import compile_to_lambda  # noqa: E402

DATA = ROOT / "data"


def _fake_options(global_level: int, **overrides) -> SimpleNamespace:
    """Build a stand-in options object: ``.trick_level`` + one attr per visible
    trick, each exposing ``.value``. ``overrides`` keys are trick attrs (e.g.
    ``trick_wall_jump=0``)."""
    ns = SimpleNamespace()
    ns.trick_level = SimpleNamespace(value=global_level)
    for t in Tricks.VISIBLE_TRICKS:
        ns.__dict__[t.attr] = SimpleNamespace(
            value=overrides.get(t.attr, Tricks.FOLLOW_GLOBAL))
    return ns


# --------------------------------------------------------------------------- #
# Trick metadata table
# --------------------------------------------------------------------------- #

def test_trick_table_shape():
    assert len(Tricks.DREAD_TRICKS) == 26
    assert len(Tricks.VISIBLE_TRICKS) == 25  # Suitless hidden
    # attrs unique and namespaced
    attrs = [t.attr for t in Tricks.VISIBLE_TRICKS]
    assert len(set(attrs)) == len(attrs)
    assert all(a.startswith("trick_") for a in attrs)
    suitless = next(t for t in Tricks.DREAD_TRICKS if t.short_name == "Suitless")
    assert suitless.hidden


# --------------------------------------------------------------------------- #
# effective_trick_levels: baseline / override / hidden
# --------------------------------------------------------------------------- #

def test_follow_global_default_uses_baseline():
    lv = Tricks.effective_trick_levels(_fake_options(2))
    assert lv["Walljump"] == 2
    assert lv["Movement"] == 2


def test_hidden_trick_always_follows_global():
    lv = Tricks.effective_trick_levels(_fake_options(3))
    assert lv["Suitless"] == 3  # no option, tracks baseline


def test_override_beats_global():
    lv = Tricks.effective_trick_levels(_fake_options(1, trick_wall_jump=3))
    assert lv["Walljump"] == 3
    assert lv["Movement"] == 1  # untouched → baseline


def test_disabled_override():
    lv = Tricks.effective_trick_levels(_fake_options(3, trick_wall_jump=0))
    assert lv["Walljump"] == 0


# --------------------------------------------------------------------------- #
# compile_to_lambda resolves symbolic trick atoms
# --------------------------------------------------------------------------- #

def test_trick_atom_assumed_when_effective_level_high_enough():
    ast = {"type": "trick", "name": "Walljump", "level": 2}
    assert compile_to_lambda(ast, 1, {"Walljump": 2})(object()) is True
    assert compile_to_lambda(ast, 1, {"Walljump": 3})(object()) is True


def test_trick_atom_rejected_when_below_effective_level():
    ast = {"type": "trick", "name": "Walljump", "level": 2}
    assert compile_to_lambda(ast, 1, {"Walljump": 1})(object()) is False
    assert compile_to_lambda(ast, 1, {"Walljump": 0})(object()) is False


def test_trick_atom_missing_map_is_disabled():
    ast = {"type": "trick", "name": "Walljump", "level": 1}
    assert compile_to_lambda(ast, 1, None)(object()) is False
    assert compile_to_lambda(ast, 1, {})(object()) is False


def test_trick_inside_or_opens_alternate_path():
    # OR(item Morph, trick Walljump@2): reachable via Morph regardless, and via
    # the trick only when enabled.
    ast = {"type": "or", "items": [
        {"type": "item", "name": "Morph Ball", "amount": 1},
        {"type": "trick", "name": "Walljump", "level": 2},
    ]}
    class _State:
        def __init__(self, has): self._has = has
        def has(self, name, player, amount=1): return self._has
    # No Morph, trick disabled → unreachable
    assert compile_to_lambda(ast, 1, {"Walljump": 0})(_State(False)) is False
    # No Morph, trick enabled → reachable via trick
    assert compile_to_lambda(ast, 1, {"Walljump": 2})(_State(False)) is True
    # Has Morph, trick disabled → reachable via item
    assert compile_to_lambda(ast, 1, {"Walljump": 0})(_State(True)) is True


# --------------------------------------------------------------------------- #
# Graph artifact shape
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def graph():
    path = DATA / "logic_graph.json"
    if not path.exists():
        pytest.skip("logic_graph.json not yet generated (run extract_dread_rules.py)")
    return json.loads(path.read_text())


def _iter_atoms(ast):
    yield ast
    for child in ast.get("items", []):
        yield from _iter_atoms(child)


def test_graph_carries_symbolic_trick_atoms(graph):
    """Trick requirements survive into the graph artifact as symbolic atoms."""
    seen_tricks = set()
    for _csrc, _cdst, rule in graph.get("entrances", []):
        for node in _iter_atoms(rule):
            if node.get("type") == "trick":
                seen_tricks.add(node["name"])
                assert isinstance(node["level"], int)
    assert seen_tricks, "no symbolic trick atoms found in graph entrances"
    known = {t.short_name for t in Tricks.DREAD_TRICKS}
    assert seen_tricks <= known, f"unknown tricks: {seen_tricks - known}"
