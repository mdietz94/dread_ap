"""Coverage for the in-game Nav Station hint generation.

``DreadWorld._generate_nav_hints`` (run from ``pre_output``) turns real
cross-world placement facts into the text baked onto the game's Nav Station
plaques. No AP server hints are registered — Nav Stations have no AP location
check, so there is no in-game event to gate the broadcast on. The patcher-side
consumption (``_apply_nav_hints``) is covered by
``scripts/tests/test_build_patcher_json.py``; this file pins the world-side
generation.

The generator needs the Archipelago runtime (``BaseClasses`` for real
``Item`` objects whose ``.advancement`` flag drives the "prefer interesting
items" pass), so the tests skip when it isn't importable — same convention as
``test_item_pool.py``.
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
           "nav-hint generation tests require it.",
)


class _Loc:
    """Minimal Location stand-in: only the attributes the generator reads."""

    def __init__(self, name: str, player: int, address, item):
        self.name = name
        self.player = player
        self.address = address
        self.item = item


class _NavFakeMW:
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


def _default_options():
    """A DreadOptions instance at defaults, built through the public Option API
    (each field's resolved class + its declared default)."""
    import typing

    from dread.Options import DreadOptions

    hints = typing.get_type_hints(DreadOptions)
    return DreadOptions(**{name: cls.from_any(cls.default)
                           for name, cls in hints.items()})


def _make_world(filled: dict[int, list]):
    from dread.World import DreadWorld

    mw = _NavFakeMW()
    mw._filled = filled
    world = DreadWorld(mw, player=1)  # type: ignore[call-arg]
    world.options = _default_options()
    world.random = random.Random(0)
    return world, mw


def _scenario():
    """A 2-slot placement rich enough to fill all NAV_HINT_COUNT plaques.

    Player 1 (the slot generating hints) owns a set of unique progression
    upgrades; some land in its own world, some in player 2's world. Player 2's
    items also land on a few of player 1's locations (so location-style hints
    exercise the cross-recipient branch)."""
    from BaseClasses import Item, ItemClassification as IC

    prog = IC.progression

    def item(name, player):
        return Item(name, prog, hash(name) & 0xFFFF, player)

    p1_upgrades = ["Screw Attack", "Grapple Beam", "Speed Booster", "Spider Magnet",
                   "Storm Missile", "Gravity Suit", "Phantom Cloak", "Spin Boost"]

    p1_locs: list[_Loc] = []
    # Half of player 1's upgrades sit in player 1's own world...
    for i, name in enumerate(p1_upgrades[:4]):
        p1_locs.append(_Loc(f"Artaria: Spot{i}", 1, 1000 + i, item(name, 1)))
    # ...and a few of player 1's locations hold player 2's items.
    for i in range(3):
        p1_locs.append(_Loc(f"Cataris: Spot{i}", 1, 2000 + i, item(f"P2 Gadget {i}", 2)))

    p2_locs: list[_Loc] = []
    # The other half of player 1's upgrades landed in player 2's world.
    for i, name in enumerate(p1_upgrades[4:]):
        p2_locs.append(_Loc(f"Burenia: Spot{i}", 2, 3000 + i, item(name, 1)))
    # Player 2 also has its own items (irrelevant to player 1's hints).
    p2_locs.append(_Loc("Elun: Own", 2, 3999, item("P2 Local", 2)))

    return {1: p1_locs, 2: p2_locs}


@pytestmark_runtime
def test_nav_hints_fill_all_plaques_with_text():
    from dread.World import NAV_HINT_COUNT

    world, _ = _make_world(_scenario())
    hints = world._generate_nav_hints()

    assert len(hints) == NAV_HINT_COUNT
    for h in hints:
        text = h["text"]
        assert isinstance(text, str) and text.strip()
        # Renders with the game's colour escapes like the starter preset.
        assert "{c1}" in text and "{c0}" in text


@pytestmark_runtime
def test_nav_hints_do_not_register_ap_server_hints():
    """Nav Station hints must NOT populate start_location_hints or start_hints.
    There is no in-game AP location check for a Nav Station visit, so there is
    no event to gate the broadcast on — adding them would reveal hint content
    to all players immediately at session start."""
    world, _ = _make_world(_scenario())
    world._generate_nav_hints()

    assert not world.options.start_location_hints.value, \
        "start_location_hints must stay empty (no Nav Station AP locations)"
    assert not world.options.start_hints.value, \
        "start_hints must stay empty (no Nav Station AP locations)"


@pytestmark_runtime
def test_nav_hints_are_deterministic_under_seed():
    a, _ = _make_world(_scenario())
    b, _ = _make_world(_scenario())
    assert a._generate_nav_hints() == b._generate_nav_hints()


@pytestmark_runtime
def test_nav_hints_no_duplicate_locations():
    """No single (player, location) is hinted by more than one plaque."""
    world, mw = _make_world(_scenario())
    hints = world._generate_nav_hints()
    # Texts must be distinct (each derives from a distinct location).
    texts = [h["text"] for h in hints]
    assert len(texts) == len(set(texts))


def _dna_scenario(n_dna: int):
    """Player-1 placement that includes n_dna Metroid DNA items scattered across
    both worlds, plus enough filler to exercise the rest of the plaques."""
    from BaseClasses import Item, ItemClassification as IC

    def item(name, player):
        return Item(name, IC.progression, hash(name) & 0xFFFF, player)

    p1_locs: list[_Loc] = []
    p2_locs: list[_Loc] = []
    for k in range(1, n_dna + 1):
        dst = p1_locs if k % 2 else p2_locs
        pl = 1 if k % 2 else 2
        dst.append(_Loc(f"Boss Spot {k}", pl, 5000 + k, item(f"Metroid DNA {k}", 1)))
    # Some non-DNA filler so the remaining plaques have material.
    for i in range(6):
        p1_locs.append(_Loc(f"Artaria: Filler{i}", 1, 6000 + i, item(f"Upgrade {i}", 1)))
    return {1: p1_locs, 2: p2_locs}


@pytestmark_runtime
def test_hint_all_dna_surfaces_every_dna_location():
    """hint_all_dna ON: every required Metroid DNA's location is hinted (within
    the plaque budget)."""
    from dread.World import NAV_HINT_COUNT

    world, _ = _make_world(_dna_scenario(5))
    world.options.required_artifacts.value = 5
    world.options.hint_all_dna.value = 1
    hints = world._generate_nav_hints()

    dna_hints = [h["text"] for h in hints if "Metroid DNA" in h["text"]]
    for k in range(1, 6):
        assert any(f"Metroid DNA {k}{{c0}}" in t for t in dna_hints), \
            f"Metroid DNA {k} location not hinted"
    assert len(hints) == NAV_HINT_COUNT


@pytestmark_runtime
def test_hint_all_dna_off_does_not_force_dna_hints():
    """hint_all_dna OFF: DNA is not force-hinted (only the usual mixed hints,
    which may include a DNA item incidentally but not all of them)."""
    world, _ = _make_world(_dna_scenario(5))
    world.options.required_artifacts.value = 5
    world.options.hint_all_dna.value = 0
    hints = world._generate_nav_hints()
    forced = [h["text"] for h in hints
              if h["text"].startswith("Your {c1}Metroid DNA")]
    assert len(forced) < 5, "OFF must not force every DNA location hint"
