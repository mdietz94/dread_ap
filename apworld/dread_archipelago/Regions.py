"""Region wiring.

M2 plumbing still uses a star topology: every region is directly
reachable from "Menu" with no entrance requirements. Per-pickup access
rules (in Rules.py / compiled_rules.json) already capture the
within-area item requirements that matter for solver feasibility,
which is enough for single-player generation. Real cross-region
adjacency derived from Randovania's dock-weakness graph is Gate B of
the M2 plumbing milestone (see docs/randovania-logic-port-m2plumbing.md).
"""
from __future__ import annotations

from BaseClasses import Region

from .Items import DreadItem
from .Locations import DreadLocation, locations_in_region
from ._data_loader import load_json


def _load_region_names() -> list[str]:
    return [entry["name"] for entry in load_json("regions.json")]


region_names: list[str] = _load_region_names()


def create_regions(world) -> None:
    multiworld = world.multiworld
    player = world.player

    menu = Region("Menu", player, multiworld)
    multiworld.regions.append(menu)

    for name in region_names:
        region = Region(name, player, multiworld)
        for loc in locations_in_region(name):
            ap_loc = DreadLocation(player, loc.name, loc.ap_id, region)
            region.locations.append(ap_loc)
        multiworld.regions.append(region)
        menu.connect(region, name)
