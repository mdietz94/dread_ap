"""Coverage for the Dreadvania-style item-pool options.

Two test surfaces:

  * **Data-table assertions** — items.json's per-item ``pool_count``
    defaults match the Randovania starter preset, and the main Power Bomb
    pickup is configured to grant 2 PBs by default (vanilla).
  * **Payload-routing assertions** — ``patcher_pipeline.placements_to_overrides``
    + ``merge_overrides`` correctly thread:
      - The ``Power Bomb`` placement's ``quantity`` into the patcher's
        pickup_resources (controls starting-PB ammo).
      - The ``starting_items`` dict, including the option-overridden
        ``ITEM_WEAPON_MISSILE_MAX``.
      - The ``cosmetic_combat["energy_per_tank"]`` field into the
        template's top-level ``energy_per_tank``.

A third surface — the actual ``DreadWorld.create_items`` pool builder —
needs the Archipelago runtime (``BaseClasses``, ``Options``,
``worlds.AutoWorld``) which is NOT installed in CI; tests that exercise
it install a thin AP stub up front and skip if the stub doesn't cover a
needed surface. Together they pin the user-visible behavior:

  - Default counts (no options) reproduce the Randovania starter pool.
  - Setting EnergyTankCount=N puts exactly N Energy Tanks in the pool.
  - ``power_bomb_tank_count=0 + starting_power_bombs=0`` raises OptionError.
  - MissileTankCount=0 means no Missile Tanks (incl. filler) — falls back to
    Energy Part / Power Bomb Tank.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))


# ---- fixtures -------------------------------------------------------------

@pytest.fixture(scope="module")
def items():
    return json.loads((DATA / "items.json").read_text())


# ---- data-table assertions ------------------------------------------------

# Randovania starter preset (verified against vendored patcher template).
_VANILLA_POOL_COUNTS = {
    "Energy Tank":     8,
    "Energy Part":     16,
    "Missile Tank":    60,
    "Missile+ Tank":   12,
    "Power Bomb Tank": 13,
}

_VANILLA_PICKUP_QUANTITIES = {
    # main Power Bomb grants 2 PBs (weapon + 2 starting ammo)
    "Power Bomb":      2,
    # each Power Bomb Tank pickup is +1 capacity (upstream default)
    "Power Bomb Tank": 1,
    # each Missile Tank is +2 missile capacity
    "Missile Tank":    2,
    # each Missile+ Tank is +10 missile capacity
    "Missile+ Tank":   10,
}


def test_default_pool_counts_match_randovania(items):
    by_name = {it["name"]: it for it in items}
    for name, expected in _VANILLA_POOL_COUNTS.items():
        assert by_name[name]["pool_count"] == expected, (
            f"{name}: pool_count={by_name[name]['pool_count']} (expected {expected})"
        )


def test_default_pickup_quantities_match_randovania(items):
    by_name = {it["name"]: it for it in items}
    for name, expected in _VANILLA_PICKUP_QUANTITIES.items():
        assert by_name[name]["quantity"] == expected, (
            f"{name}: quantity={by_name[name]['quantity']} (expected {expected})"
        )


def test_unique_progression_items_have_pool_count_one(items):
    """Every progression item that isn't a tank/expansion should have
    pool_count=1 (one copy in the pool by default)."""
    tank_like = set(_VANILLA_POOL_COUNTS)
    for it in items:
        if it["name"].startswith("Event: "):
            continue
        if it["name"].startswith("Metroid DNA"):
            continue
        if it["name"] in tank_like:
            continue
        assert it["pool_count"] == 1, (
            f"{it['name']}: expected pool_count=1, got {it['pool_count']}"
        )


# ---- patcher pipeline routing --------------------------------------------

def _make_placement(scenario: str, actor: str, *, ap_item_name: str,
                    patcher_item_id: str, quantity: int) -> dict:
    """Synthetic placement matching DreadWorld._build_placements_payload shape."""
    return {
        "location_name": f"Test: {actor}",
        "scenario": scenario,
        "actor": actor,
        "pickup_type": "actor",
        "pickup_index": 0,
        "ap_item_name": ap_item_name,
        "patcher_item_id": patcher_item_id,
        "quantity": quantity,
        "recipient_slot_name": "TestSlot",
        "is_own_player": True,
    }


def _build_placements(*, starting_missiles: int = 15, energy_per_tank: int = 100,
                      power_bomb_quantity: int = 2,
                      pb_placement_actor: str = "item_powerbomb") -> dict:
    """Build a placements payload with one Power Bomb placement, mirroring the
    shape DreadWorld._build_placements_payload emits."""
    return {
        "slot_name": "TestSlot",
        "seed_id": "TEST0001",
        "starting_area": 0,
        "include_boss_pickups": True,
        "starting_items": {
            "ITEM_FLOOR_SLIDE": 1,
            "ITEM_SONAR": 1,
            "ITEM_WEAPON_MISSILE_MAX": starting_missiles,
        },
        "cosmetic_combat": {
            "energy_per_tank": energy_per_tank,
        },
        "required_artifacts": 3,
        "placements": [
            _make_placement(
                # Use a real template (scenario, actor) so merge_overrides finds it.
                scenario="s030_baselab", actor="item_missiletank_001",
                ap_item_name="Power Bomb",
                patcher_item_id="ITEM_WEAPON_POWER_BOMB",
                quantity=power_bomb_quantity,
            ),
        ],
    }


def test_starting_power_bombs_quantity_routes_to_pickup_resource():
    """The Power Bomb placement's quantity (controlled by StartingPowerBombs)
    must land in the patcher's pickup_resources for that location."""
    from dread.patcher_pipeline import placements_to_overrides

    payload = _build_placements(power_bomb_quantity=4)
    overrides = placements_to_overrides(payload)
    key = "s030_baselab/item_missiletank_001"
    assert key in overrides["pickup_resources"]
    resources = overrides["pickup_resources"][key]
    assert resources == [[{"item_id": "ITEM_WEAPON_POWER_BOMB", "quantity": 4}]]


