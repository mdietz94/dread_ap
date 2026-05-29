"""Gate B regression coverage for the trick-level option.

Pins the contract that three pre-baked rule files exist, share the pinned
commit, carry their trick_level + region_access, and that the loader
dispatches by level. These run without an Archipelago install.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.Rules import _TRICK_LEVEL_FILE, load_compiled_rules  # noqa: E402

DATA = ROOT / "data"
LEVEL_FILES = {
    1: "compiled_rules.json",
    2: "compiled_rules_l2.json",
    3: "compiled_rules_l3.json",
}


@pytest.fixture(scope="module")
def levels():
    return {n: json.loads((DATA / f).read_text()) for n, f in LEVEL_FILES.items()}


def test_three_trick_level_files_exist_and_parse(levels):
    for n, d in levels.items():
        for key in ("rules", "events", "region_access", "victory_condition"):
            assert key in d, f"L{n} missing {key}"
        assert d["trick_level"] == n, f"L{n} file has trick_level {d['trick_level']}"


def test_all_levels_share_pinned_commit(levels):
    commits = {d["pinned_commit"] for d in levels.values()}
    assert len(commits) == 1, f"trick-level files disagree on pinned_commit: {commits}"


def test_higher_levels_are_at_least_as_permissive(levels):
    """More tricks → at most as many impossible rules. Uses >= so the test
    still passes if the data has no per-level distinction for some rule."""
    def n_impossible(d):
        return sum(1 for r in d["rules"].values() if r.get("type") == "impossible")
    assert n_impossible(levels[1]) >= n_impossible(levels[2]) >= n_impossible(levels[3])


def test_levels_differ(levels):
    """The whole point of the option — the three files must not be identical
    (else picking a level changes nothing)."""
    assert levels[1]["rules"] != levels[2]["rules"]
    assert levels[2]["rules"] != levels[3]["rules"]


def test_loader_dispatches_by_level():
    assert _TRICK_LEVEL_FILE == LEVEL_FILES
    l1 = load_compiled_rules(1)
    l2 = load_compiled_rules(2)
    l3 = load_compiled_rules(3)
    assert l1["trick_level"] == 1
    assert l2["trick_level"] == 2
    assert l3["trick_level"] == 3
    # Unknown level falls back to canonical L1.
    assert load_compiled_rules(99)["trick_level"] == 1
