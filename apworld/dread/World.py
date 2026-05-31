"""DreadWorld — the AP World subclass.

Registers items / locations / regions, creates one ``DreadItem`` per
item-pool entry, and writes a small slot_data so the client can derive
its mapping at connect time.

Logic status (see docs/randovania-logic-port-notes.md):
  * Forward resolver: a whole-game sphere expansion (scripts/extract_dread_
    rules.py::compile_forward) emits ITEM-ONLY rules — events are INLINED
    (each event atom replaced by its item-only reach cost), so they are no
    longer AP items/locations (we skip them in create_items / create_regions /
    set_rules; the data tables keep them only for AP-ID stability).
    region_access is a plain star — cross-region cost is inlined per rule.
  * accessibility=items/full WORK: item-only rules bootstrap in AP's monotonic
    sweep. This needed classifying logic-required items as progression
    (Missile Tank etc.). An earlier crutch — forcing Charge Beam as a starting
    item (EXTRA_STARTING_ITEMS) to clear an early-prerequisite fill bottleneck —
    is no longer required: once Missile Tank became advancement the early
    reachable set opened up, and Charge Beam places normally as a findable item
    (verified: 146 generations across solo/multiworld × all trick levels ×
    minimal/items/full, 0 fill failures). EXTRA_STARTING_ITEMS is now empty.
  * Trick Level option (3 pre-baked rule files); DNA-collection goal
    (RequiredArtifacts 0-12 + ArtifactPlacement; goal = reach-ship AND N DNA).

Skipped for now (later phases):
  * Progressive items; per-area starting-location randomization; hint
    distribution; per-trick-category granularity; door/elevator randomization.
  * Ammo / damage / E-tank counting (v0.3) — rules collapse ammo to >=1 and
    damage to suit ownership (over/under-permissive, not blocking).
  * Cutscene-safe item delivery — see client/protocol.py + the risk note in
    CLAUDE.md. Needs idempotent (ReceivedPickups-gated) delivery first.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from BaseClasses import Item, ItemClassification, Region, Tutorial
from worlds.AutoWorld import World, WebWorld

from .Items import (
    CLASSIFICATION_MAP, DreadItem, DreadItemData, item_table, item_name_to_id,
    item_name_to_item, get_item_classification,
)
from .Locations import (
    DreadLocation, location_name_to_id, location_table,
    location_name_to_location,
)
from .Options import DreadOptions
from .Regions import create_regions, region_names
from .Rules import set_rules


GAME_NAME = "Metroid Dread"

# Number of in-game Nav Station hint plaques baked into the starter template.
# _generate_nav_hints fills up to this many with real AP placement hints;
# patcher_pipeline._apply_nav_hints maps them onto the template plaques and
# falls back to neutral filler for any shortfall.
NAV_HINT_COUNT = 11


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

    def create_regions(self) -> None:
        create_regions(self)

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
        # Sanity guard: PB gates become unreachable if neither the main pickup
        # nor any tank can grant ammo. Rules currently collapse ammo to >=1
        # (CLAUDE.md v0.3 deferred), so AP fill won't catch this — we raise
        # here at generation time instead.
        if (int(o.power_bomb_tank_count.value) == 0
                and int(o.starting_power_bombs.value) == 0):
            raise OptionError(
                "power_bomb_tank_count=0 with starting_power_bombs=0 makes "
                "Power Bomb gates unreachable. Set at least one to >=1."
            )

        # Option-driven counts override items.json pool_count for tanks.
        pool_overrides = {
            "Energy Tank":     int(o.energy_tank_count.value),
            "Energy Part":     int(o.energy_part_count.value),
            "Missile Tank":    int(o.missile_tank_count.value),
            "Missile+ Tank":   int(o.missile_plus_tank_count.value),
            "Power Bomb Tank": int(o.power_bomb_tank_count.value),
        }

        # For items where the pool has more copies than compiled rules need
        # at amount=1, only the FIRST N copies get the row's classification —
        # the rest fall back to "useful" (logic-irrelevant but still placed in
        # reachable spots where possible).
        #   - Missile+ Tank: 336 amount=1 refs, NOT precollected. The first
        #     copy is logic-gating; the other 11 are ammo capacity.
        #   - Flash Shift Upgrade / Speed Booster Upgrade: rules reference
        #     each up to amount=2 (max amount seen). Real Dread ships 2 of
        #     each, so pool_count=2 matches the game; the FIRST copy is
        #     logic-gating (any rule disjunct needing the upgrade is
        #     satisfiable with just one), the second is QoL routing.
        # (Missile Tank has NO entry on purpose: its 3634 amount=1 atoms +
        # `sum` ammo gates need state.has/count to see it, so EVERY copy stays
        # progression_skip_balancing — not capped to N. Precollecting it does
        # NOT substitute for advancement: AP's collect_item skips non-
        # advancement items, so a `useful` precollected copy never enters
        # prog_items and state.has("Missile Tank") would be permanently False.
        # That was the items/full + minimal generation bug; see
        # test_missile_tank_copies_are_advancement.)
        MIXED_CLASSIFICATION_FIRST_N = {
            "Missile+ Tank": 1,
            "Flash Shift Upgrade": 1,
            "Speed Booster Upgrade": 1,
            # Region floors (Hanubia at trick levels 1/2) gate Menu→Region on
            # `state.has("Energy Tank", 3)`; the first 3 copies are
            # progression-relevant, the remaining 5 are pure HP capacity and
            # would over-saturate the progression pool.
            "Energy Tank": 3,
            # PBAmmo sum gates top out at threshold=2 (= 1 PB Tank's worth),
            # so only the first copy needs to be progression. The remaining
            # 12 are pure ammo capacity.
            "Power Bomb Tank": 1,
        }

        non_event_locations = sum(
            1 for l in location_table if l.pickup_type != "event"
        )

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
        forced_starting = list(self.BASE_STARTING_ITEMS) + list(self.EXTRA_STARTING_ITEMS)
        if not start_with_radar:
            forced_starting = [n for n in forced_starting if n != "Pulse Radar"]
        for name in forced_starting:
            self.multiworld.push_precollected(self.create_item(name))
        # Starting-only items are removed from the findable pool. Missile Tank
        # is precollected for capacity but stays findable. Pulse Radar is only
        # excluded when it's a starting item.
        pool_excluded = {"Slide"} | set(self.EXTRA_STARTING_ITEMS)
        if start_with_radar:
            pool_excluded.add("Pulse Radar")

        pool: list[Item] = []
        for it in item_table:
            if it.name.startswith("Event: "):
                continue
            if it.name.startswith("Metroid DNA"):
                continue
            if it.name in pool_excluded:
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
            # the copies fall back to useful (e.g. Missile+ Tank). For items
            # without an override, n_special == count → every copy uses the
            # row's classification (the legacy behavior).
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
        with guidance pointing at the user-facing knobs to lower."""
        while len(pool) < target:
            pool.append(self.create_item(self.get_filler_item_name()))
        if len(pool) <= target:
            return
        # Trim least-impactful items first.
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
                if pool[i].name == name:
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
        # set_rules owns both add_rule application AND the
        # completion_condition. Don't touch completion_condition here —
        # Rules.py wires it via compile_to_lambda(victory_condition),
        # which currently resolves to ``state.has("Event: Ship", player)``.
        # A post-set_rules override here would silently break that.
        set_rules(self)

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

        o = self.options
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
            ap_item_name = item.name
            if is_own:
                own_item_data = item_name_to_item.get(item.name)
                if own_item_data is not None:
                    patcher_item_id = own_item_data.patcher_item_id
                    quantity = own_item_data.quantity
                    # Main Power Bomb pickup grants weapon + N PB capacity.
                    # The option overrides items.json's vanilla default (2).
                    if item.name == "Power Bomb":
                        quantity = int(o.starting_power_bombs.value)
            placements.append({
                "location_name": loc_data.name,
                "scenario": loc_data.scenario,
                "actor": loc_data.actor,
                "pickup_type": loc_data.pickup_type,
                "pickup_index": loc_data.pickup_index,
                "ap_item_name": ap_item_name,
                "patcher_item_id": patcher_item_id,
                "quantity": quantity,
                "recipient_slot_name": recipient_slot,
                "is_own_player": is_own,
            })

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
        # the player would have them in AP logic but not on the Switch.
        for name in self.EXTRA_STARTING_ITEMS:
            data = item_name_to_item.get(name)
            if data and data.patcher_item_id:
                starting_items[data.patcher_item_id] = max(1, int(data.quantity))
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
            "enable_room_name_display": o.room_name_display.current_key.upper(),
            "raven_beak_damage_table_handling": o.raven_beak_damage_table.current_key,
            "nerf_power_bombs": bool(o.nerf_power_bombs.value),
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
            "starting_items": starting_items,
            "cosmetic_combat": cosmetic_combat,
            "required_artifacts": n_dna,
            # Real AP placement hints baked into the in-game Nav Station
            # plaques. Computed in pre_output (post-fill, pre-multidata) and
            # stashed there so it's already set when this method runs later in
            # generate_output / fill_slot_data. Empty in offline / direct-call
            # flows that skip pre_output ⇒ patcher falls back to neutral filler.
            "nav_hints": getattr(self, "_nav_hints", []),
            "placements": placements,
        }

    def pre_output(self) -> None:
        """Generate the in-game Nav Station hints and register them as real AP
        server hints.

        This runs after fill (every location has its item) and before AP builds
        the multidata — the only window where mutating ``start_location_hints``
        / ``start_hints`` still feeds AP's precollected-hint pass (so the picks
        also show in the tracker and notify recipients). The rendered text is
        stashed on ``self`` for :meth:`_build_placements_payload`, which runs
        later (and concurrently) inside ``generate_output`` / ``fill_slot_data``
        and bakes it into the patcher output."""
        self._nav_hints = self._generate_nav_hints()

    def _generate_nav_hints(self) -> list[dict[str, Any]]:
        """Pick real cross-world placement facts and render them as Nav Station
        hint text, registering each as a real AP server hint as we go.

        Half are "location" hints (what sits at one of THIS slot's own pickups);
        half are "item" hints (where one of THIS slot's own items ended up).
        Both flavours are scoped to this slot so they're registrable through
        this slot's own options: location hints via ``start_location_hints``
        (the location is ours), item hints via ``start_hints`` (the item is
        ours, restricted to item names unique in our pool so the entry resolves
        to exactly one location). Deterministic under ``self.random``. ``{c1}``
        / ``{c5}`` / ``{c0}`` are the game's colour escapes, kept so the lines
        render like the starter preset's originals."""
        mw = self.multiworld
        me = self.player
        rng = self.random

        def place(loc: Any) -> str:
            if loc.player == me:
                return f"{{c5}}{loc.name}{{c0}}"
            return f"{{c5}}{mw.get_player_name(loc.player)}{{c0}}'s {{c5}}{loc.name}{{c0}}"

        # "location" candidates: this slot's own filled pickups.
        my_locs = [loc for loc in mw.get_filled_locations(me)
                   if loc.address is not None and loc.item is not None]
        # "item" candidates: where this slot's own items landed, anywhere in the
        # multiworld. Keep only item names unique in our pool, so the matching
        # start_hints entry maps to a single location.
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
                self.options.start_location_hints.value.add(loc.name)
                return {"text": text}
            return None

        def take_item_hint() -> dict[str, Any] | None:
            for loc in item_pool:
                key = (loc.player, loc.name)
                if key in used:
                    continue
                used.add(key)
                text = f"Your {{c1}}{loc.item.name}{{c0}} is at {place(loc)}."
                self.options.start_hints.value.add(loc.item.name)
                return {"text": text}
            return None

        n_item = NAV_HINT_COUNT // 2
        n_loc = NAV_HINT_COUNT - n_item
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

        rng.shuffle(hints)
        return hints

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
