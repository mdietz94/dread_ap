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
    """Every progression item that isn't a tank/expansion/upgrade should
    have pool_count=1 (one copy in the pool by default). The two chain-upgrade
    items have pool_count=0 — covered by test_upgrade_pool_count_is_zero."""
    multi_copy = set(_VANILLA_POOL_COUNTS) | {
        "Flash Shift Upgrade", "Speed Booster Upgrade",
    }
    for it in items:
        if it["name"].startswith("Metroid DNA"):
            continue
        if it["name"] in multi_copy:
            continue
        # Progressive items ship one copy per tier (validated in test_data_tables).
        if it.get("progression_tiers"):
            assert it["pool_count"] == len(it["progression_tiers"])
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
    must land in the patcher's pickup_resources as the ammo CAPACITY
    (ITEM_WEAPON_POWER_BOMB_MAX), alongside the weapon-unlock flag. Granting
    only ITEM_WEAPON_POWER_BOMB leaves the player at 0/0 ammo — an unusable
    power bomb that reads as "?" in the menu."""
    from dread.patcher_pipeline import placements_to_overrides

    payload = _build_placements(power_bomb_quantity=4)
    overrides = placements_to_overrides(payload)
    key = "s030_baselab/item_missiletank_001"
    assert key in overrides["pickup_resources"]
    resources = overrides["pickup_resources"][key]
    assert resources == [[
        {"item_id": "ITEM_WEAPON_POWER_BOMB", "quantity": 1},
        {"item_id": "ITEM_WEAPON_POWER_BOMB_MAX", "quantity": 4},
    ]]


@pytest.mark.parametrize(
    "ap_item_name, patcher_item_id, amount",
    [
        ("Missile Tank", "ITEM_WEAPON_MISSILE_MAX", 5),
        ("Missile+ Tank", "ITEM_WEAPON_MISSILE_MAX", 25),
        ("Power Bomb Tank", "ITEM_WEAPON_POWER_BOMB_MAX", 3),
        ("Flash Shift Upgrade", "ITEM_UPGRADE_FLASH_SHIFT_CHAIN", 2),
        # Speed Booster Upgrade has no amount option (standard pickup), so it is
        # not parametrized here.
    ],
)
def test_ammo_amount_quantity_routes_to_pickup_resource(
        ap_item_name, patcher_item_id, amount):
    """Each per-pickup ammo/upgrade amount option (mirroring Randovania's
    `ammo_count`) must land in the patcher's pickup_resources as the granted
    capacity. The amount rides each placement's `quantity`, which
    placements_to_overrides expands via pickup_resource_stage."""
    from dread.patcher_pipeline import placements_to_overrides

    payload = _build_placements()
    payload["placements"] = [
        _make_placement(
            scenario="s030_baselab", actor="item_missiletank_001",
            ap_item_name=ap_item_name,
            patcher_item_id=patcher_item_id,
            quantity=amount,
        ),
    ]
    overrides = placements_to_overrides(payload)
    key = "s030_baselab/item_missiletank_001"
    assert overrides["pickup_resources"][key] == [
        [{"item_id": patcher_item_id, "quantity": amount}]
    ]


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
        from random import Random

        self.itempool: list = []
        self.precollected_items: list = []
        self.seed_name = seed_name
        # AutoWorld.World.__init__ seeds its per-world RNG from this and
        # records it back into per_slot_randoms.
        self.random = Random(0)
        self.per_slot_randoms: dict = {}

    def push_precollected(self, item) -> None:
        self.precollected_items.append(item)

    def get_player_name(self, player) -> str:
        return "TestSlot"

    def get_locations(self, player):
        # _build_placements_payload iterates this; an empty world is enough to
        # exercise the slot_data fields (item_amounts, starting_items) that don't
        # depend on placed items.
        return []


def _build_world(**option_overrides):
    """Construct a DreadWorld bound to a fake multiworld, with the given
    option overrides applied. Returns (world, multiworld)."""
    import typing

    from dread.Options import DreadOptions
    from dread.World import DreadWorld

    mw = _FakeMultiWorld()

    # Build a DreadOptions instance with defaults, then override.
    # ``from_any`` is a classmethod on each Option *subclass*, not on the
    # options dataclass — instantiate every field from its own default.
    hints = typing.get_type_hints(DreadOptions)
    opts = DreadOptions(**{name: cls.from_any(cls.default) for name, cls in hints.items()})
    for key, value in option_overrides.items():
        if isinstance(value, str):
            # Choice options (e.g. accessibility="minimal") need from_any to map
            # the key string to the right value; numeric ranges set .value direct.
            setattr(opts, key, hints[key].from_any(value))
        else:
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
def test_item_amounts_reflect_options_in_slot_data():
    """The per-pickup ammo/upgrade amount options must flow into the slot_data
    ``item_amounts`` map, which the client reads to grant the configured amount
    on the live wire path (multiworld deliveries)."""
    world, _ = _build_world(
        missile_tank_ammo=5,
        missile_plus_tank_ammo=25,
        power_bomb_tank_ammo=3,
        flash_shift_upgrade_amount=2,
        starting_power_bombs=2,
    )
    payload = world.fill_slot_data()
    assert payload["item_amounts"] == {
        "Power Bomb": 2,
        "Missile Tank": 5,
        "Missile+ Tank": 25,
        "Power Bomb Tank": 3,
        "Flash Shift Upgrade": 2,
        # The main Flash Shift bundles its included_ammo (default 2) — flows to
        # both the seed-baked and wire delivery paths. Speed Booster's main has
        # no included ammo, and Speed Booster Upgrade (a standard pickup) has no
        # amount option, so neither appears here.
        "Flash Shift": 2,
    }


@pytestmark_runtime
def test_item_amounts_default_to_randovania_values():
    """A default seed reproduces the starter-preset ammo amounts."""
    world, _ = _build_world()
    payload = world.fill_slot_data()
    assert payload["item_amounts"] == {
        "Power Bomb": 2,
        "Missile Tank": 2,
        "Missile+ Tank": 10,
        "Power Bomb Tank": 1,
        "Flash Shift Upgrade": 1,
        # main Flash Shift bundles included_ammo=2 (vanilla); Speed Booster main
        # has no included ammo, and Speed Booster Upgrade has no amount option.
        "Flash Shift": 2,
    }


@pytestmark_runtime
def test_trick_levels_in_slot_data():
    """Per-trick effective levels must ride slot_data so external trackers (e.g.
    PopTracker) can pre-populate their difficulty dials to match the seed."""
    from dread.Tricks import DREAD_TRICKS, effective_trick_levels

    # Global baseline Intermediate (2), with IBJ pinned to Mastery (5).
    world, _ = _build_world(trick_level=2, trick_infinite_bomb_jump=5)
    payload = world.fill_slot_data()
    levels = payload["trick_levels"]

    # Every Randovania trick is present, keyed by its short_name.
    assert set(levels) == {t.short_name for t in DREAD_TRICKS}
    assert levels == effective_trick_levels(world.options)
    # The per-trick override wins; a follow-global trick takes the baseline; the
    # hidden Suitless trick always follows global.
    assert levels["IBJ"] == 5
    assert levels["Knowledge"] == 2
    assert levels["Suitless"] == 2


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
def test_missile_tank_classification():
    """The PRECOLLECTED Missile Tank must be advancement; the findable copies
    need not be.

    Every compiled atom on Missile Tank is amount=1, so the single precollected
    copy (BASE_STARTING_ITEMS) satisfies them all from turn 0 — and it IS
    advancement because create_item reads items.json's classification, not the
    MIXED cap. (Regression guard for the original bug: a *useful* precollected
    copy never enters prog_items, so state.has("Missile Tank") would be
    permanently False, making ~36 locations unreachable.)

    The 60 findable copies, by contrast, carry no logic weight: the binding
    missile-capacity `sum` gate is only 17 (= 15 base + the precollected copy's
    2); every higher threshold is OR'd with a weapon alternative. Leaving all 60
    advancement starved fill_restrictive of swap space (fragile/slow
    generation). So only a small margin stays advancement; the rest are
    non-advancement (`useful` from the cap + `filler` from padding)."""
    from BaseClasses import ItemClassification
    world, mw = _build_world()
    world.create_items()

    # Precollected copy: advancement — the actual logic requirement.
    pre = [it for it in mw.precollected_items if it.name == "Missile Tank"]
    assert pre, "Missile Tank must be precollected"
    assert all(it.advancement for it in pre), \
        "precollected Missile Tank must be advancement (else state.has is blind)"

    # Findable copies: a small advancement margin, the rest non-advancement.
    mt = [it for it in mw.itempool if it.name == "Missile Tank"]
    assert mt, "no findable Missile Tank copies"
    adv = sum(1 for it in mt if it.advancement)
    assert adv == 3, f"expected 3 advancement findable Missile Tanks, got {adv}"
    assert all(it.classification in (ItemClassification.useful,
                                     ItemClassification.filler)
               for it in mt if not it.advancement), \
        "non-margin Missile Tanks must be useful/filler (non-advancement)"


@pytestmark_runtime
def test_pulse_radar_default_is_precollected_not_findable():
    """Default (start_with_pulse_radar on): Pulse Radar is precollected and kept
    out of the findable pool — mirrors the Randovania starter preset."""
    world, mw = _build_world()
    world.create_items()
    precollected = {it.name for it in mw.precollected_items}
    in_pool = {it.name for it in mw.itempool}
    assert "Pulse Radar" in precollected
    assert "Pulse Radar" not in in_pool


@pytestmark_runtime
def test_pulse_radar_off_is_findable_and_useful():
    """start_with_pulse_radar off: Pulse Radar is NOT precollected, rejoins the
    findable pool, and is classified `useful` (it gates nothing — 0 rule atoms —
    so it must not consume a progression slot). Solvability is unchanged."""
    from BaseClasses import ItemClassification
    world, mw = _build_world(start_with_pulse_radar=False)
    world.create_items()
    precollected = {it.name for it in mw.precollected_items}
    radar = [it for it in mw.itempool if it.name == "Pulse Radar"]
    assert "Pulse Radar" not in precollected
    assert len(radar) == 1, f"expected 1 findable Pulse Radar, got {len(radar)}"
    assert radar[0].classification == ItemClassification.useful


@pytestmark_runtime
def test_missile_plus_tank_first_is_progression_rest_useful():
    """Missile+ Tank has 336 amount=1 logic refs but is NOT precollected — the
    FIRST copy is logic-gating, the rest are pure ammo. MIXED_CLASSIFICATION_
    FIRST_N["Missile+ Tank"]=1 enforces that split."""
    from BaseClasses import ItemClassification
    world, mw = _build_world()
    world.create_items()
    mpt = [it for it in mw.itempool if it.name == "Missile+ Tank"]
    progression_n = sum(
        1 for it in mpt if it.classification == ItemClassification.progression
    )
    useful_n = sum(
        1 for it in mpt if it.classification == ItemClassification.useful
    )
    assert progression_n == 1, (
        f"Missile+ Tank: expected exactly 1 progression copy, got {progression_n}"
    )
    assert useful_n == len(mpt) - 1, (
        f"Missile+ Tank: expected {len(mpt) - 1} useful copies, got {useful_n}"
    )


def test_upgrade_pool_count_is_zero(items):
    """Flash Shift Upgrade and Speed Booster Upgrade have pool_count=0 —
    Randovania doesn't shuffle them by default. The Flash Shift main bundles its
    vanilla chains via flash_shift_included_ammo (default 2); the Speed Booster
    main includes nothing. The *_upgrade_count options shuffle them in."""
    by_name = {it["name"]: it for it in items}
    assert by_name["Flash Shift Upgrade"]["pool_count"] == 0
    assert by_name["Speed Booster Upgrade"]["pool_count"] == 0


@pytestmark_runtime
@pytest.mark.parametrize("name", ["Flash Shift Upgrade", "Speed Booster Upgrade"])
def test_chain_upgrades_absent_from_default_pool(name):
    """Default seed shuffles zero chain upgrades — the main pickup carries the
    vanilla chains, so none are findable."""
    world, mw = _build_world()
    world.create_items()
    assert [it for it in mw.itempool if it.name == name] == []


@pytestmark_runtime
@pytest.mark.parametrize(
    "name, count_opt",
    [
        ("Flash Shift Upgrade", "flash_shift_upgrade_count"),
        ("Speed Booster Upgrade", "speed_booster_upgrade_count"),
    ],
)
def test_chain_upgrade_count_drives_pool(name, count_opt):
    """Raising the count option shuffles that many copies into the pool."""
    world, mw = _build_world(**{count_opt: 4})
    world.create_items()
    assert len([it for it in mw.itempool if it.name == name]) == 4


@pytestmark_runtime
def test_flash_shift_upgrade_progression_split_tracks_included_ammo():
    """The main Flash Shift bundles `included_ammo` (default 2), so only the
    first MAX_CHAIN_REQ - 2 = 1 shuffled copy is logic-relevant (`progression`);
    the rest are `useful`. Raising included_ammo to the cap (3) makes every
    shuffled copy useful."""
    from BaseClasses import ItemClassification
    name = "Flash Shift Upgrade"
    # vanilla included_ammo=2 → first 1 progression.
    world, mw = _build_world(flash_shift_upgrade_count=3)
    world.create_items()
    copies = [it for it in mw.itempool if it.name == name]
    assert len(copies) == 3
    progression_n = sum(
        1 for c in copies if c.classification == ItemClassification.progression
    )
    assert progression_n == 1, f"expected 1 progression copy, got {progression_n}"

    # included_ammo at the cap → every shuffled copy is useful.
    world, mw = _build_world(flash_shift_upgrade_count=3, flash_shift_included_ammo=3)
    world.create_items()
    copies = [it for it in mw.itempool if it.name == name]
    assert all(c.classification == ItemClassification.useful for c in copies)


@pytestmark_runtime
def test_speed_booster_upgrade_progression_split_has_no_main_credit():
    """The Speed Booster major includes nothing, so the first MAX_CHAIN_REQ = 3
    shuffled Speed Booster Upgrades are all logic-relevant (`progression`)."""
    from BaseClasses import ItemClassification
    world, mw = _build_world(speed_booster_upgrade_count=4)
    world.create_items()
    copies = [it for it in mw.itempool if it.name == "Speed Booster Upgrade"]
    assert len(copies) == 4
    progression_n = sum(
        1 for c in copies if c.classification == ItemClassification.progression
    )
    useful_n = sum(
        1 for c in copies if c.classification == ItemClassification.useful
    )
    assert progression_n == 3, f"expected 3 progression copies, got {progression_n}"
    assert useful_n == 1, f"expected 1 useful copy, got {useful_n}"


@pytestmark_runtime
def test_energy_tank_count_drives_pool():
    world, mw = _build_world(energy_tank_count=4)
    world.create_items()
    n = sum(1 for it in mw.itempool if it.name == "Energy Tank")
    assert n == 4


@pytestmark_runtime
def test_energy_is_progression_visible_to_logic():
    """Faithful v0.3 HP model: enough Energy Tank / Energy Part copies are
    advancement that AP's sweep can clear the worst no-suit damage gate. At the
    vanilla default (8 tanks, 16 parts, 100/tank) that's all 8 tanks and 11
    parts (99 + 800 + 25*11 = 1174 >= MAX_NO_SUIT_HP 1150). Without this the
    sweep sees 0 energy and every gate above 99 HP is unreachable."""
    from BaseClasses import ItemClassification
    from dread.World import _energy_progression_counts, MAX_NO_SUIT_HP, BASE_HP
    world, mw = _build_world()
    world.create_items()

    et_adv = sum(1 for it in mw.itempool
                 if it.name == "Energy Tank" and it.advancement)
    ep_adv = sum(1 for it in mw.itempool
                 if it.name == "Energy Part" and it.advancement)
    exp_t, exp_p = _energy_progression_counts(100, 8, 16)
    assert (et_adv, ep_adv) == (exp_t, exp_p) == (8, 11)
    # Provisioned budget actually covers the worst threshold.
    assert BASE_HP + 100 * et_adv + 25 * ep_adv >= MAX_NO_SUIT_HP
    # Surplus copies are non-advancement (pure capacity).
    assert all(it.classification in (ItemClassification.useful,
                                     ItemClassification.filler)
               for it in mw.itempool
               if it.name == "Energy Part" and not it.advancement)


@pytestmark_runtime
def test_energy_progression_scales_with_energy_per_tank():
    """Lowering energy_per_tank makes each tank/part worth less HP, so MORE
    copies must be progression to cover the same worst-case gate; raising it
    means fewer. At 50/tank the full default pool (8 tanks + 16 parts) is all
    advancement; at 200/tank only 6 tanks (no parts) are needed."""
    from dread.World import _energy_progression_counts
    assert _energy_progression_counts(50, 8, 16) == (8, 16)
    assert _energy_progression_counts(200, 8, 16) == (6, 0)

    world, mw = _build_world(energy_per_tank=200)
    world.create_items()
    et_adv = sum(1 for it in mw.itempool
                 if it.name == "Energy Tank" and it.advancement)
    ep_adv = sum(1 for it in mw.itempool
                 if it.name == "Energy Part" and it.advancement)
    assert (et_adv, ep_adv) == (6, 0)


@pytestmark_runtime
def test_energy_progression_caps_at_available_pool():
    """If even the full pool can't reach the worst gate (counts too low, or a
    very low energy_per_tank), every copy is progression and the unreachable
    route simply drops from logic — no crash, no over-allocation past the pool.
    With 2 tanks + 2 parts the function returns exactly (2, 2)."""
    from dread.World import _energy_progression_counts
    assert _energy_progression_counts(100, 2, 2) == (2, 2)
    assert _energy_progression_counts(1, 8, 16) == (8, 16)
    # Zeroed energy: nothing to classify, no error.
    assert _energy_progression_counts(100, 0, 0) == (0, 0)


@pytestmark_runtime
def test_low_energy_per_tank_still_generates():
    """At a low energy_per_tank the full energy pool can't reach the worst
    no-suit damage gate (1150 HP), but those high gates are always OR'd with a
    non-damage alternative (Combat/Knowledge trick or Power Bomb route), so
    create_items does NOT raise — fill resolves reachability. We deliberately
    don't pre-judge solvability with a budget guard (it would reject solvable
    seeds). Just assert the pool builds and energy is all-progression here."""
    world, mw = _build_world(energy_per_tank=50)  # default accessibility = full
    world.create_items()  # must NOT raise
    et_adv = sum(1 for it in mw.itempool
                 if it.name == "Energy Tank" and it.advancement)
    ep_adv = sum(1 for it in mw.itempool
                 if it.name == "Energy Part" and it.advancement)
    # 50/tank: full default pool is all advancement (still under the worst gate).
    assert (et_adv, ep_adv) == (8, 16)


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
def test_max_tank_counts_fit_after_trim():
    """Every tank dialed to its max no longer overflows: now that Missile Tanks
    are mostly non-advancement, _balance_pool_to_locations can trim the junk
    down to the location count instead of raising. (Before the Missile-Tank
    reclassification the 60+ advancement copies were untrimmable and this raised
    OptionError.) The requested counts sum to ~244 tanks, far more than the ~141
    locations, so a fit proves trimming worked."""
    world, mw = _build_world(
        energy_tank_count=20,
        energy_part_count=64,
        missile_tank_count=120,
        missile_plus_tank_count=20,
        power_bomb_tank_count=20,
    )
    world.create_items()  # must NOT raise
    assert len(mw.itempool) <= 160, (
        f"pool should be trimmed to ~location count, got {len(mw.itempool)}"
    )


# ---- progressive items ----------------------------------------------------

@pytestmark_runtime
def test_progressive_off_default_keeps_individual_tiers():
    """Defaults (all progressive toggles off): the individual tier items are in
    the pool and no Progressive X item is."""
    world, mw = _build_world()
    world.create_items()
    names = {it.name for it in mw.itempool}
    for tier in ("Wide Beam", "Plasma Beam", "Wave Beam",
                 "Varia Suit", "Gravity Suit", "Bomb", "Cross Bomb"):
        assert tier in names, f"{tier} should be a findable item by default"
    for prog in ("Progressive Beam", "Progressive Suit", "Progressive Bomb"):
        assert prog not in names, f"{prog} should be absent by default"


@pytestmark_runtime
def test_progressive_beam_on_swaps_pool_size_neutral():
    """progressive_beam on: the 3 beam tiers leave the pool, 3 Progressive Beam
    copies enter; total pool size unchanged."""
    base, base_mw = _build_world()
    base.create_items()
    base_len = len(base_mw.itempool)

    world, mw = _build_world(progressive_beam=True)
    world.create_items()
    names = [it.name for it in mw.itempool]
    for tier in ("Wide Beam", "Plasma Beam", "Wave Beam"):
        assert tier not in names, f"{tier} should be folded into Progressive Beam"
    assert names.count("Progressive Beam") == 3
    assert all(it.advancement for it in mw.itempool
               if it.name == "Progressive Beam")
    assert len(mw.itempool) == base_len  # 3 out, 3 in


@pytestmark_runtime
def test_all_progressives_on_pool_length_invariant():
    """Enabling every group is pool-size neutral and removes every tier item."""
    base, base_mw = _build_world()
    base.create_items()
    base_len = len(base_mw.itempool)

    world, mw = _build_world(
        progressive_suit=True, progressive_spin=True,
        progressive_charge_beam=True, progressive_beam=True,
        progressive_missile=True, progressive_bomb=True,
    )
    world.create_items()
    assert len(mw.itempool) == base_len
    names = {it.name for it in mw.itempool}
    for tier in ("Varia Suit", "Gravity Suit", "Spin Boost", "Space Jump",
                 "Charge Beam", "Diffusion Beam", "Wide Beam", "Plasma Beam",
                 "Wave Beam", "Super Missile", "Ice Missile", "Bomb",
                 "Cross Bomb"):
        assert tier not in names
    for prog in ("Progressive Suit", "Progressive Spin",
                 "Progressive Charge Beam", "Progressive Beam",
                 "Progressive Missile", "Progressive Bomb"):
        assert prog in names


class _MiniState:
    """Faithful slice of AP's CollectionState: just the prog_items Counter and
    the add_item/remove_item/has/count methods World.collect/remove and our
    progressive override touch (copied from BaseClasses.CollectionState)."""

    def __init__(self):
        import collections
        self.prog_items = collections.defaultdict(collections.Counter)

    def add_item(self, item, player, count=1):
        self.prog_items[player][item] += count

    def remove_item(self, item, player, count=1):
        self.prog_items[player][item] -= count
        if self.prog_items[player][item] < 1:
            del self.prog_items[player][item]

    def has(self, item, player, count=1):
        return self.prog_items[player][item] >= count

    def count(self, item, player):
        return self.prog_items[player][item]


@pytestmark_runtime
def test_progressive_collect_remove_round_trips():
    """The k-th Progressive Beam credits the k-th tier (Wide, then Plasma, then
    Wave) so the compiled rules — which reference the tier names — see the right
    atoms; remove is the exact inverse so collect/remove round-trip during fill."""
    world, _ = _build_world(progressive_beam=True)
    beam = world.create_item("Progressive Beam")
    assert beam.advancement
    p = world.player
    state = _MiniState()

    order = ["Wide Beam", "Plasma Beam", "Wave Beam"]
    for k, tier in enumerate(order, start=1):
        assert world.collect(state, beam) is True
        assert state.count("Progressive Beam", p) == k
        for owned in order[:k]:
            assert state.has(owned, p)
        for missing in order[k:]:
            assert not state.has(missing, p)

    # A 4th copy (e.g. from start_inventory) must not over-grant past the top.
    world.collect(state, beam)
    assert state.count("Wave Beam", p) == 1

    # Remove the overflow, then each tier drops in reverse order.
    world.remove(state, beam)
    for tier in reversed(order):
        world.remove(state, beam)
        assert not state.has(tier, p)


@pytestmark_runtime
def test_main_flash_shift_credits_included_ammo_in_logic():
    """Collecting the main Flash Shift credits its `included_ammo` (default 2)
    onto Flash Shift Upgrade in state, so state.has("Flash Shift Upgrade", 2)
    clears with no upgrades shuffled. remove is the exact inverse so
    collect/remove round-trip during fill."""
    world, _ = _build_world()  # default: included_ammo == 2
    p = world.player
    main = world.create_item("Flash Shift")
    state = _MiniState()

    # No credit before the main is collected.
    assert not state.has("Flash Shift Upgrade", p, 1)
    assert world.collect(state, main) is True
    assert state.count("Flash Shift Upgrade", p) == 2
    assert state.has("Flash Shift Upgrade", p, 2)

    # Exact inverse on remove.
    world.remove(state, main)
    assert state.count("Flash Shift Upgrade", p) == 0

    # A different included_ammo credits that many.
    world, _ = _build_world(flash_shift_included_ammo=1)
    main = world.create_item("Flash Shift")
    state = _MiniState()
    world.collect(state, main)
    assert state.count("Flash Shift Upgrade", world.player) == 1


@pytestmark_runtime
def test_main_speed_booster_credits_nothing_in_logic():
    """The Speed Booster major includes no charge upgrades, so collecting the
    main Speed Booster must NOT credit any Speed Booster Upgrade in state."""
    world, _ = _build_world()
    main = world.create_item("Speed Booster")
    state = _MiniState()
    world.collect(state, main)
    assert state.count("Speed Booster Upgrade", world.player) == 0