def test_starting_missiles_routes_to_template_starting_items():
    from dread.patcher_pipeline import (
        load_starter_template, placements_to_overrides, merge_overrides,
    )

    payload = _build_placements(starting_missiles=42)
    overrides = placements_to_overrides(payload)
    merged = merge_overrides(load_starter_template(), overrides)
    assert merged["starting_items"]["ITEM_WEAPON_MISSILE_MAX"] == 42


def test_energy_per_tank_routes_to_top_level_template_field():
    """EnergyPerTank flows: option -> cosmetic_combat[energy_per_tank] ->
    COSMETIC_COMBAT_PATHS -> top-level template field."""
    from dread.patcher_pipeline import (
        COSMETIC_COMBAT_PATHS, load_starter_template,
        placements_to_overrides, merge_overrides,
    )

    # COSMETIC_COMBAT_PATHS must include the new entry, pointing at the
    # top-level template key.
    assert COSMETIC_COMBAT_PATHS.get("energy_per_tank") == ("energy_per_tank",)

    payload = _build_placements(energy_per_tank=250)
    overrides = placements_to_overrides(payload)
    merged = merge_overrides(load_starter_template(), overrides)
    assert merged["energy_per_tank"] == 250


def test_default_payload_preserves_template_energy_per_tank():
    """Backward-compat: a payload that omits energy_per_tank from
    cosmetic_combat must leave the template's default (100) untouched."""
    from dread.patcher_pipeline import (
        load_starter_template, placements_to_overrides, merge_overrides,
    )

    payload = _build_placements()
    # Strip our new key to simulate an older payload.
    payload["cosmetic_combat"].pop("energy_per_tank", None)
    overrides = placements_to_overrides(payload)
    merged = merge_overrides(load_starter_template(), overrides)
    # Template default is 100 (vanilla Randovania).
    assert merged["energy_per_tank"] == 100


