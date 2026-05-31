"""Coverage for the softlock_locks.json table + Rules.set_rules' use of it.

Background: scripts/extract_dread_rules.py::_strip_fill_fragile_items strips
single-pool-copy progression items from per-pickup escape rules so AP fill
can converge. The strip would otherwise leave 12 actor pickups where the
escape requires an item AP can now legally place elsewhere — see the
research report in PR #48 follow-up. This file pins the response:

  * Each "lock" entry names a real location and a single-pool-copy progression
    item that exists in items.json (so place_locked_item has something to
    pull from the pool).
  * Each "filler_only" entry names a real location and a sibling lock to
    share with — the sibling's locked item is what would otherwise satisfy
    this room's escape.
  * The locations are disjoint from the DNA prefer_bosses lock pool (all
    actor pickups vs. boss/EMMI/cutscene/corex pickups).
  * The combined lock + filler-only count is exactly 12, matching the
    diagnostic at trick L1 (the broadest user surface).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

DATA = ROOT / "data"


@pytest.fixture(scope="module")
def softlock_table():
    return json.loads((DATA / "softlock_locks.json").read_text())


@pytest.fixture(scope="module")
def entries(softlock_table):
    return softlock_table["entries"]


@pytest.fixture(scope="module")
def locations():
    return json.loads((DATA / "locations.json").read_text())


@pytest.fixture(scope="module")
def items():
    return json.loads((DATA / "items.json").read_text())


def test_entry_count_matches_diagnostic(entries):
    """Exactly 12 candidate rooms at trick L1 (see PR #48 follow-up). Drift
    means either the diagnostic re-ran with a different answer (regenerate
    softlock_locks.json) or the table got out of sync."""
    assert len(entries) == 12


def test_every_location_exists(entries, locations):
    by_name = {l["name"]: l for l in locations}
    missing = [e["location"] for e in entries if e["location"] not in by_name]
    assert not missing, f"softlock_locks references unknown locations: {missing}"


def test_every_location_is_an_actor_pickup(entries, locations):
    """The whole strip risk lives in actor pickups (boss/EMMI/cutscene/corex
    were already skipped by the escape pass). Anything else here is a bug."""
    by_name = {l["name"]: l for l in locations}
    for e in entries:
        loc = by_name[e["location"]]
        assert loc["pickup_type"] == "actor", (
            f"{e['location']}: pickup_type={loc['pickup_type']} "
            f"— softlock_locks should only target actor pickups"
        )


def test_lock_items_exist_and_are_single_copy(entries, items):
    """Each lock entry's item must (a) be a real AP item, and (b) have
    pool_count=1 — otherwise the strip wouldn't have removed it (multi-copy
    items aren't fragile)."""
    by_name = {it["name"]: it for it in items}
    for e in entries:
        if e["mode"] != "lock":
            continue
        item = by_name.get(e["item"])
        assert item is not None, f"unknown item: {e['item']}"
        assert int(item.get("pool_count", 0)) == 1, (
            f"{e['item']}: pool_count={item.get('pool_count')} "
            f"— lock target must be fill-fragile (single copy)"
        )


def test_filler_only_entries_share_with_a_real_lock(entries):
    """Every filler_only entry must point at a sibling lock entry whose
    item is what would have satisfied this room's escape."""
    lock_locs = {e["location"] for e in entries if e["mode"] == "lock"}
    for e in entries:
        if e["mode"] != "filler_only":
            continue
        sibling = e.get("shares_with")
        assert sibling is not None, (
            f"{e['location']}: filler_only entry missing shares_with"
        )
        assert sibling in lock_locs, (
            f"{e['location']}: shares_with={sibling!r} not in lock set"
        )


def test_modes_are_well_known(entries):
    for e in entries:
        assert e["mode"] in ("lock", "filler_only"), (
            f"{e['location']}: unknown mode {e['mode']!r}"
        )


def test_no_collision_with_dna_prefer_bosses(entries, locations):
    """DNA prefer_bosses locks N items to boss/EMMI/cutscene/corex locations
    (Rules.py:225-245). Our locks operate on actor pickups. Verify they
    cannot fight over a location by construction."""
    by_name = {l["name"]: l for l in locations}
    boss_types = {"corpius", "emmi", "cutscene", "corex"}
    for e in entries:
        pt = by_name[e["location"]]["pickup_type"]
        assert pt not in boss_types, (
            f"{e['location']}: pickup_type={pt} collides with DNA "
            f"prefer_bosses domain"
        )


def test_lock_items_are_progression(items, entries):
    """A lock target must be classified `progression` or
    `progression_skip_balancing` — otherwise the worth-locking premise is
    wrong (filler items don't gate escape)."""
    by_name = {it["name"]: it for it in items}
    progression_classes = {"progression", "progression_skip_balancing"}
    for e in entries:
        if e["mode"] != "lock":
            continue
        cls = by_name[e["item"]].get("classification")
        assert cls in progression_classes, (
            f"{e['item']}: classification={cls!r} — lock target must be "
            f"progression"
        )


def test_expected_lock_pairs():
    """Pin the exact (location, item) pairs we lock. Regression guard for the
    set chosen from the trick-L1 diagnostic; any change to this list should
    re-run scripts/diagnose_reverse_reachability.py first."""
    expected = {
        ("Artaria: GrappleBeam",       "Grapple Beam"),
        ("Artaria: ScrewAttack",       "Screw Attack"),
        ("Artaria: VARIA_GEN_001",     "Varia Suit"),
        ("Artaria: missiletank_002",   "Gravity Suit"),
        ("Dairon: powerbombtank_002",  "Bomb"),
        ("Ferenia: energyfragment",    "Storm Missile"),
        ("Ferenia: energyfragment_002","Wide Beam"),
        ("Ghavoran: missiletank",      "Ice Missile"),
    }
    table = json.loads((DATA / "softlock_locks.json").read_text())
    actual = {(e["location"], e["item"])
              for e in table["entries"] if e["mode"] == "lock"}
    assert actual == expected


def test_expected_filler_only_locations():
    expected = {
        "Artaria: energytank_000",
        "Ferenia: missiletank_004",
        "Ghavoran: missiletank_006",
        "Ghavoran: powerup_sonar",
    }
    table = json.loads((DATA / "softlock_locks.json").read_text())
    actual = {e["location"]
              for e in table["entries"] if e["mode"] == "filler_only"}
    assert actual == expected
