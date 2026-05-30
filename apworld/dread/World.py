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
    sweep. This needed (a) classifying logic-required items as progression
    (Missile Tank etc.), and (b) forcing Charge Beam as a starting item
    (EXTRA_STARTING_ITEMS) to clear the early-prerequisite fill bottleneck.
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

        # For items where compiled rules need amount=1 but the pool has many
        # copies, only the FIRST N copies get the row's classification — the
        # rest fall back to "useful" (logic-irrelevant but still placed in
        # reachable spots where possible). Missile+ Tank: 336 rule refs all
        # amount=1, NOT precollected, so the first copy is logic-gating and
        # the other 11 are pure ammo capacity.
        # (Missile Tank doesn't need an entry here — Missile Tank is in
        # BASE_STARTING_ITEMS / precollected, which satisfies its 3634
        # amount=1 atoms from turn 0, so its row classification is already
        # "useful" for every findable copy.)
        MIXED_CLASSIFICATION_FIRST_N = {
            "Missile+ Tank": 1,
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
        forced_starting = tuple(self.BASE_STARTING_ITEMS) + tuple(self.EXTRA_STARTING_ITEMS)
        for name in forced_starting:
            self.multiworld.push_precollected(self.create_item(name))
        # Starting-only items are removed from the findable pool. Missile Tank
        # is precollected for capacity but stays findable.
        pool_excluded = {"Slide", "Pulse Radar"} | set(self.EXTRA_STARTING_ITEMS)

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
    # Without these the seed isn't playable:
    #   - Slide: required to pass under the first low ceiling in s010_cave
    #     (the very first room after the intro). No slide == softlock at
    #     literal step 1.
    #   - Sonar (Pulse Radar): EMMI zones are intended to be entered with
    #     Pulse Radar available; without it some routes become unreachable.
    #   - 15 starting missile capacity: matches Randovania default; vanilla
    #     gives 5. Less than ~10 makes early-game boss fights unwinnable.
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
    # the patcher. Slide + Pulse Radar are starting-only (not findable); Missile
    # Tank is precollected for the starting capacity but stays findable.
    BASE_STARTING_ITEMS: tuple[str, ...] = ("Slide", "Pulse Radar", "Missile Tank")

    # Minimal bottleneck set forced as STARTING items so the globally-faithful
    # (forward-resolver, item-only) logic is fillable. Those rules make Charge
    # Beam a near-universal early prerequisite, so AP's fill_restrictive has too
    # few early-reachable spots to place it; granting it at start clears the
    # bottleneck. Determined empirically as the MINIMAL set (just Charge Beam).
    # Precollected into AP logic, removed from the findable pool, and added to
    # the patcher's starting_items so the game grants it too.
    EXTRA_STARTING_ITEMS: tuple[str, ...] = ("Charge Beam",)

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
            "placements": placements,
        }

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
