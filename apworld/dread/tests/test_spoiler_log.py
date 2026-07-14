"""Coverage for the end-credits "Major Item Locations" spoiler log.

``DreadWorld._generate_spoiler_log`` (run from ``pre_output``) replaces the
starter preset's baked example placements — false for any AP seed — with where
this slot's major items actually landed. open-dread-rando's ``patch_credits``
renders the dict after the randomizer credits, so it's shown only once the run
is beaten. The patcher-side consumption (``merge_overrides`` replacing the
template key, blank-on-empty) is covered by
``scripts/tests/test_build_patcher_json.py``; this file pins the world-side
generation.

Same AP-runtime gate and fake-multiworld harness as ``test_nav_hints.py``.
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))


def _ap_runtime_available() -> bool:
    try:
        import BaseClasses  # noqa: F401
        import Options  # noqa: F401
        from worlds.AutoWorld import World  # noqa: F401
    except ImportError:
        return False
    return True


pytestmark_runtime = pytest.mark.skipif(
    not _ap_runtime_available(),
    reason="Archipelago runtime not installed (BaseClasses/Options/worlds.AutoWorld); "
           "spoiler-log generation tests require it.",
)


class _Loc:
    """Minimal Location stand-in: only the attributes the generator reads."""

    def __init__(self, name: str, player: int, address, item):
        self.name = name
        self.player = player
        self.address = address
        self.item = item


class _FakeMW:
    """MultiWorld stand-in exposing just the surface the generator touches."""

    def __init__(self):
        self.itempool: list = []
        self.precollected_items: list = []
        self.seed_name = "TEST"
        self.random = random.Random(1)
        self.per_slot_randoms: dict[int, random.Random] = {}
        self._names = {1: "SamusA", 2: "SamusB"}
        self._filled: dict[int, list[_Loc]] = {}

    def push_precollected(self, item) -> None:
        self.precollected_items.append(item)

    def get_player_name(self, player: int) -> str:
        return self._names[player]

    def get_filled_locations(self, player=None):
        if player is None:
            return [loc for locs in self._filled.values() for loc in locs]
        return self._filled.get(player, [])


def _make_world(filled: dict[int, list]):
    from dread.World import DreadWorld

    mw = _FakeMW()
    mw._filled = filled
    world = DreadWorld(mw, player=1)  # type: ignore[call-arg]
    world.random = random.Random(0)
    return world, mw


def _item(name, player):
    from BaseClasses import Item, ItemClassification as IC
    return Item(name, IC.progression, hash(name) & 0xFFFF, player)


@pytestmark_runtime
def test_majors_appear_with_their_real_locations():
    """Unique single-copy abilities land in the log; a cross-world placement
    renders as "<player>'s <location>"."""
    filled = {
        1: [_Loc("Artaria: Charge Beam Room", 1, 1000, _item("Grapple Beam", 1))],
        2: [_Loc("Cool Zone", 2, 2000, _item("Screw Attack", 1))],
    }
    world, _ = _make_world(filled)
    log = world._generate_spoiler_log()
    assert log["Grapple Beam"] == "Artaria: Charge Beam Room"
    assert log["Screw Attack"] == "SamusB's Cool Zone"


@pytestmark_runtime
def test_tanks_ammo_and_other_slots_items_excluded():
    """Multi-copy tanks/ammo, chain upgrades, other slots' items, and
    addressless (event) locations never reach the credits."""
    filled = {
        1: [
            _Loc("Artaria: Spot A", 1, 1000, _item("Missile Tank", 1)),
            _Loc("Artaria: Spot B", 1, 1001, _item("Energy Tank", 1)),
            _Loc("Artaria: Spot C", 1, 1002, _item("Flash Shift Upgrade", 1)),
            # Another slot's major sitting in our world — theirs, not ours.
            _Loc("Artaria: Spot D", 1, 1003, _item("P2 Gadget", 2)),
            # Our major on an addressless (event) location can't be reported.
            _Loc("Event: Foo", 1, None, _item("Morph Ball", 1)),
        ],
    }
    world, _ = _make_world(filled)
    assert world._generate_spoiler_log() == {}


@pytestmark_runtime
def test_progressive_copies_newline_join_sorted():
    """Multi-copy progressive items join their locations with newlines,
    matching the template's format, sorted for determinism."""
    filled = {
        1: [_Loc("Cataris: Spot Z", 1, 1000, _item("Progressive Beam", 1)),
            _Loc("Artaria: Spot A", 1, 1001, _item("Progressive Beam", 1))],
        2: [_Loc("Button", 2, 2000, _item("Progressive Beam", 1))],
    }
    world, _ = _make_world(filled)
    log = world._generate_spoiler_log()
    assert log["Progressive Beam"] == (
        "Artaria: Spot A\nCataris: Spot Z\nSamusB's Button"
    )


@pytestmark_runtime
def test_placed_dna_appears():
    filled = {
        1: [_Loc("Artaria: Corpius Arena", 1, 1000, _item("Metroid DNA 2", 1))],
    }
    world, _ = _make_world(filled)
    log = world._generate_spoiler_log()
    assert log == {"Metroid DNA 2": "Artaria: Corpius Arena"}


@pytestmark_runtime
def test_entries_follow_item_table_order():
    """Deterministic entry order: items.json definition order (Morph Ball
    before Grapple Beam before the progressives), independent of fill order."""
    filled = {
        1: [
            _Loc("Spot A", 1, 1000, _item("Progressive Suit", 1)),
            _Loc("Spot B", 1, 1001, _item("Grapple Beam", 1)),
            _Loc("Spot C", 1, 1002, _item("Morph Ball", 1)),
        ],
    }
    world, _ = _make_world(filled)
    log = world._generate_spoiler_log()
    assert list(log) == ["Morph Ball", "Grapple Beam", "Progressive Suit"]
