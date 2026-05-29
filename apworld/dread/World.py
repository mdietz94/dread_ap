"""DreadWorld — the AP World subclass.

Registers items / locations / regions, creates one ``DreadItem`` per
item-pool entry, and writes a small slot_data so the client can derive
its mapping at connect time.

Logic status (see docs/randovania-logic-port-notes.md):
  * Milestone 2 plumbing shipped: 137 actor pickup rules + ~184 event
    reach rules consumed end-to-end. Events are real AP items locked to
    synthetic event locations.

Skipped for v0.1 (lands in later phases):
  * Cross-region access rules (Regions.py still uses star topology — Gate B)
  * Trick-level UI Choice option — Gate B
  * Progressive items (Progressive Beam, Progressive Suit)
  * Per-area starting-location randomization
  * Hint distribution
  * Filler-item rebalancing
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from BaseClasses import Item, Region, Tutorial
from worlds.AutoWorld import World, WebWorld

from .Items import (
    DreadItem, DreadItemData, item_table, item_name_to_id,
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

    def create_item(self, name: str) -> Item:
        return DreadItem(
            name,
            get_item_classification(name),
            item_name_to_id[name],
            self.player,
        )

    def create_regions(self) -> None:
        create_regions(self)

    def create_items(self) -> None:
        # Pool layout (post-M2):
        #   - One copy of every progression / useful game item.
        #   - One copy of each event item (locked to its event location
        #     by Rules.set_rules below — they don't share slots with
        #     the regular pool).
        #   - Filler (Missile Tank) until the regular-pool slots
        #     equal the actor/boss/EMMI/cutscene location count.
        # Total itempool == total location count; events get pulled out
        # and place_locked_item'd by set_rules, leaving exactly the
        # non-event locations for the regular pool.
        non_event_locations = sum(
            1 for l in location_table if l.pickup_type != "event"
        )
        pool: list[Item] = []
        for it in item_table:
            if it.name.startswith("Event: "):
                pool.append(self.create_item(it.name))
                continue
            if it.classification == "progression":
                pool.append(self.create_item(it.name))
            elif it.classification == "useful":
                pool.append(self.create_item(it.name))

        non_event_in_pool = sum(
            1 for i in pool if not i.name.startswith("Event: ")
        )
        filler_name = "Missile Tank"
        while non_event_in_pool < non_event_locations:
            pool.append(self.create_item(filler_name))
            non_event_in_pool += 1
        self.multiworld.itempool += pool

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
    #   - Rando artifacts 4..12 (9 of 12): the patcher's init.lc sets
    #     `iNumRequiredArtifacts = 3`, so 9 starting artifacts means the
    #     player only needs to find 0 more by default. This is the
    #     Randovania starter setting; it makes the goal trivially
    #     reachable for a smoke seed without affecting the rest of the
    #     pool. Future Options.py work can expose artifact count.
    # TODO(post-v0.1): make this configurable via DreadOptions
    # (a `starting_inventory` Option mirroring Randovania's "starting items").
    DEFAULT_STARTING_ITEMS: dict[str, int] = {
        "ITEM_FLOOR_SLIDE": 1,
        "ITEM_SONAR": 1,
        "ITEM_WEAPON_MISSILE_MAX": 15,
        "ITEM_RANDO_ARTIFACT_4": 1,
        "ITEM_RANDO_ARTIFACT_5": 1,
        "ITEM_RANDO_ARTIFACT_6": 1,
        "ITEM_RANDO_ARTIFACT_7": 1,
        "ITEM_RANDO_ARTIFACT_8": 1,
        "ITEM_RANDO_ARTIFACT_9": 1,
        "ITEM_RANDO_ARTIFACT_10": 1,
        "ITEM_RANDO_ARTIFACT_11": 1,
        "ITEM_RANDO_ARTIFACT_12": 1,
    }

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

        return {
            "slot_name": slot_name,
            "seed_id": seed_id,
            "starting_area": int(self.options.starting_area.value),
            "include_boss_pickups": bool(self.options.include_boss_pickups.value),
            "starting_items": dict(self.DEFAULT_STARTING_ITEMS),
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
        return "Missile Tank"

    def generate_output(self, output_directory: str) -> None:
        """Write per-slot Dread placements JSON alongside the .archipelago.

        This is the legacy path consumed by
        ``scripts/seed_to_patcher_overrides.py``. The same payload is also
        embedded in ``fill_slot_data`` for in-client ``/patch``.
        """
        payload = self._build_placements_payload()
        seed_id = payload["seed_id"]
        slot_name = payload["slot_name"]
        # AP bundles anything we write into output_directory into the seed zip.
        out_path = (
            Path(output_directory)
            / f"AP_{seed_id}_P{self.player}_Dread_{slot_name}.json"
        )
        out_path.write_text(json.dumps(payload, indent=2))