# ---- create_items behavior (uses AP runtime stubs) -----------------------

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
           "create_items tests require it.",
)


class _FakeMultiWorld:
    """Bare-minimum MultiWorld stand-in for create_items."""

    def __init__(self, seed_name: str = "TEST"):
        self.itempool: list = []
        self.precollected_items: list = []
        self.seed_name = seed_name

    def push_precollected(self, item) -> None:
        self.precollected_items.append(item)


def _build_world(**option_overrides):
    """Construct a DreadWorld bound to a fake multiworld, with the given
    option overrides applied. Returns (world, multiworld)."""
    from dread.Options import DreadOptions
    from dread.World import DreadWorld

    mw = _FakeMultiWorld()

    # Build a DreadOptions instance with defaults, then override.
    opts = DreadOptions.from_any({})  # type: ignore[attr-defined]
    for key, value in option_overrides.items():
        getattr(opts, key).value = value

    world = DreadWorld(mw, player=1)  # type: ignore[call-arg]
    world.options = opts
    return world, mw


@pytestmark_runtime
def test_pool_total_equals_non_event_locations():
    world, mw = _build_world()
    world.create_items()
    # Sum: count of items in pool; should equal non-event location count (149).
    from dread.Locations import location_table
    target = sum(1 for l in location_table if l.pickup_type != "event")
    assert len(mw.itempool) == target


@pytestmark_runtime
def test_default_pool_has_randovania_counts():
    world, mw = _build_world()
    world.create_items()
    counts: dict[str, int] = {}
    for item in mw.itempool:
        counts[item.name] = counts.get(item.name, 0) + 1
    for name, expected in _VANILLA_POOL_COUNTS.items():
        assert counts.get(name, 0) >= expected, (
            f"{name}: pool count {counts.get(name, 0)} < expected {expected}"
        )
    # Main Power Bomb appears exactly once in the pool.
    assert counts.get("Power Bomb", 0) == 1
    # DNA: default RequiredArtifacts=3 → exactly 3 DNA items.
    dna_total = sum(c for n, c in counts.items() if n.startswith("Metroid DNA"))
    assert dna_total == 3


@pytestmark_runtime
def test_energy_tank_count_drives_pool():
    world, mw = _build_world(energy_tank_count=4)
    world.create_items()
    n = sum(1 for it in mw.itempool if it.name == "Energy Tank")
    assert n == 4


@pytestmark_runtime
def test_zero_power_bombs_combo_raises():
    from Options import OptionError
    world, _ = _build_world(power_bomb_tank_count=0, starting_power_bombs=0)
    with pytest.raises(OptionError):
        world.create_items()


@pytestmark_runtime
def test_filler_respects_missile_tank_zero():
    """MissileTankCount=0 → no Missile Tanks in the pool, period
    (filler falls back to Energy Part)."""
    world, mw = _build_world(missile_tank_count=0)
    world.create_items()
    mt = sum(1 for it in mw.itempool if it.name == "Missile Tank")
    assert mt == 0
    # Confirm Energy Part picked up the slack (some present).
    ep = sum(1 for it in mw.itempool if it.name == "Energy Part")
    assert ep >= 16  # the default count, plus any filler padding


@pytestmark_runtime
def test_overflow_raises_option_error_when_unrecoverable():
    """Set every tank to its max — even after trimming, the pool may exceed
    149 slots. The error should be clear about which knobs to lower."""
    from Options import OptionError
    world, _ = _build_world(
        energy_tank_count=20,
        energy_part_count=64,
        missile_tank_count=120,
        missile_plus_tank_count=20,
        power_bomb_tank_count=20,
    )
    with pytest.raises(OptionError):
        world.create_items()
