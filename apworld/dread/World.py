"""DreadWorld — the AP World subclass.

Registers items / locations / regions, creates one ``DreadItem`` per
item-pool entry, and writes a small slot_data so the client can derive
its mapping at connect time.

Logic uses the native AP region-graph (``graph_logic.py`` +
``data/logic_graph.json``): one Region per trivial-SCC of Randovania
nodes, events as pure-logic regions, per-seed dock/transport assignment.
See ``graph_logic.py`` and ``scripts/extract_dread_rules.py``.

Progressive items (Randovania's Progressive Suit/Spin/Charge Beam/Beam/Missile/
Bomb) are exposed as six opt-out-by-default toggles. When a group is enabled its
tier items leave the findable pool and one "Progressive X" item (one copy per
tier) takes their place; collect/remove credit the k-th copy onto the k-th tier
in state so ``state.has("Wave Beam")`` etc. still works.
Delivery sends the full multi-stage progression with the first tier's class and
the game grants the next missing tier (see client/protocol.py + Options.py).

Skipped for now (later phases):
  * Hint distribution; door/elevator randomization.
  * Ammo / damage / E-tank counting (v0.3).
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, ClassVar

import settings as ap_settings
from BaseClasses import Item, ItemClassification, Region, Tutorial
from worlds.AutoWorld import World, WebWorld

from .Items import (
    CLASSIFICATION_MAP, DreadItem, DreadItemData, item_table, item_name_to_id,
    item_name_to_item, get_item_classification,
    PROGRESSIVE_GROUPS, PROGRESSIVE_TIERS, PROGRESSIVE_MAP_ICON,
)
from .Locations import (
    DreadLocation, location_name_to_id, location_table,
    location_name_to_location,
)
from .Options import DreadOptions


GAME_NAME = "Metroid Dread"

# Highest chain-upgrade amount the access logic ever asks for: Randovania's
# FlashUpgrade / SpeedBoostUpgrade requirements top out at 3 (verified against
# the upstream logic snapshot). When the main pickup grants at least this many
# "from main" (the default), every chain gate clears with no upgrades shuffled
# and the upgrade items are pure QoL. See create_items (chain_progression_n)
# and collect/remove (the in-logic credit).
MAX_CHAIN_REQ = 3

# Faithful v0.3 HP damage model. The native logic graph emits no-suit
# ``damage_threshold`` atoms with real ``hp_needed`` values (Randovania's
# pre-resolved damage amounts on a route). ``Rules.compile_to_lambda`` checks
# them against an energy budget of ``(energy_per_tank - 1) + energy_per_tank·
# EnergyTank + (energy_per_tank/4)·EnergyPart`` (the pre-tank base scales with
# energy_per_tank — the game and Randovania both start Samus at one less than a
# full tank; at the vanilla 100/tank this base is 99). For AP's advancement-only
# beatability sweep to ever satisfy those gates, the Energy Tanks and Energy
# Parts it counts must be ``progression`` — otherwise ``state.count`` sees 0 and
# every threshold above the base is permanently unreachable (the v0.2 bug this
# replaces). We classify
# exactly enough copies progression to cover the worst binding threshold at the
# slot's ``energy_per_tank``; the rest are ``useful`` (pure HP capacity).
#
# MAX_NO_SUIT_HP is the largest no-suit ``hp_needed`` in logic_graph.json
# (regenerate the graph and re-run scripts/tests/test_extract_dread_rules.py
# ::test_max_no_suit_threshold if upstream logic changes it). Tanks are credited
# before parts (a tank is worth 4 parts), so the minimal sound progression count
# is: all tanks, then enough parts to close the gap.
MAX_NO_SUIT_HP = 1150
BASE_HP = 99
PARTS_PER_TANK = 4


def _energy_progression_counts(
    energy_per_tank: int, tank_count: int, part_count: int,
) -> tuple[int, int]:
    """How many Energy Tanks / Energy Parts must be ``progression`` so the
    advancement sweep can clear the worst no-suit damage gate.

    Credits tanks first (each worth ``energy_per_tank`` HP), then parts (1/4 of
    a tank). Caps at the available pool counts — if even the full pool can't
    reach ``MAX_NO_SUIT_HP`` (e.g. the user zeroed the counts or set a very low
    ``energy_per_tank``), all copies are progression and the unreachable
    threshold simply drops that route out of logic (AP accessibility stays
    sound — see the soundness note in create_items)."""
    per_tank = max(1, int(energy_per_tank))
    per_part = per_tank / PARTS_PER_TANK
    # Base (pre-tank) HP scales with energy_per_tank (== energy_per_tank - 1),
    # matching the game / Randovania and the damage predicate in Rules.py. At
    # the vanilla 100/tank this is BASE_HP (99).
    deficit = MAX_NO_SUIT_HP - (per_tank - 1)
    if deficit <= 0:
        return 0, 0
    tanks = min(tank_count, -(-deficit // per_tank))  # ceil div
    remaining = deficit - per_tank * tanks
    if remaining <= 0 or per_part <= 0:
        return tanks, 0
    import math
    parts = min(part_count, math.ceil(remaining / per_part))
    return tanks, parts


# Faithful v0.3 ammo model (missiles + power bombs). The native logic graph emits
# ``sum`` atoms for ammo capacity (missiles: base 15 + 2/Missile Tank +
# 10/Missile+ Tank; power bombs: base 0 + 2/Power Bomb launcher + 2/Power Bomb
# Tank) plus, separately, a plain ``Missile Tank>=1`` atom standing in for
# Randovania's MissileLauncher "can fire a missile at all" unlock. We empirically
# walked logic_graph.json (see the ammo retro): EVERY missile ``sum`` threshold
# above MAX_BINDING_MISSILE_CAP and every PB threshold above MAX_BINDING_PB_CAP is
# OR'd with a non-sum alternative that the advancement sweep can reach, so the
# only capacity actually on the critical path is these caps. (Re-derive with
# scripts/tests/test_extract_dread_rules.py::test_max_binding_ammo_cap +
# scripts/analyze_ammo_binding.py if upstream logic changes.)
#
# The launcher unlock (Missile Tank>=1) is already satisfied by the PRECOLLECTED
# Missile Tank in BASE_STARTING_ITEMS, so at the vanilla defaults (start 15
# missiles / 2 PB) NO findable tank is required at all — base capacity already
# meets the caps. We only need findable progression copies when the user lowers
# the starting ammo or per-tank yield enough that base+precollected falls short
# of a cap. This is STRICTLY FEWER advancement Missile Tanks than the old static
# cap of 3, so fill health improves.
MAX_BINDING_MISSILE_CAP = 15   # largest sole-binding missile capacity in logic
MAX_BINDING_PB_CAP = 2         # largest sole-binding power-bomb capacity in logic
PRECOLLECTED_MISSILE_TANKS = 1  # BASE_STARTING_ITEMS includes one Missile Tank


def _ammo_progression_counts(
    starting_missiles: int, missile_tank_ammo: int, missile_plus_tank_ammo: int,
    missile_tank_count: int, missile_plus_tank_count: int,
    starting_power_bombs: int, power_bomb_tank_ammo: int, power_bomb_tank_count: int,
) -> tuple[int, int, int]:
    """How many findable Missile Tank / Missile+ Tank / Power Bomb Tank copies
    must be ``progression`` so the advancement sweep can clear the worst
    sole-binding ammo capacity gate at this slot's ammo settings.

    Missiles: the precollected Missile Tank contributes
    ``PRECOLLECTED_MISSILE_TANKS * missile_tank_ammo`` on top of
    ``starting_missiles``; we credit findable Missile Tanks first (largest pool,
    cheapest swap headroom), then Missile+ Tanks, until base+credits reach
    ``MAX_BINDING_MISSILE_CAP``. Power bombs: the Power Bomb launcher (a findable
    progression item) grants ``starting_power_bombs``; we credit PB Tanks until
    base+credits reach ``MAX_BINDING_PB_CAP``.

    Caps at the available pool counts. If even the full pool can't reach a cap
    (e.g. zeroed starting ammo AND zero per-tank yield), all copies are
    progression and that capacity route drops out of logic — AP accessibility
    stays sound, exactly as for the energy model. The MissileLauncher unlock
    (Missile Tank>=1) is covered by the precollected copy, not by these counts."""
    import math
    # Missiles
    have_m = int(starting_missiles) + PRECOLLECTED_MISSILE_TANKS * int(missile_tank_ammo)
    need_m = MAX_BINDING_MISSILE_CAP - have_m
    mt = 0
    mpt = 0
    if need_m > 0 and int(missile_tank_ammo) > 0:
        mt = min(int(missile_tank_count), math.ceil(need_m / int(missile_tank_ammo)))
        need_m -= mt * int(missile_tank_ammo)
    if need_m > 0 and int(missile_plus_tank_ammo) > 0:
        mpt = min(int(missile_plus_tank_count),
                  math.ceil(need_m / int(missile_plus_tank_ammo)))
    # Power bombs
    need_pb = MAX_BINDING_PB_CAP - int(starting_power_bombs)
    pbt = 0
    if need_pb > 0 and int(power_bomb_tank_ammo) > 0:
        pbt = min(int(power_bomb_tank_count),
                  math.ceil(need_pb / int(power_bomb_tank_ammo)))
    return mt, mpt, pbt


# Number of in-game Nav Station hint plaques baked into the starter template.
# _generate_nav_hints fills up to this many with real AP placement hints;
# patcher_pipeline._apply_nav_hints maps them onto the template plaques and
# falls back to neutral filler for any shortfall.
NAV_HINT_COUNT = 11


class DreadSettings(ap_settings.Group):
    class DreadvaniaPath(ap_settings.OptionalUserFilePath):
        """Path to the Python interpreter that has open-dread-rando installed.

        Leave blank to let the client auto-detect it at startup.
        Example: C:\\Users\\you\\AppData\\Local\\Programs\\Python\\Python311\\python.exe
        """
        description = "dreadvania Python executable"
        is_exe = True

    dreadvania_python: DreadvaniaPath = DreadvaniaPath("")


class DreadWebWorld(WebWorld):
    theme = "ocean"
    tutorials = [
        Tutorial(
            "Multiworld Setup Guide",
            "A guide to setting up Metroid Dread for Archipelago multiworld.",
            "English",
            "setup_en.md",
            "setup/en",
            ["maxdietz"],
        )
    ]


class DreadWorld(World):
    """Metroid Dread (Switch, modded)."""

    game = GAME_NAME
    options_dataclass = DreadOptions
    options: DreadOptions

    settings_key = "dread_options"
    settings: ClassVar[DreadSettings]

    item_name_to_id = item_name_to_id
    location_name_to_id = location_name_to_id

    web = DreadWebWorld()

    required_client_version = (0, 5, 0)

    def create_item(self, name: str,
                    classification: ItemClassification | None = None) -> Item:
        if classification is None:
            classification = get_item_classification(name)
        return DreadItem(
            name,
            classification,
            item_name_to_id[name],
            self.player,
        )

    def create_filler(self) -> Item:
        """Junk fill. ``get_filler_item_name`` returns a real expansion (usually
        Missile Tank), whose items.json classification is advancement — but a
        FILLER copy must be non-advancement, or AP's fill (and our own pool
        padding) would silently inject extra advancement items, re-saturating
        the progression pool we deliberately cap (see
        MIXED_CLASSIFICATION_FIRST_N / test_missile_tank_classification). Force
        ``filler`` regardless of the item's default class."""
        return self.create_item(
            self.get_filler_item_name(), classification=ItemClassification.filler
        )

    def generate_early(self) -> None:
        # Resolve transports, spawn, and door assignment now, so
        # create_regions/create_items can use them. All ride the native graph.
        self._dock_assignments: dict[str, str] = {}
        self._transport_matching: dict[str, str] = {}
        self._start_comp: int | None = None           # None => graph default (Artaria)
        self._start_patcher: dict | None = None       # None => STARTING_AREA index 0
        self._start_extra_items: list[str] = []
        self._ut = False                              # Universal Tracker regen?

        # Universal Tracker re-runs generation from slot_data to recompute the
        # reachable set for a live game. Its single-player regen uses a DIFFERENT
        # RNG stream than the original multiworld, so re-rolling door/transport/
        # start randomization here would build a degenerate graph and collapse
        # reachability to a near-empty set. Instead, restore every per-seed
        # decision (and the logic-affecting options, in case UT has no player
        # YAML) from the `ut_state` payload fill_slot_data exported. See
        # _ut_state / interpret_slot_data.
        ut_state = self._ut_passthrough()
        if ut_state is not None:
            self._ut = True
            self._restore_ut_state(ut_state)
            return

        door_on = int(self.options.door_lock_rando.value) != 0
        transport_on = int(self.options.transport_rando.value) != 0
        area = int(self.options.starting_area.value)
        if not (door_on or transport_on or area != 0):
            return

        from .graph_logic import load_graph
        from .Tricks import effective_trick_levels
        from .StartArea import start_node_for, minimal_start_items
        graph = load_graph()
        tl = effective_trick_levels(self.options)
        base_items = {n: 1 for n in self.BASE_STARTING_ITEMS}

        # Transport matching FIRST — every reachability check below depends on it.
        if transport_on:
            from .TransportRando import roll_connected_matching
            self._transport_matching = roll_connected_matching(
                graph, self.random, tl, mode="randomized",
                door_lock_rando=door_on)
        tm = self._transport_matching

        # Spawn point + the minimal extra starting kit that bootstraps it.
        if area != 0:
            chosen = start_node_for(graph, area, tl, base_items, tm,
                                    door_lock_rando=door_on)
            if chosen is not None:
                _key, self._start_comp, self._start_patcher = chosen
                self._start_extra_items = minimal_start_items(
                    graph, self._start_comp, base_items, tl, transport_matching=tm,
                    door_lock_rando=door_on)
                for n in self._start_extra_items:
                    base_items[n] = 1

        # Door-lock weakness assignment, guarded so the early frontier (from THIS
        # spawn, with the full starting kit) stays vanilla and fill can bootstrap.
        if door_on:
            from .DoorRando import roll_assignments
            from .graph_logic import ammo_amounts_from_options
            from .Options import DoorLockRando
            door_mode = ("types"
                         if int(self.options.door_lock_rando.value)
                         == DoorLockRando.option_door_types
                         else "individual")
            self._dock_assignments = roll_assignments(
                graph, self.random, mode=door_mode,
                starting_items=base_items, trick_levels=tl,
                start_comp=self._start_comp, transport_matching=tm,
                energy_per_tank=int(self.options.energy_per_tank.value),
                ammo_amounts=ammo_amounts_from_options(self.options),
                doors_to_change=set(self.options.doors_to_change.value),
                change_doors_to=set(self.options.change_doors_to.value))

    def _compute_dropped_locations(self) -> set[str]:
        """AP-names of pickup locations that are unreachable even with a FULL
        loadout under this config — the set we DROP under ``accessibility:
        full``/``items`` so generation succeeds instead of failing.

        Randovania's starter preset disables every trick and only guarantees the
        seed is BEATABLE — its trick-gated pickups (e.g. the Speed-Booster-
        Conservation speedboost rooms) simply hold filler. That is exactly AP's
        ``accessibility: minimal``, under which fill places only filler in the
        unreachable spots and we drop nothing. ``full`` (and its ``items`` alias)
        instead require every location reachable, which an all-tricks-off config
        genuinely cannot satisfy. Rather than raise a late ``FillError`` (or the
        earlier up-front ``OptionError``), we simply don't create those
        locations: ``build_regions`` skips them and ``create_items`` trims a
        matching number of non-advancement copies, so the pool stays balanced.
        The physical in-game spot is untouched — the patcher merges per-location
        overrides onto the full starter-preset template, so an uncreated location
        keeps whatever item the template baked there (a real, collectable-but-
        untracked pickup). Deterministic in (options, rolled graph state), so a
        Universal Tracker re-gen reproduces the same drop set.

        Returns the empty set under ``minimal`` (unreachable spots stay as
        tracked filler, the Randovania-faithful behavior) and whenever the config
        leaves nothing stranded (the default Beginner config)."""
        acc = self.options.accessibility
        if acc.value == acc.option_minimal:
            return set()

        from .graph_logic import (
            load_graph, ammo_amounts_from_options, unreachable_pickup_locations,
        )
        from .Tricks import effective_trick_levels, DREAD_TRICKS

        unreachable, gating = unreachable_pickup_locations(
            load_graph(), effective_trick_levels(self.options),
            start_comp=getattr(self, "_start_comp", None),
            transport_matching=getattr(self, "_transport_matching", None),
            dock_assignments=getattr(self, "_dock_assignments", None),
            energy_per_tank=int(self.options.energy_per_tank.value),
            ammo_amounts=ammo_amounts_from_options(self.options),
            door_lock_rando=int(self.options.door_lock_rando.value) != 0)
        if not unreachable:
            return set()

        import logging
        long_name = {t.short_name: t.long_name for t in DREAD_TRICKS}
        tricks = sorted(long_name.get(s, s) for s in gating)
        shown = ", ".join(unreachable[:6]) + (
            f", ... (+{len(unreachable) - 6} more)" if len(unreachable) > 6 else "")
        logging.getLogger(__name__).info(
            "Metroid Dread [%s]: dropping %d location(s) unreachable under this "
            "configuration even with a full loadout, so 'accessibility: %s' can "
            "be satisfied: %s. These are gated by trick(s) you have disabled: %s. "
            "Their in-game pickups keep the starter-preset item (collectable but "
            "not AP-tracked); enable the gating trick(s) or use 'accessibility: "
            "minimal' to keep them as tracked filler instead.",
            self.player_name, len(unreachable), acc.current_key, shown,
            ", ".join(tricks) or "(see logic)")
        return set(unreachable)

    def create_regions(self) -> None:
        # Events are graph regions; AP's BFS must re-derive event-gated
        # entrances when an event region becomes reachable.
        type(self).explicit_indirect_conditions = True
        from .graph_logic import build_regions
        # Locations unreachable-by-config under full/items are dropped (not
        # created) rather than failing generation — see _compute_dropped_locations.
        self._dropped_locations = self._compute_dropped_locations()
        build_regions(self, getattr(self, "_dock_assignments", None),
                      getattr(self, "_transport_matching", None),
                      dropped=self._dropped_locations)

    def create_items(self) -> None:
        # Pool layout (post-M2 + Dreadvania options):
        #   - Each tank/expansion item is added pool_count times, where
        #     pool_count comes from a per-item option (default = Randovania
        #     starter preset count).
        #   - Each unique progression item is added once (pool_count=1 from
        #     items.json).
        #   - Each event item is skipped — events are inlined into compiled
        #     rules; they remain in items.json for AP-ID stability only.
        #   - Metroid DNA: exactly N copies (RequiredArtifacts option).
        #   - Then _balance_pool_to_locations pads/trims to the slot count.
        from Options import OptionError

        o = self.options
        # Sanity guards (faithful v0.3 ammo model): a config that can never reach
        # the binding ammo capacity makes the ammo-gated locations unreachable.
        # AP's fill would still raise a (cryptic) FillError; we raise a clearer
        # OptionError up front pointing at the knobs to fix.
        #
        # Power bombs: PB gates need >= MAX_BINDING_PB_CAP. The only PB capacity
        # sources are the launcher's starting_power_bombs and PB Tanks; if neither
        # can contribute, the gates are unreachable.
        if (int(o.starting_power_bombs.value) < MAX_BINDING_PB_CAP
                and (int(o.power_bomb_tank_count.value) == 0
                     or int(o.power_bomb_tank_ammo.value) == 0)):
            raise OptionError(
                "Power Bomb capacity can never reach the in-logic requirement "
                f"({MAX_BINDING_PB_CAP}): starting_power_bombs="
                f"{int(o.starting_power_bombs.value)} and no PB Tank capacity "
                "(power_bomb_tank_count or power_bomb_tank_ammo is 0). Raise "
                "starting_power_bombs, or give Power Bomb Tanks a nonzero count "
                "and ammo."
            )
        # Missiles: missile gates need >= MAX_BINDING_MISSILE_CAP. Sources are
        # starting_missiles + the precollected Missile Tank + findable tanks.
        max_missiles = (
            int(o.starting_missiles.value)
            + (PRECOLLECTED_MISSILE_TANKS + int(o.missile_tank_count.value))
            * int(o.missile_tank_ammo.value)
            + int(o.missile_plus_tank_count.value)
            * int(o.missile_plus_tank_ammo.value)
        )
        if max_missiles < MAX_BINDING_MISSILE_CAP:
            raise OptionError(
                "Missile capacity can never reach the in-logic requirement "
                f"({MAX_BINDING_MISSILE_CAP}): the most missiles obtainable is "
                f"{max_missiles} (starting_missiles + all tanks at their ammo "
                "values). Raise starting_missiles or missile_tank_ammo / "
                "missile_plus_tank_ammo."
            )
        # Zero starting missile capacity (starting_missiles=0 AND
        # missile_tank_ammo=0, so the precollected Missile Tank also grants 0)
        # forces ALL missile capacity through the 12 Missile+ Tanks — but the
        # earliest missile gates then require a *found* Missile+ Tank that itself
        # sits behind missile gates. That self-gate is unsolvable for fill at any
        # accessibility (verified across seeds). This mirrors the known-
        # unsupported energy_tank_count=0 extreme (CLAUDE.md v0.3). Any nonzero
        # starting_missiles OR missile_tank_ammo bootstraps the early sphere and
        # is fine.
        if (int(o.starting_missiles.value) == 0
                and int(o.missile_tank_ammo.value) == 0):
            raise OptionError(
                "starting_missiles=0 with missile_tank_ammo=0 leaves no early "
                "missile capacity (the abundant Missile Tank pool grants 0), so "
                "the missile-gated start sphere can't bootstrap. Give "
                "starting_missiles or missile_tank_ammo a nonzero value."
            )

        # Option-driven counts override items.json pool_count for tanks.
        # Flash Shift / Speed Booster Upgrade default to 0 (Randovania doesn't
        # shuffle them). The Flash Shift main bundles its vanilla chains via
        # flash_shift_included_ammo (see collect below); the Speed Booster main
        # includes nothing.
        pool_overrides = {
            "Energy Tank":     int(o.energy_tank_count.value),
            "Energy Part":     int(o.energy_part_count.value),
            "Missile Tank":    int(o.missile_tank_count.value),
            "Missile+ Tank":   int(o.missile_plus_tank_count.value),
            "Power Bomb Tank": int(o.power_bomb_tank_count.value),
            "Flash Shift Upgrade":   int(o.flash_shift_upgrade_count.value),
            "Speed Booster Upgrade": int(o.speed_booster_upgrade_count.value),
        }

        # Chain-upgrade progression classification (dynamic, unlike the static
        # MIXED_CLASSIFICATION_FIRST_N below). The access logic gates some routes
        # on Flash Shift / Speed Booster upgrades up to MAX_CHAIN_REQ; the main
        # Flash Shift credits its `included_ammo` in logic (see collect), so only
        # the first `MAX_CHAIN_REQ - included` *shuffled* Flash Shift Upgrades are
        # logic-relevant → progression (1 at the vanilla default included=2). The
        # Speed Booster major includes nothing, so all of its first MAX_CHAIN_REQ
        # shuffled copies are logic-relevant. Copies past these are pure QoL
        # (`useful`). If the main plus shuffled copies still can't reach a route's
        # requirement, that route drops out of logic (AP accessibility stays
        # sound).
        chain_progression_n = {
            "Flash Shift Upgrade": max(
                0, MAX_CHAIN_REQ - int(o.flash_shift_included_ammo.value)),
            "Speed Booster Upgrade": MAX_CHAIN_REQ,
        }

        # Faithful HP model (v0.3): make enough Energy Tank / Energy Part copies
        # progression that AP's advancement sweep can clear the worst no-suit
        # damage_threshold at this slot's energy_per_tank. See
        # _energy_progression_counts + MAX_NO_SUIT_HP. The remaining copies are
        # useful (pure HP capacity, placeable anywhere). This is what makes the
        # real thresholds in logic_graph.json SOUND under fill: without it
        # state.count(Energy *) is 0 for the sweep and every gate above base HP
        # is unreachable.
        ept = int(o.energy_per_tank.value)
        etank_prog, epart_prog = _energy_progression_counts(
            ept, int(o.energy_tank_count.value), int(o.energy_part_count.value),
        )
        chain_progression_n["Energy Tank"] = etank_prog
        chain_progression_n["Energy Part"] = epart_prog

        # Faithful ammo model (v0.3): make enough Missile / Missile+ / Power Bomb
        # Tank copies progression that AP's advancement sweep can clear the worst
        # sole-binding ammo capacity gate at this slot's ammo settings. See
        # _ammo_progression_counts + MAX_BINDING_MISSILE_CAP / MAX_BINDING_PB_CAP.
        # At vanilla defaults this is (0, 0, 0) — the precollected Missile Tank
        # plus starting capacity already meet the binding caps, so no findable
        # tank is required (strictly fewer advancement copies than the old static
        # cap of 3/1/1, which improves fill health). It only grows when the user
        # lowers starting ammo / per-tank yield.
        mt_prog, mpt_prog, pbt_prog = _ammo_progression_counts(
            int(o.starting_missiles.value), int(o.missile_tank_ammo.value),
            int(o.missile_plus_tank_ammo.value),
            int(o.missile_tank_count.value), int(o.missile_plus_tank_count.value),
            int(o.starting_power_bombs.value), int(o.power_bomb_tank_ammo.value),
            int(o.power_bomb_tank_count.value),
        )
        chain_progression_n["Missile Tank"] = mt_prog
        chain_progression_n["Missile+ Tank"] = mpt_prog
        chain_progression_n["Power Bomb Tank"] = pbt_prog
        # NOTE: we deliberately do NOT raise when the full energy pool can't reach
        # the worst no-suit threshold (e.g. very low energy_per_tank, or a small
        # tank count). Those high-HP gates are ALWAYS inside an OR with a
        # non-damage alternative (a Combat/Knowledge trick branch and/or a Power
        # Bomb route — verified against logic_graph.json), so the energy budget is
        # rarely the sole binding factor. Whether a given config is reachable at
        # `full` accessibility is exactly what AP's fill/beatability sweep
        # decides; a pre-emptive budget guard would reject solvable seeds (4
        # tanks, default trick level, is solvable). If a config truly can't reach
        # everything, AP raises its own FillError — the faithful, accurate signal.

        # For items where the pool has more copies than compiled rules need
        # at amount=1, only the FIRST N copies get the row's classification —
        # the rest fall back to "useful" (logic-irrelevant but still placed in
        # reachable spots where possible).
        #   - Flash Shift Upgrade / Speed Booster Upgrade: default to 0 in the
        #     pool (the chains are baked into the main pickup); when shuffled
        #     their progression count is dynamic — see chain_progression_n.
        #   - Missile Tank / Missile+ Tank / Power Bomb Tank: dynamic ammo model
        #     (chain_progression_n above). The MissileLauncher unlock (a plain
        #     Missile Tank>=1 atom) is satisfied by the PRECOLLECTED Missile Tank
        #     (BASE_STARTING_ITEMS), which is advancement because create_item
        #     reads items.json's class, NOT this cap. The `sum` capacity gates
        #     bind only up to MAX_BINDING_MISSILE_CAP / MAX_BINDING_PB_CAP; every
        #     higher threshold is OR'd with a reachable alternative, so at vanilla
        #     ammo settings _ammo_progression_counts returns (0,0,0) and the rest
        #     are `useful` (pure capacity, placeable anywhere). Leaving all 60
        #     findable Missile Tanks advancement would starve AP's fill_restrictive
        #     of swap space. (create_filler() force-classes its junk `filler` for
        #     the same reason — get_filler_item_name returns Missile Tank.) See
        #     test_missile_tank_classification.
        # Energy Tank / Energy Part (HP model) AND Missile Tank / Missile+ Tank /
        # Power Bomb Tank (ammo model) progression counts are all dynamic (they
        # depend on energy_per_tank / starting + per-tank ammo) and live in
        # chain_progression_n above. None are listed here — this static table is
        # now only for items whose binding is option-independent.
        MIXED_CLASSIFICATION_FIRST_N: dict[str, int] = {}

        # Dropped locations (unreachable-by-config under full/items) are not
        # created, so the pool target shrinks by that many — _balance_pool_to_
        # locations then trims a matching number of non-advancement copies
        # (filler / useful Missile Tanks etc.), keeping the pool balanced.
        non_event_locations = sum(
            1 for l in location_table if l.pickup_type != "event"
        ) - len(getattr(self, "_dropped_locations", ()))

        # Forced starting items: precollect into AP logic so state.has() is true
        # from turn 0 (the compiled rules reference them; without this the
        # opening rooms and everything past them are unreachable). See the
        # class-attr docstrings for why the bottleneck set is needed.
        #
        # Pulse Radar is the one starter that gates NOTHING in our logic (0 rule
        # atoms — Randovania never requires it for traversal), so it's an opt-out
        # selector: when start_with_pulse_radar is off we don't precollect it and
        # it rejoins the findable pool. Solvability is identical either way.
        start_with_radar = bool(o.start_with_pulse_radar.value)
        # Per-spawn bootstrap kit (more-starting-areas): minimal extra unlocks so
        # a deep spawn has an early sphere for fill. Empty for the Artaria spawn.
        start_area_items = list(getattr(self, "_start_extra_items", []) or [])
        forced_starting = (list(self.BASE_STARTING_ITEMS)
                           + list(self.EXTRA_STARTING_ITEMS)
                           + start_area_items)
        if not start_with_radar:
            forced_starting = [n for n in forced_starting if n != "Pulse Radar"]
        for name in forced_starting:
            self.multiworld.push_precollected(self.create_item(name))
        # Starting-only items are removed from the findable pool. Missile Tank
        # is precollected for capacity but stays findable. Pulse Radar is only
        # excluded when it's a starting item.
        pool_excluded = ({"Slide"} | set(self.EXTRA_STARTING_ITEMS)
                         | set(start_area_items))
        if start_with_radar:
            pool_excluded.add("Pulse Radar")

        # Progressive item groups (mirror Randovania). Each enabled group's
        # single "Progressive X" item (pool_count == tier count) replaces its
        # individual tier items in the findable pool — pool-size neutral. None
        # of the forced starters (Slide / Pulse Radar / Missile Tank) overlap a
        # progressive tier, so the swap is independent of the starting set.
        enabled_progressives = {
            prog_name for opt_attr, prog_name in PROGRESSIVE_GROUPS.items()
            if bool(getattr(o, opt_attr).value)
        }
        replaced_components: set[str] = set()
        for prog_name in enabled_progressives:
            replaced_components.update(PROGRESSIVE_TIERS[prog_name])

        pool: list[Item] = []
        for it in item_table:
            if it.name.startswith("Event: "):
                continue
            if it.name.startswith("Metroid DNA"):
                continue
            if it.name in pool_excluded:
                continue
            # Progressive items live in the table for AP-ID stability but are
            # only shuffled when their group's toggle is on.
            if it.progression_tiers and it.name not in enabled_progressives:
                continue
            # When a group is on, its tier items are folded into the progressive
            # item — drop them from the findable pool.
            if it.name in replaced_components:
                continue
            count = pool_overrides.get(it.name, it.pool_count)
            default_cls = CLASSIFICATION_MAP.get(
                it.classification, ItemClassification.filler,
            )
            # Pulse Radar only reaches the pool when start_with_pulse_radar is
            # off. It gates nothing (0 rule atoms), so as a findable item it's
            # QoL, not progression — don't let it consume a progression slot.
            if it.name == "Pulse Radar":
                default_cls = ItemClassification.useful
            # If this item has a "first N progression" override, the rest of
            # the copies fall back to useful (e.g. Missile+ Tank). The chain
            # upgrades use the dynamic count (depends on the "from main"
            # option); everything else uses the static table, and items absent
            # from both get n_special == count → every copy uses the row's
            # classification (the legacy behavior).
            if it.name in chain_progression_n:
                n_special = chain_progression_n[it.name]
            else:
                n_special = MIXED_CLASSIFICATION_FIRST_N.get(it.name, count)
            for i in range(count):
                cls = default_cls if i < n_special else ItemClassification.useful
                pool.append(self.create_item(it.name, classification=cls))

        # Metroid DNA: exactly the first N (mapping to artifacts 1..N).
        n_dna = int(o.required_artifacts.value)
        for k in range(1, n_dna + 1):
            pool.append(self.create_item(f"Metroid DNA {k}"))

        self._balance_pool_to_locations(pool, non_event_locations)
        self.multiworld.itempool += pool

    def _balance_pool_to_locations(self, pool: list[Item], target: int) -> None:
        """Pad short pools with filler; trim overflows in a defined preference
        order. Raise OptionError if even after trimming we exceed target —
        with guidance pointing at the user-facing knobs to lower.

        Only NON-advancement copies are eligible for trimming. Removing an
        advancement copy would silently break logic: the first few Energy Tank /
        Power Bomb Tank / Missile+ Tank / Missile Tank copies are progression
        (and the precollected Missile Tank gates ~every location). Trimming
        those to "make it fit" would produce an unsolvable seed with no fill
        error (the compiled rules collapse ammo to >=1, so AP's fill can't catch
        it). So we trim the slack (filler + the useful overflow copies, which
        now include most Missile Tanks) and, if that isn't enough, raise rather
        than eat a required item."""
        while len(pool) < target:
            pool.append(self.create_filler())
        if len(pool) <= target:
            return
        # Trim least-impactful items first — but never an advancement copy.
        trim_order = (
            "Energy Part", "Power Bomb Tank", "Missile Tank",
            "Energy Tank", "Missile+ Tank",
        )
        overflow = len(pool) - target
        for name in trim_order:
            if overflow == 0:
                break
            for i in range(len(pool) - 1, -1, -1):
                if overflow == 0:
                    break
                if pool[i].name == name and not pool[i].advancement:
                    pool.pop(i)
                    overflow -= 1
        if overflow > 0:
            from Options import OptionError
            raise OptionError(
                f"Dread item pool exceeds {target} available locations even "
                "after trimming. Reduce energy_tank_count / energy_part_count "
                "/ missile_tank_count / missile_plus_tank_count / "
                "power_bomb_tank_count."
            )

    def set_rules(self) -> None:
        from .graph_logic import set_graph_rules
        set_graph_rules(self)

    # ------------------------------------------------------------------
    # Universal Tracker logic explanation (see ut_explain.py).
    #
    # UT probes the regenerated world for these methods with ``hasattr`` and
    # uses them to render /explain and /get_logical_path. Without them UT falls
    # back to introspecting our opaque access-rule lambdas, printing internal
    # names (``R37 (True): e5 : True``). Here we render the symbolic rule ASTs
    # stashed by ``graph_logic.build_regions`` into human-readable message parts,
    # coloring each atom by evaluating it against the tracker's state.
    # ------------------------------------------------------------------
    def _explain_ctx(self):
        from .ut_explain import ExplainCtx
        from .Tricks import effective_trick_levels
        from .graph_logic import ammo_amounts_from_options
        labels = getattr(self, "_explain_labels", {})
        return ExplainCtx(
            player=self.player,
            trick_levels=effective_trick_levels(self.options),
            energy_per_tank=int(self.options.energy_per_tank.value),
            ammo_amounts=ammo_amounts_from_options(self.options),
            door_lock_rando=int(self.options.door_lock_rando.value) != 0,
            comp_label=lambda c: labels.get(c, f"R{c}"),
        )

    def _region_label(self, region) -> str:
        """Human name for one of our machine-named regions."""
        nm = getattr(region, "name", "?")
        if nm == "Menu":
            return "Start"
        if nm.startswith("Event:"):
            from .ut_explain import humanize_event
            return f"{humanize_event(nm[6:])} (event)"
        if nm.startswith("R"):
            try:
                comp = int(nm[1:])
            except ValueError:
                return nm
            return getattr(self, "_explain_labels", {}).get(comp, nm)
        return nm

    def _explain_entrance_parts(self, entrance, ctx, state) -> list:
        """Render one hop: ``<src> [reachable] -> <dst> : <requirement>``.

        The separator is ASCII ``->`` on purpose: the tracker's console font
        renders a U+2192 arrow as a tofu box."""
        from .ut_explain import _text, _colored, render_ast, _LABEL
        src = entrance.parent_region
        reach = bool(src.can_reach(state)) if (src and state is not None) else None
        mark = ("reachable" if reach else "blocked") if reach is not None else "?"
        parts = [
            _colored(self._region_label(src) if src else "?", _LABEL),
            _text(f" [{mark}] -> "),
            _colored(self._region_label(entrance.connected_region)
                     if entrance.connected_region else "?", _LABEL),
        ]
        ast = getattr(self, "_explain_asts", {}).get(entrance.name)
        if ast is not None:
            parts.append(_text(": "))
            parts.extend(render_ast(ast, ctx, state))
        return parts

    def explain_path(self, entrance, state):
        """UT hook: render ONE hop of the default /get_logical_path walk."""
        return self._explain_entrance_parts(entrance, self._explain_ctx(), state)

    def _resolve_explain_target(self, name: str):
        mw = self.multiworld
        p = self.player
        locs = {loc.name: loc for loc in mw.get_locations(p)}
        regs = {reg.name: reg for reg in mw.get_regions(p)}
        try:
            from Utils import get_intended_text
            result, usable, _ = get_intended_text(
                name, set(locs) | set(regs))
        except Exception:
            result, usable = name, (name in locs or name in regs)
        if not usable:
            return None
        if result in locs:
            return "location", locs[result]
        if result in regs:
            return "region", regs[result]
        return None

    def explain_rule(self, target_name, state):
        """UT hook: explain the logic gating a location/region for /explain.

        Returns None on an unresolved name so UT falls back to its default
        (which at least surfaces a name-matching error)."""
        from .ut_explain import _text, _colored, _GREEN, _SALMON
        resolved = self._resolve_explain_target(target_name)
        if resolved is None:
            return None
        kind, obj = resolved
        ctx = self._explain_ctx()
        if kind == "location":
            region = obj.parent_region
            ok = bool(obj.can_reach(state)) if state is not None else None
            head = _colored(obj.name, _GREEN if ok else _SALMON)
            suffix = (" — reachable" if ok
                      else " — not reachable") if ok is not None else ""
        else:
            region = obj
            ok = bool(region.can_reach(state)) if state is not None else None
            head = _colored(self._region_label(region),
                            _GREEN if ok else _SALMON)
            suffix = (" — reachable" if ok
                      else " — not reachable") if ok is not None else ""
        parts = [head, _text(suffix)]
        # When the target is UNREACHABLE, recurse into each blocked source
        # region so the user can see WHY it's blocked all the way up the chain
        # — not just a dead-end "Burenia [blocked]". A reachable target only
        # needs its own gated entrances (one level).
        recurse = ok is False
        visited = {region.name} if region is not None else set()
        self._explain_region_tree(region, ctx, state, parts, 0, visited, recurse)
        return parts

    def _explain_region_tree(self, region, ctx, state, parts, depth,
                             visited, recurse) -> None:
        """Append a region's gated entrances (indented by ``depth``); when
        ``recurse`` and a source region is itself blocked, descend into it so
        the explanation shows the full unreachable chain. Guarded by a
        visited-set (cycles) and a depth cap (bounds output size)."""
        from .ut_explain import _text
        MAX_DEPTH = 5
        indent = "  " * (depth + 1)
        entrances = list(region.entrances) if region else []
        if not entrances:
            parts.append(_text(f"\n{indent}(no gated entrances)"))
            return
        for ent in entrances:
            parts.append(_text(f"\n{indent}"))
            parts.extend(self._explain_entrance_parts(ent, ctx, state))
            if not recurse:
                continue
            src = ent.parent_region
            if (state is None or src is None or src.can_reach(state)
                    or src.name in visited):
                continue
            visited.add(src.name)
            if depth + 1 >= MAX_DEPTH:
                parts.append(_text(f"\n{indent}  ... (deeper path truncated)"))
                continue
            self._explain_region_tree(src, ctx, state, parts, depth + 1,
                                      visited, recurse)

    # Progressive-item logic translation. The compiled access rules reference
    # the individual tier items by name (e.g. state.has("Wave Beam")), but when
    # a progressive group is enabled those tiers aren't in the pool — only the
    # "Progressive X" item is. So we mirror each progressive copy onto its tier
    # component in `state.prog_items`: the k-th "Progressive Beam" credits the
    # k-th tier (Wide, then Plasma, then Wave), keeping the rules untouched.
    # `remove` is the exact inverse so collect/remove round-trip during fill.
    # (Standard AP progressive idiom; see [[ap-nonadvancement-invisible-to-state]]
    # — the progressive items are `progression`, so AP's sweep calls collect.)
    # Main ability → (ammo item, "included ammo" option attr). Collecting the
    # main Flash Shift credits its `included_ammo` (Randovania's term) onto the
    # Flash Shift Upgrade item in ACCESS LOGIC — exactly mirroring the patcher
    # bundling them into the main pickup (see pickup_resource_stage /
    # ammo_amount_override). So state.has("Flash Shift Upgrade", 2) clears once
    # the main is reachable, with no upgrades shuffled. The credited count is the
    # option value (one Flash Shift Upgrade per FlashUpgrade unit, matching the
    # logic atom); remove is the exact inverse so collect/remove round-trip during
    # fill. Speed Booster is NOT here: in Randovania the Speed Booster major
    # includes no charge upgrades (Speed Booster Upgrade is a standalone standard
    # pickup), so the main credits nothing.
    MAIN_CHAIN_CREDIT: dict[str, tuple[str, str]] = {
        "Flash Shift": ("Flash Shift Upgrade", "flash_shift_included_ammo"),
    }

    def collect(self, state, item) -> bool:
        change = super().collect(state, item)
        if change:
            tiers = PROGRESSIVE_TIERS.get(item.name)
            if tiers:
                # super() just credited this progressive item; n is the new
                # count, so the k-th copy grants the k-th tier.
                n = state.prog_items[self.player][item.name]
                if n <= len(tiers):
                    state.add_item(tiers[n - 1], self.player)
            credit = self.MAIN_CHAIN_CREDIT.get(item.name)
            if credit:
                chain_item, opt_attr = credit
                for _ in range(int(getattr(self.options, opt_attr).value)):
                    state.add_item(chain_item, self.player)
        return change

    def remove(self, state, item) -> bool:
        change = super().remove(state, item)
        if change:
            tiers = PROGRESSIVE_TIERS.get(item.name)
            if tiers:
                # super() just removed one copy; n is the new (post-decrement)
                # count, so tiers[n] is the tier that copy had granted.
                n = state.prog_items[self.player][item.name]
                if n < len(tiers):
                    state.remove_item(tiers[n], self.player)
            credit = self.MAIN_CHAIN_CREDIT.get(item.name)
            if credit:
                chain_item, opt_attr = credit
                for _ in range(int(getattr(self.options, opt_attr).value)):
                    state.remove_item(chain_item, self.player)
        return change

    # Baseline starting inventory — matches Randovania's starter preset.
    #   - Slide: required to pass under the first low ceiling in s010_cave
    #     (the very first room after the intro). A genuine logic gate (191 rule
    #     atoms); no slide == softlock at literal step 1.
    #   - Sonar (Pulse Radar): does NOT gate anything in our access logic (0
    #     rule atoms — Randovania never requires it for traversal; it only
    #     reveals breakable blocks). Included here to mirror the preset, but
    #     it's opt-out via start_with_pulse_radar — turning it off leaves
    #     solvability untouched and makes it a findable pickup. (ITEM_SONAR is
    #     dropped from the patcher starting_items in that case.)
    #   - 15 starting missile capacity: matches Randovania default; vanilla
    #     gives 5. Less than ~10 makes early-game boss fights unwinnable. NOTE:
    #     the compiled ammo `sum` thresholds bake in this 15, so it's the one
    #     starter that is also a (mild) logic assumption.
    # Rando artifacts are handled dynamically in _build_placements_payload:
    # the RequiredArtifacts option picks N, the in-game gate checks
    # ITEM_RANDO_ARTIFACT_1..N (granted by the N placed Metroid DNA pickups),
    # and artifacts N+1..12 are added to the starting inventory there so the
    # remaining artifact flags are pre-satisfied (mirroring the starter
    # preset, which placed 3 and started 9).
    DEFAULT_STARTING_ITEMS: dict[str, int] = {
        "ITEM_FLOOR_SLIDE": 1,
        "ITEM_SONAR": 1,
        "ITEM_WEAPON_MISSILE_MAX": 15,
    }

    # Randovania starter abilities — precollected into AP logic AND granted by
    # the patcher. Slide is starting-only (not findable); Missile Tank is
    # precollected for the starting capacity but stays findable. Pulse Radar is
    # precollected only when start_with_pulse_radar is on (default); otherwise
    # it's filtered out of this set and shuffled into the findable pool.
    BASE_STARTING_ITEMS: tuple[str, ...] = ("Slide", "Pulse Radar", "Missile Tank")

    # Extra items forced as STARTING items beyond the Randovania starter set,
    # to clear fill bottlenecks in the globally-faithful (forward-resolver,
    # item-only) logic. Each entry is precollected into AP logic, removed from
    # the findable pool, and added to the patcher's starting_items so the game
    # grants it too.
    #
    # NOW EMPTY. This used to hold Charge Beam: the rules make it a
    # near-universal early prerequisite, and before Missile Tank was classified
    # advancement, AP's fill_restrictive had too few early-reachable spots to
    # place it. The Missile-Tank fix (commit 32f3da2) opened the early reachable
    # set enough that Charge Beam now places normally as a findable item —
    # verified over 146 generations (solo + multiworld, every trick level,
    # accessibility minimal/items/full, up to 30 seeds per config) with zero
    # fill failures. Dropping it is also more faithful to Randovania, whose
    # starter preset ships Charge Beam as a findable pickup, not a start item.
    EXTRA_STARTING_ITEMS: tuple[str, ...] = ()

    # ---- Universal Tracker integration ------------------------------------
    #
    # UT (https://github.com/FarisTheAncient/Archipelago tracker branch) recomputes
    # a slot's reachable locations by re-running generation from slot_data. Our
    # world builds a CUSTOM per-seed region graph (door-lock rando, transport
    # rando, start-area gating — all rolled from self.random in generate_early),
    # so without restoring those decisions UT would re-roll a different graph and
    # report a wildly wrong in-logic set. We export the rolled state (and the
    # logic-affecting options) in `ut_state` and restore it on a UT regen.

    @staticmethod
    def interpret_slot_data(slot_data: dict[str, Any]) -> dict[str, Any]:
        """Universal Tracker hook. Returning the slot_data (a truthy value) tells
        UT to run a re-generation pass with ``multiworld.re_gen_passthrough[GAME]``
        set to this dict, so :meth:`generate_early` can restore the seed's per-seed
        randomization from ``ut_state`` instead of re-rolling it."""
        return slot_data

    def _ut_passthrough(self) -> dict[str, Any] | None:
        """The ``ut_state`` dict UT handed back via ``re_gen_passthrough`` for this
        game, or None during a normal generation."""
        mw = self.multiworld
        passthrough = getattr(mw, "re_gen_passthrough", None)
        if not passthrough or GAME_NAME not in passthrough:
            return None
        slot_data = passthrough[GAME_NAME] or {}
        return slot_data.get("ut_state")

    # Options that are part of every game (accessibility, progression_balancing,
    # start_inventory, …) are owned by UT/the YAML and need no round-trip; only the
    # Dread-specific options affect the graph/rules we rebuild.
    def _dread_option_keys(self) -> list[str]:
        from Options import PerGameCommonOptions
        common = set(PerGameCommonOptions.type_hints)
        return [k for k in self.options_dataclass.type_hints if k not in common]

    def _ut_state(self) -> dict[str, Any]:
        """Per-seed state Universal Tracker needs to reconstruct this slot's
        region graph + rules on a regen (see the class comment above). Persists
        (a) every Dread-specific option value — so the regen matches even when UT
        has no player YAML — and (b) the rolled door/transport/start decisions."""
        opts: dict[str, Any] = {}
        for key in self._dread_option_keys():
            opt = getattr(self.options, key, None)
            if opt is None:
                continue
            val = opt.value
            if isinstance(val, (set, frozenset)):
                val = sorted(val)            # OptionSet → JSON-serializable list
            opts[key] = val
        return {
            "options": opts,
            "dock_assignments": dict(getattr(self, "_dock_assignments", {}) or {}),
            "transport_matching": dict(getattr(self, "_transport_matching", {}) or {}),
            "start_comp": getattr(self, "_start_comp", None),
            "start_patcher": getattr(self, "_start_patcher", None),
            "start_extra_items": list(getattr(self, "_start_extra_items", []) or []),
        }

    def _restore_ut_state(self, ut_state: dict[str, Any]) -> None:
        """Apply a previously-exported :meth:`_ut_state` onto this world during a
        UT regen: restore the logic-affecting options, then the rolled per-seed
        decisions (so create_regions/set_rules rebuild the original graph without
        touching self.random)."""
        for key, value in (ut_state.get("options") or {}).items():
            opt = getattr(self.options, key, None)
            if opt is None:
                continue
            # OptionSet values were stored as lists; restore them as sets.
            opt.value = (set(value)
                         if isinstance(opt.value, (set, frozenset)) else value)
        self._dock_assignments = dict(ut_state.get("dock_assignments") or {})
        self._transport_matching = dict(ut_state.get("transport_matching") or {})
        sc = ut_state.get("start_comp")
        self._start_comp = int(sc) if sc is not None else None
        self._start_patcher = ut_state.get("start_patcher")
        self._start_extra_items = list(ut_state.get("start_extra_items") or [])

    def _build_placements_payload(self) -> dict[str, Any]:
        """Build the per-slot placements payload.

        Shared between ``fill_slot_data`` (transmitted to the client at
        connect time, used by the in-client ``/patch`` command) and
        ``generate_output`` (also written as a sibling JSON in the seed
        zip for the CLI ``scripts/seed_to_patcher_overrides.py`` flow).
        Schema is documented at
        [scripts/seed_to_patcher_overrides.py](../../scripts/seed_to_patcher_overrides.py).
        """
        slot_name = self.multiworld.get_player_name(self.player)
        seed_id = str(self.multiworld.seed_name)

        from .Tricks import effective_trick_levels

        o = self.options
        # AP item name → per-pickup grant amount (mirrors Randovania's
        # `ammo_count`). Shared by the placement loop (own seed-baked grants)
        # and the slot_data `item_amounts` payload (live wire deliveries), so
        # both paths grant the same configured amount.
        ammo_amount_override = {
            "Power Bomb":            int(o.starting_power_bombs.value),
            "Missile Tank":          int(o.missile_tank_ammo.value),
            "Missile+ Tank":         int(o.missile_plus_tank_ammo.value),
            "Power Bomb Tank":       int(o.power_bomb_tank_ammo.value),
            "Flash Shift Upgrade":   int(o.flash_shift_upgrade_amount.value),
            # Speed Booster Upgrade has no entry: it's a standard pickup in
            # Randovania (no per-pickup ammo_count), so each copy grants its
            # items.json quantity of 1. The MAIN Flash Shift bundles its
            # `included_ammo` FlashUpgrade into
            # the pickup (pickup_resource_stage expands ITEM_GHOST_AURA into
            # unlock + N chain). The value is the FlashUpgrade count, not a copy
            # count. This flows to both the seed-baked placement quantity AND
            # slot_data item_amounts, so own and wire-delivered mains bundle the
            # same chains. Speed Booster has no included ammo (its upgrade is a
            # standalone standard pickup), so its main stays a single resource.
            "Flash Shift":           int(o.flash_shift_included_ammo.value),
        }
        placements: list[dict[str, Any]] = []
        for loc in self.multiworld.get_locations(self.player):
            loc_data = location_name_to_location.get(loc.name)
            if loc_data is None:
                continue
            item = loc.item
            if item is None:
                continue
            recipient_slot = self.multiworld.get_player_name(item.player)
            is_own = (item.player == self.player)
            patcher_item_id = ""
            quantity = 1
            patcher_model = ""
            ap_item_name = item.name
            # Progressive (multi-stage) fields — populated only for an own-slot
            # progressive item; None for everything else (single-stage path).
            progression_stages = None
            progression_models = None
            map_icon_id = None
            if is_own:
                own_item_data = item_name_to_item.get(item.name)
                if own_item_data is not None and own_item_data.progression_tiers:
                    # Progressive item: emit the FULL multi-stage resources +
                    # per-tier model list + progressive map icon, exactly like
                    # the starter preset's progressive pickups. The game grants
                    # the next missing tier from this full progression, so the
                    # local (seed-baked) and remote (wire) paths behave alike.
                    from .client.protocol import pickup_resource_stage
                    tiers = [item_name_to_item[t]
                             for t in own_item_data.progression_tiers]
                    progression_stages = [
                        pickup_resource_stage(t.patcher_item_id, t.quantity)
                        for t in tiers
                    ]
                    progression_models = [t.model_name for t in tiers]
                    map_icon_id = PROGRESSIVE_MAP_ICON.get(item.name)
                elif own_item_data is not None:
                    patcher_item_id = own_item_data.patcher_item_id
                    quantity = own_item_data.quantity
                    # In-world sphere model for THIS item, so a shuffled pickup
                    # shows what it grants instead of the starter preset's stale
                    # vanilla model (cross-slot items get CROSS_SLOT_MODEL).
                    patcher_model = own_item_data.model_name
                    # Per-pickup grant amounts: each option overrides items.json's
                    # vanilla default (mirrors Randovania's `ammo_count`). The
                    # main Power Bomb pickup grants weapon + N PB capacity; the
                    # ammo/upgrade tanks grant their configured amount. Applied to
                    # OWN items only — cross-slot copies keep quantity=1 and the
                    # receiving player's own seed owns their amount.
                    if item.name in ammo_amount_override:
                        quantity = ammo_amount_override[item.name]
            placement = {
                "location_name": loc_data.name,
                "scenario": loc_data.scenario,
                "actor": loc_data.actor,
                "pickup_type": loc_data.pickup_type,
                "pickup_index": loc_data.pickup_index,
                "ap_item_name": ap_item_name,
                "patcher_item_id": patcher_item_id,
                "patcher_model": patcher_model,
                "quantity": quantity,
                "recipient_slot_name": recipient_slot,
                "is_own_player": is_own,
            }
            if progression_stages is not None:
                placement["progression_stages"] = progression_stages
                placement["models"] = progression_models
                placement["map_icon_id"] = map_icon_id
            placements.append(placement)

        n_dna = int(o.required_artifacts.value)
        # Starting inventory: baseline + the artifacts the player ISN'T required
        # to collect (N+1..12), so the in-game gate (which checks 1..N) is
        # satisfied exactly by collecting the N placed Metroid DNA.
        starting_items = dict(self.DEFAULT_STARTING_ITEMS)
        # Starting missile capacity is option-driven (DEFAULT_STARTING_ITEMS is
        # the vanilla fallback for offline CLI flows that don't pass options).
        starting_items["ITEM_WEAPON_MISSILE_MAX"] = int(o.starting_missiles.value)
        # Pulse Radar (ITEM_SONAR) is granted at start only when opted in; when
        # off it's a findable pickup, so the patcher must not pre-grant it.
        if not bool(o.start_with_pulse_radar.value):
            starting_items.pop("ITEM_SONAR", None)
        for k in range(n_dna + 1, 13):
            starting_items[f"ITEM_RANDO_ARTIFACT_{k}"] = 1
        # The forced bottleneck starting items must ALSO be granted in-game, or
        # the player would have them in AP logic but not on the Switch. This
        # covers BOTH the static EXTRA_STARTING_ITEMS (currently empty) AND the
        # per-spawn bootstrap kit (_start_extra_items) that more-starting-areas
        # computes per seed: a non-Artaria spawn precollects that kit into AP
        # logic and drops it from the findable pool (see create_items), so the
        # ROM must grant the same items or the player spawns in a deep region
        # without the unlocks logic assumed and is stuck in the opening rooms.
        forced_start_abilities = (list(self.EXTRA_STARTING_ITEMS)
                                  + list(getattr(self, "_start_extra_items", [])
                                         or []))
        from .client.protocol import pickup_resource_stage
        for name in forced_start_abilities:
            data = item_name_to_item.get(name)
            if not (data and data.patcher_item_id):
                continue
            # Expand paired-resource items so a baked starter is actually usable.
            # The Power Bomb "launcher" (ITEM_WEAPON_POWER_BOMB) must ALSO grant
            # ITEM_WEAPON_POWER_BOMB_MAX capacity — granting only the unlock flag
            # leaves the player at 0/0 power bombs (unusable), even though AP logic
            # credits the launcher's starting_power_bombs capacity. This was the
            # start-location-randomizer "0/0 power bombs" bug. The capacity comes
            # from the per-pickup ammo override (starting_power_bombs); other
            # unlocks fall back to their items.json quantity. Mirrors the seed-
            # baked placement path (which already uses pickup_resource_stage).
            qty = ammo_amount_override.get(name, max(1, int(data.quantity)))
            for res in pickup_resource_stage(data.patcher_item_id, qty):
                starting_items[res["item_id"]] = max(1, int(res["quantity"]))

        # User-configured AP start_inventory / start_inventory_from_pool. AP
        # precollects these into logic and also streams them to the client as
        # ReceivedItems, but that wire-delivery path is best-effort and strictly
        # head-of-line ordered (RL.ReceivePickup only grants when its index ==
        # the game's ReceivedPickups counter, so one stalled grant blocks the
        # rest). Bake the *abilities* into the ROM's starting_items so they are
        # granted at save creation, reliably, the same way the forced starters
        # above are — and harmlessly, since re-granting a boolean ability over
        # the wire is idempotent (SetItemAmount stays >0).
        #
        # Counter items are deliberately EXCLUDED: their OnPickedUp grant is
        # additive, so a ROM grant + the (working) wire grant would stack
        # (e.g. a start_inventory Missile Tank would add its capacity twice).
        # The artifacts are the DNA goal — granting one at start would satisfy
        # the in-game gate early. Both stay on the wire path, which already
        # grants additive items correctly.
        _NON_BAKEABLE_PREFIXES = ("ITEM_RANDO_ARTIFACT_",)
        _NON_BAKEABLE_IDS = {
            "ITEM_WEAPON_MISSILE_MAX", "ITEM_WEAPON_POWER_BOMB_MAX",
            "ITEM_ENERGY_TANKS", "ITEM_LIFE_SHARDS", "ITEM_MAX_LIFE",
            "ITEM_UPGRADE_FLASH_SHIFT_CHAIN", "ITEM_UPGRADE_SPEED_BOOST_CHARGE",
        }
        start_inv: set[str] = set()
        for opt_name in ("start_inventory", "start_inventory_from_pool"):
            opt = getattr(o, opt_name, None)
            if opt is None:
                continue
            for name, count in dict(getattr(opt, "value", {}) or {}).items():
                if int(count) > 0:
                    start_inv.add(name)
        for name in start_inv:
            data = item_name_to_item.get(name)
            if not (data and data.patcher_item_id):
                continue
            pid = data.patcher_item_id
            if pid in _NON_BAKEABLE_IDS or pid.startswith(_NON_BAKEABLE_PREFIXES):
                continue
            starting_items[pid] = max(1, int(data.quantity))
        # Resolve cosmetic/combat options to the exact patcher values here so
        # patcher_pipeline stays AP-import-free. Choices map their current_key
        # to the schema string (room name is upper-cased; raven beak keys ARE
        # the schema strings).
        cosmetic_combat = {
            "bShowBossLifebar": bool(o.show_boss_lifebar.value),
            "bShowEnemyLife": bool(o.show_enemy_life.value),
            "bShowEnemyDamage": bool(o.show_enemy_damage.value),
            "bShowPlayerDamage": bool(o.show_player_damage.value),
            "enable_death_counter": bool(o.enable_death_counter.value),
            "show_dna_in_hud": bool(o.show_dna_in_hud.value),
            "enable_room_name_display": o.room_name_display.current_key.upper(),
            "raven_beak_damage_table_handling": o.raven_beak_damage_table.current_key,
            "nerf_power_bombs": bool(o.nerf_power_bombs.value),
            # Release the X from Elun at game start. Logic-safe to toggle (the
            # compiled rules assume the release must be triggered, so ON only
            # relaxes X-gated encounters). Routed to game_patches.default_x_released.
            "default_x_released": bool(o.x_starts_released.value),
            # Top-level patcher field — controls Samus's base max HP and each
            # Energy Tank's grant. Routed via COSMETIC_COMBAT_PATHS in
            # patcher_pipeline.py.
            "energy_per_tank": int(o.energy_per_tank.value),
        }
        return {
            "slot_name": slot_name,
            "seed_id": seed_id,
            "starting_area": int(o.starting_area.value),
            "include_boss_pickups": bool(o.include_boss_pickups.value),
            # Client-only: drives whether DreadContext enables the DeathLink tag
            # and the death-detection poll. Not consumed by the patcher.
            "death_link": bool(o.death_link.value),
            "starting_items": starting_items,
            "cosmetic_combat": cosmetic_combat,
            "required_artifacts": n_dna,
            # Per-trick effective difficulty levels ({short_name: level}, 0..5),
            # resolved from the global Trick Level baseline + any per-trick
            # overrides (Tricks.effective_trick_levels). Client-only: lets
            # external trackers (e.g. PopTracker) pre-populate their per-trick
            # difficulty dials from slot_data so reachability matches the seed.
            # Not consumed by the patcher.
            "trick_levels": effective_trick_levels(o),
            # Per-pickup grant amounts (AP item name → capacity). The client
            # reads this from slot_data so wire-delivered (multiworld) copies
            # grant the seed's configured amount instead of the items.json
            # default. Same map the placement quantities above were baked from.
            "item_amounts": ammo_amount_override,
            # Real AP placement hints baked into the in-game Nav Station
            # plaques. Computed in pre_output (post-fill, pre-multidata) and
            # stashed there so it's already set when this method runs later in
            # generate_output / fill_slot_data. Empty in offline / direct-call
            # flows that skip pre_output ⇒ patcher falls back to neutral filler.
            "nav_hints": getattr(self, "_nav_hints", []),
            # End-credits "Major Item Locations" (open-dread-rando's
            # patch_credits renders this after the randomizer credits). Real
            # post-fill placements of this slot's major items, replacing the
            # starter preset's baked example log — which is false for any AP
            # seed. Computed in pre_output like nav_hints; empty in offline /
            # direct-call flows ⇒ the credits section is blanked (patch_credits
            # skips an empty dict) rather than left lying.
            "spoiler_log": getattr(self, "_spoiler_log", {}),
            "placements": placements,
            # Door-lock rando: open-dread-rando door_patches (one per physical
            # door). Empty when door rando is off.
            "door_patches": self._door_patches(),
            # Transport rando: open-dread-rando elevators config. Empty when off.
            "elevators": self._elevators(),
            # Transport rando: room-name-display overrides so the in-game room
            # name of each shuffled ride's source room reflects its NEW
            # destination instead of the vanilla one. Empty when off.
            "transport_room_names": self._transport_room_names(),
            # Disabled Lights: one mass_delete_actors entry per region whose
            # light actors are stripped from the RomFS (Randovania's per-region
            # "Lights Out"). Empty when no region is darkened. Purely visual —
            # no logic impact.
            "light_patches": self._light_patches(),
            # More-starting-areas: resolved spawn {scenario, actor} for a
            # non-Artaria start, else None (patcher uses the Artaria default).
            "start_location_override": getattr(self, "_start_patcher", None),
        }

    def _light_patches(self) -> list:
        """mass_delete_actors entries for the Disabled Lights option (mirrors
        Randovania's DreadPatchDataFactory._light_patches). Empty ⇒ the patcher
        input carries no mass_delete_actors block at all."""
        regions = sorted(self.options.disabled_lights.value)
        if not regions:
            return []
        from .patcher_pipeline import light_patches_for_regions
        return light_patches_for_regions(regions)

    def _door_patches(self) -> list:
        assign = getattr(self, "_dock_assignments", None)
        if not assign:
            return []
        from .DoorRando import assignments_to_door_patches
        from .graph_logic import load_graph
        return assignments_to_door_patches(assign, load_graph())

    def _elevators(self) -> list:
        matching = getattr(self, "_transport_matching", None)
        if not matching:
            return []
        from .TransportRando import matching_to_elevators
        from .graph_logic import load_graph
        return matching_to_elevators(matching, load_graph())

    def _transport_room_names(self) -> dict:
        matching = getattr(self, "_transport_matching", None)
        if not matching:
            return {}
        from .TransportRando import matching_to_room_names
        from .graph_logic import load_graph
        return matching_to_room_names(matching, load_graph())

    def pre_output(self) -> None:
        """Generate the in-game Nav Station hint text.

        Runs after fill so every location has its item. The rendered text is
        stashed on ``self`` for :meth:`_build_placements_payload`, which bakes
        it into the patcher output so the game's Nav Station plaques show real
        placement facts. No AP server hint is registered HERE (at generation):
        each plaque also carries its revealed ``loc``, and the client registers
        that as a free AP hint only when the player actually reaches the station
        (``DreadContext._register_nav_hints``) — so nothing is broadcast at
        session start."""
        self._nav_hints = self._generate_nav_hints()
        self._spoiler_log = self._generate_spoiler_log()

    def _generate_nav_hints(self) -> list[dict[str, Any]]:
        """Pick real cross-world placement facts and render them as Nav Station
        hint text.

        Half are "location" hints (what sits at one of THIS slot's own pickups);
        half are "item" hints (where one of THIS slot's own items ended up).
        Deterministic under ``self.random``. ``{c1}`` / ``{c5}`` / ``{c0}``
        are the game's colour escapes, kept so the lines render like the
        starter preset's originals."""
        mw = self.multiworld
        me = self.player
        rng = self.random

        def place(loc: Any) -> str:
            if loc.player == me:
                return f"{{c5}}{loc.name}{{c0}}"
            return f"{{c5}}{mw.get_player_name(loc.player)}{{c0}}'s {{c5}}{loc.name}{{c0}}"

        def _hint_loc(loc: Any) -> list[int] | None:
            # [location-owner slot, location id] for the client to register as a
            # free AP server hint (CreateHints / LocationScouts) when the player
            # reaches the Nav Station showing this plaque. None for an addressless
            # (event) location, which can't be hinted.
            return None if loc.address is None else [loc.player, loc.address]

        # "location" candidates: this slot's own filled pickups.
        my_locs = [loc for loc in mw.get_filled_locations(me)
                   if loc.address is not None and loc.item is not None]
        # "item" candidates: where this slot's own items landed, anywhere in the
        # multiworld. Keep only item names unique in our pool so each hint text
        # resolves to exactly one location.
        mine_anywhere = [loc for loc in mw.get_filled_locations()
                         if loc.item is not None and loc.item.player == me
                         and loc.address is not None]
        name_counts = Counter(loc.item.name for loc in mine_anywhere)
        item_locs = [loc for loc in mine_anywhere if name_counts[loc.item.name] == 1]

        def prefer_advancement(locs: list[Any]) -> list[Any]:
            adv = [loc for loc in locs if loc.item.advancement]
            rest = [loc for loc in locs if not loc.item.advancement]
            rng.shuffle(adv)
            rng.shuffle(rest)
            return adv + rest

        loc_pool = prefer_advancement(my_locs)
        item_pool = prefer_advancement(item_locs)

        used: set[tuple[int, str]] = set()  # (player, location name) already hinted
        hints: list[dict[str, Any]] = []

        # "Hint All Metroid DNA": lead with a guaranteed hint for every required
        # DNA's location (mirrors Randovania's Dairon/Itorash DNA hints). Each
        # Metroid DNA item is unique to this slot and the goal, so surfacing all
        # of them is the high-value information a player wants from the stations.
        n_dna = int(self.options.required_artifacts.value)
        if n_dna > 0 and bool(self.options.hint_all_dna.value):
            dna_names = {f"Metroid DNA {k}" for k in range(1, n_dna + 1)}
            dna_locs = sorted(
                (loc for loc in mine_anywhere if loc.item.name in dna_names),
                key=lambda loc: loc.item.name,
            )
            for loc in dna_locs:
                key = (loc.player, loc.name)
                if key in used:
                    continue
                used.add(key)
                hints.append({"text": f"Your {{c1}}{loc.item.name}{{c0}} "
                                      f"is at {place(loc)}.",
                              "loc": _hint_loc(loc)})

        def take_location_hint() -> dict[str, Any] | None:
            for loc in loc_pool:
                key = (loc.player, loc.name)
                if key in used:
                    continue
                used.add(key)
                item = loc.item
                if item.player == me:
                    text = f"{place(loc)} holds your {{c1}}{item.name}{{c0}}."
                else:
                    who = mw.get_player_name(item.player)
                    text = (f"{place(loc)} holds {{c1}}{item.name}{{c0}} "
                            f"for {{c5}}{who}{{c0}}.")
                return {"text": text, "loc": _hint_loc(loc)}
            return None

        def take_item_hint() -> dict[str, Any] | None:
            for loc in item_pool:
                key = (loc.player, loc.name)
                if key in used:
                    continue
                used.add(key)
                text = f"Your {{c1}}{loc.item.name}{{c0}} is at {place(loc)}."
                return {"text": text, "loc": _hint_loc(loc)}
            return None

        # DNA hints (if any) take the front; fill the REMAINING plaque budget
        # with the usual mixed location/item hints so the total never exceeds the
        # plaque count (every generated hint past NAV_HINT_COUNT is dropped by
        # _apply_nav_hints, which would otherwise shuffle a DNA hint out of view).
        forced = hints[:NAV_HINT_COUNT]   # guard the 12-DNA / 11-plaque edge
        hints = forced[:]
        remaining = NAV_HINT_COUNT - len(hints)
        n_item = remaining // 2
        n_loc = remaining - n_item
        for _ in range(n_loc):
            h = take_location_hint()
            if h:
                hints.append(h)
        for _ in range(n_item):
            h = take_item_hint()
            if h:
                hints.append(h)
        # Top up from whichever pool still has unused material if one ran short.
        while len(hints) < NAV_HINT_COUNT:
            h = take_location_hint() or take_item_hint()
            if h is None:
                break
            hints.append(h)

        # Shuffle only the non-forced tail so every DNA hint stays within the
        # shown plaque budget (order among plaques is otherwise cosmetic).
        tail = hints[len(forced):]
        rng.shuffle(tail)
        return forced + tail

    def _generate_spoiler_log(self) -> dict[str, str]:
        """Real "Major Item Locations" for the end credits.

        open-dread-rando's ``patch_credits`` renders the patcher input's
        ``spoiler_log`` (item name → location description) as a "Major Item
        Locations" section after the randomizer credits — shown only once the
        run is beaten, so real spoilers are appropriate (Randovania does the
        same). The starter preset template bakes Randovania's own example
        placements there, false for any AP seed; this replaces them with where
        this slot's majors actually landed.

        Majors mirror the template's curation: the unique single-copy
        abilities, the Progressive group items, and the placed Metroid DNA —
        tanks/ammo/chain upgrades are excluded. Multi-copy items (progressives)
        newline-join their locations, matching the template's format. A major
        in another slot's world renders as "<player>'s <location>". Precollected
        starters aren't placed anywhere, so they simply don't appear.
        Deterministic (no RNG): entries follow items.json definition order,
        locations sort alphabetically within an entry."""
        mw = self.multiworld
        me = self.player

        def place(loc: Any) -> str:
            if loc.player == me:
                return loc.name
            return f"{mw.get_player_name(loc.player)}'s {loc.name}"

        def is_major(name: str) -> bool:
            data = item_name_to_item.get(name)
            if data is None:
                return False
            return (bool(data.progression_tiers)
                    or name.startswith("Metroid DNA")
                    or data.pool_count == 1)

        by_name: dict[str, list[str]] = {}
        for loc in mw.get_filled_locations():
            item = loc.item
            if item is None or item.player != me or loc.address is None:
                continue
            if is_major(item.name):
                by_name.setdefault(item.name, []).append(place(loc))

        return {it.name: "\n".join(sorted(by_name[it.name]))
                for it in item_table if it.name in by_name}

    def fill_slot_data(self) -> dict[str, Any]:
        # Bundle the full placements payload so the in-client /patch command
        # can build the patcher input from just the AP connection (no local
        # seed zip required). Adds ~100-200 KB to the slot's payload —
        # acceptable trade for the better UX. The legacy CLI conversion
        # path still works because generate_output writes the same JSON to
        # the seed zip too.
        payload = self._build_placements_payload()
        payload["location_count"] = len(location_table)
        payload["item_count"] = len(item_table)
        # Universal Tracker regen state (additive; the client ignores it). Only
        # on the slot_data path — the offline CLI placements JSON doesn't need it.
        payload["ut_state"] = self._ut_state()
        return payload

    def get_filler_item_name(self) -> str:
        # Respect the user's intent: if Missile Tank is dialed to zero, don't
        # sneak it back in via filler. Fall through the alternates in roughly
        # increasing impact order.
        o = self.options
        if int(o.missile_tank_count.value) > 0:
            return "Missile Tank"
        if int(o.energy_part_count.value) > 0:
            return "Energy Part"
        if int(o.power_bomb_tank_count.value) > 0:
            return "Power Bomb Tank"
        return "Missile Tank"  # AP-API safety net

    def generate_output(self, output_directory: str) -> None:
        """Write the per-slot artifacts AP bundles into the seed zip:

        * ``<base>.dreadap`` — the clickable Launcher entry point. Double-
          clicking it opens the Dread Client pre-filled with this slot's name
          (see ``client/dreadap_file.py`` + ``launch_dread_client``).
        * ``AP_<seed>_P<n>_Dread_<slot>.json`` — the placements payload the
          CLI patcher path (``scripts/seed_to_patcher_overrides.py``) consumes.
          The same payload also rides ``fill_slot_data`` for in-client
          ``/patch``, so this file is only needed for the offline CLI flow.
        """
        payload = self._build_placements_payload()
        seed_id = payload["seed_id"]
        slot_name = payload["slot_name"]
        out_dir = Path(output_directory)

        # Clickable launcher file. server_address is intentionally empty — the
        # generator can't know where the user will host; the client's Connect
        # bar prompts for it.
        from .client.dreadap_file import DreadapFile
        base = self.multiworld.get_out_file_name_base(self.player)
        DreadapFile(
            slot_name=slot_name,
            seed_name=seed_id,
        ).write(out_dir / f"{base}.dreadap")

        # Legacy placements JSON for the CLI patcher path.
        out_path = out_dir / f"AP_{seed_id}_P{self.player}_Dread_{slot_name}.json"
        out_path.write_text(json.dumps(payload, indent=2))
