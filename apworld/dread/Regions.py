"""Region wiring.

The forward resolver (scripts/extract_dread_rules.py) emits item-only,
globally-resolved per-pickup rules — the cross-region cost is baked into each
rule — so ``region_access`` is a plain STAR: ``Menu`` connects to every region
with a trivial rule, and all the real gating lives in the per-pickup access
rules (applied in Rules.py). Event locations are inlined away and skipped here.

``region_access`` is read from ``compiled_rules.json`` (keyed by AP region name,
selected by trick level) purely so a future non-star map would Just Work; today
every entry is Trivial. If no compiled rules are present (pre-bake dev env),
regions fall back to trivial connections too, so the apworld still loads.
"""
from __future__ import annotations

from BaseClasses import Region

from .Items import DreadItem
from .Locations import DreadLocation, locations_in_region
from ._data_loader import load_json
from .Rules import compile_to_lambda, load_compiled_rules

TRIVIAL_RULE = {"type": "trivial"}


def _load_region_names() -> list[str]:
    return [entry["name"] for entry in load_json("regions.json")]


region_names: list[str] = _load_region_names()


def create_regions(world) -> None:
    multiworld = world.multiworld
    player = world.player

    try:
        compiled = load_compiled_rules(int(world.options.trick_level.value))
        region_access = compiled.get("region_access", {})
    except FileNotFoundError:
        # No compiled rules — fall back to the old star topology so the
        # apworld still loads in a pre-bake dev environment.
        region_access = {}

    menu = Region("Menu", player, multiworld)
    multiworld.regions.append(menu)

    for name in region_names:
        region = Region(name, player, multiworld)
        for loc in locations_in_region(name):
            if loc.pickup_type == "event":
                # Events are inlined into the item-only rules; not AP locations.
                continue
            ap_loc = DreadLocation(player, loc.name, loc.ap_id, region)
            region.locations.append(ap_loc)
        multiworld.regions.append(region)

        rule_ast = region_access.get(name, TRIVIAL_RULE)
        if rule_ast == TRIVIAL_RULE:
            menu.connect(region, name)
        else:
            menu.connect(region, name, rule=compile_to_lambda(rule_ast, player))
