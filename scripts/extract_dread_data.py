"""Extract apworld data tables from the vendored Randovania sources.

Inputs:
  * vendor/open-dread-rando/tests/test_files/patcher_files/starter_preset_patcher.json
    — the canonical 149-pickup layout (137 actor + 12 boss/EMMI/cutscene).
    We mine this for the (scenario, actor) tuples and their vanilla
    resources.
  * vendor/open-dread-rando/src/open_dread_rando/files/schema.json
    — defines the ITEM_* identifier space (cross-checked).

Outputs:
  * apworld/dread/data/items.json
  * apworld/dread/data/locations.json
  * apworld/dread/data/regions.json

ID derivation: mirrors smo_archipelago — polynomial hash over the seed
``"Metroid Dread" + "maxdietz"`` to pick a 16-bit base; per-row
sequential offsets from there. Stable across re-runs (same inputs ->
same IDs), but a future schema change (renaming a location, adding a
new pickup) would shift everything past that row, forcing seed regen.
That's a documented v0.1 trade-off.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "vendor" / "open-dread-rando" / "tests" / "test_files" / "patcher_files" / "starter_preset_patcher.json"
OUT_DIR = ROOT / "apworld" / "dread" / "data"

# scenario internal name -> AP-facing region display name.
SCENARIO_TO_REGION = {
    "s010_cave":       "Artaria",
    "s020_magma":      "Cataris",
    "s030_baselab":    "Dairon",
    "s040_aqua":       "Burenia",
    "s050_forest":     "Ghavoran",
    "s060_quarantine": "Elun",
    "s070_basesanc":   "Ferenia",
    "s080_shipyard":   "Hanubia",
    "s090_skybase":    "Itorash",
}

# Lifted from the Randovania pickup-database fetch + per-item quantities
# we observed in the vendored starter preset. Keys are AP-facing item
# names; values are (patcher_item_id, default_quantity, default_pool_count,
# classification).
#
# Fields:
#   * patcher_item_id     — open-dread-rando resource ID granted on pickup.
#   * default_quantity    — capacity-per-pickup (passed to the patcher as the
#                           resource's `quantity`). Vanilla values verified
#                           against starter_preset_patcher.json.
#   * default_pool_count  — how many copies of this item are in the AP pool
#                           by default. Mirrors Randovania starter counts:
#                           Energy Tank 8, Energy Part 16, Missile Tank 60,
#                           Missile+ Tank 12, Power Bomb Tank 13. Options in
#                           Options.py can override this at generation time.
#                           Unique progression items are 1 (one copy each).
# Classifications:
#   * "progression"               — unlocks new logic (logic-required).
#   * "progression_skip_balancing" — progression but exempt from balancing.
#   * "useful"                    — quality of life (non-logic capacity).
#   * "filler"                    — trivially safe to lose.
ITEM_TABLE: list[tuple[str, str, int, int, str]] = [
    # Progression core
    ("Morph Ball",          "ITEM_MORPH_BALL",          1, 1, "progression"),
    ("Bomb",                "ITEM_WEAPON_BOMB",         1, 1, "progression"),
    ("Cross Bomb",          "ITEM_WEAPON_LINE_BOMB",    1, 1, "progression"),
    # Main Power Bomb pickup grants the weapon + N starting PB capacity.
    # Vanilla Randovania starter is N=2 (verified in starter_preset_patcher.json).
    # World._build_placements_payload overrides this at runtime from the
    # StartingPowerBombs option.
    ("Power Bomb",          "ITEM_WEAPON_POWER_BOMB",   2, 1, "progression"),
    ("Charge Beam",         "ITEM_WEAPON_CHARGE_BEAM",  1, 1, "progression"),
    ("Wide Beam",           "ITEM_WEAPON_WIDE_BEAM",    1, 1, "progression"),
    ("Plasma Beam",         "ITEM_WEAPON_PLASMA_BEAM",  1, 1, "progression"),
    ("Wave Beam",           "ITEM_WEAPON_WAVE_BEAM",    1, 1, "progression"),
    ("Diffusion Beam",      "ITEM_WEAPON_DIFFUSION_BEAM", 1, 1, "progression"),
    ("Grapple Beam",        "ITEM_WEAPON_GRAPPLE_BEAM", 1, 1, "progression"),
    ("Ice Missile",         "ITEM_WEAPON_ICE_MISSILE",  1, 1, "progression"),
    ("Storm Missile",       "ITEM_MULTILOCKON",         1, 1, "progression"),
    ("Varia Suit",          "ITEM_VARIA_SUIT",          1, 1, "progression"),
    ("Gravity Suit",        "ITEM_GRAVITY_SUIT",        1, 1, "progression"),
    ("Phantom Cloak",       "ITEM_OPTIC_CAMOUFLAGE",    1, 1, "progression"),
    ("Flash Shift",         "ITEM_GHOST_AURA",          1, 1, "progression"),
    ("Pulse Radar",         "ITEM_SONAR",               1, 1, "progression"),
    ("Speed Booster",       "ITEM_SPEED_BOOSTER",       1, 1, "progression"),
    ("Spider Magnet",       "ITEM_MAGNET_GLOVE",        1, 1, "progression"),
    ("Spin Boost",          "ITEM_DOUBLE_JUMP",         1, 1, "progression"),
    ("Space Jump",          "ITEM_SPACE_JUMP",          1, 1, "progression"),
    ("Screw Attack",        "ITEM_SCREW_ATTACK",        1, 1, "progression"),
    ("Slide",               "ITEM_FLOOR_SLIDE",         1, 1, "progression"),
    # Capacity / utility items. Classification is informed by what compiled
    # rules ACTUALLY reference (verified by walking compiled_rules.json):
    #   - Energy Tank, Energy Part, Power Bomb Tank: 0 logic references.
    #     Rules only check the main Power Bomb item; tanks are pure QoL.
    #   - Missile Tank: 3634 refs, all amount=1. BUT Missile Tank is in
    #     BASE_STARTING_ITEMS (precollected), so the atom is satisfied from
    #     turn 0 — all 60 findable copies add zero logic value, hence useful.
    #   - Missile+ Tank: 336 refs, all amount=1, NOT precollected. The FIRST
    #     copy is logic-gating; the remaining 11 are pure ammo capacity. The
    #     mixed classification is handled in World.create_items via
    #     MIXED_CLASSIFICATION_FIRST_N (this row is "progression" — the World
    #     uses that for the first copy, useful for the rest).
    #   - Flash Shift Upgrade / Speed Booster Upgrade: rules want amount=2 but
    #     we only have 1 in pool (pre-existing logic-data quirk; pickups don't
    #     exist in the vanilla starter preset, AP places them ex nihilo). Left
    #     as progression — gen succeeds because the amount=2 atoms live in
    #     disjuncts with other paths.
    ("Energy Tank",         "ITEM_ENERGY_TANKS",        1, 8, "useful"),
    ("Missile+ Tank",       "ITEM_WEAPON_MISSILE_MAX",  10, 12, "progression"),
    ("Flash Shift Upgrade", "ITEM_UPGRADE_FLASH_SHIFT_CHAIN", 1, 1, "progression"),
    ("Speed Booster Upgrade","ITEM_UPGRADE_SPEED_BOOST_CHARGE", 1, 1, "progression"),
    ("Missile Tank",        "ITEM_WEAPON_MISSILE_MAX",  2, 60, "useful"),
    # Each Power Bomb Tank pickup grants +1 PB capacity (vanilla).
    ("Power Bomb Tank",     "ITEM_WEAPON_POWER_BOMB_MAX", 1, 13, "filler"),
    ("Energy Part",         "ITEM_LIFE_SHARDS",         1, 16, "filler"),
]


def _hash16(s: str) -> int:
    """Simple polynomial hash → 16-bit base. Stable and deterministic."""
    h = 0
    for ch in s:
        h = (h * 31 + ord(ch)) & 0xFFFF
    return h


def _resource_to_ap_name(resources: list[list[dict]]) -> str:
    """Best-effort: pick an AP-facing item name for a vanilla resource list.

    Vanilla pickups carry the original item directly; once we add
    progressive items this needs to be smarter."""
    if not resources or not resources[0]:
        return "Missile Tank"
    first = resources[0][0]
    pid = first.get("item_id", "")
    qty = int(first.get("quantity", 1))
    # Reverse-lookup against ITEM_TABLE
    for ap_name, patcher_id, default_qty, _pool, _cls in ITEM_TABLE:
        if patcher_id == pid and default_qty == qty:
            return ap_name
    for ap_name, patcher_id, _default_qty, _pool, _cls in ITEM_TABLE:
        if patcher_id == pid:
            return ap_name
    return "Missile Tank"  # last-resort filler


def _build_location_name(scenario: str, actor: str, pickup_type: str) -> str:
    region = SCENARIO_TO_REGION.get(scenario, scenario)
    if pickup_type == "actor":
        # Strip noisy prefixes — Item_/ItemSphere_/IT_/itemsphere_/item_
        compact = actor
        for pfx in ("ItemSphere_", "Item_", "IT_", "itemsphere_", "item_"):
            if compact.startswith(pfx):
                compact = compact[len(pfx):]
        return f"{region}: {compact}"
    return f"{region}: {actor} ({pickup_type})"


def extract(template_path: Path) -> tuple[list, list, list]:
    template = json.loads(template_path.read_text())
    pickups = template.get("pickups", [])

    items_seed = _hash16("Metroid Dread maxdietz items")
    locs_seed = _hash16("Metroid Dread maxdietz locations")

    items = []
    for offset, (name, patcher_id, qty, pool_count, cls) in enumerate(ITEM_TABLE):
        items.append({
            "name": name,
            "ap_id": items_seed + offset,
            "patcher_item_id": patcher_id,
            "quantity": qty,
            "pool_count": pool_count,
            "classification": cls,
        })

    locations = []
    for offset, pickup in enumerate(pickups):
        pt = pickup.get("pickup_type", "actor")
        if pt == "actor":
            pa = pickup["pickup_actor"]
            scenario = pa["scenario"]
            actor = pa["actor"]
        else:
            cb = pickup.get("pickup_lua_callback", {})
            scenario = cb.get("scenario") or "unknown"
            actor = cb.get("function") or pt
        region = SCENARIO_TO_REGION.get(scenario, scenario)
        ap_id = locs_seed + offset
        name = _build_location_name(scenario, actor, pt)
        vanilla_item = _resource_to_ap_name(pickup.get("resources", []))
        locations.append({
            "name": name,
            "ap_id": ap_id,
            "region": region,
            "scenario": scenario,
            "actor": actor,
            "pickup_type": pt,
            "vanilla_item": vanilla_item,
            # pickup_index = position in the patcher template's `pickups`
            # array. Used by the runtime wire to translate Switch-reported
            # collected-indices bitfields back to AP location_ids. Event
            # locations (appended by append_event_data.py) don't have one.
            "pickup_index": offset,
        })

    # Regions are just the set of distinct region names observed
    regions = []
    seen_regions = []
    for loc in locations:
        if loc["region"] not in seen_regions:
            seen_regions.append(loc["region"])
    for offset, name in enumerate(seen_regions):
        regions.append({
            "name": name,
            "scenario_ids": [s for s, r in SCENARIO_TO_REGION.items() if r == name],
        })

    return items, locations, regions


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", type=Path, default=TEMPLATE,
                        help=f"Patcher template to mine (default {TEMPLATE})")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR,
                        help=f"Output directory (default {OUT_DIR})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print summary; don't write files")
    args = parser.parse_args(argv)

    items, locations, regions = extract(args.template)
    print(f"items:     {len(items)}")
    print(f"locations: {len(locations)}")
    print(f"regions:   {len(regions)}")

    by_region: dict[str, int] = {}
    for loc in locations:
        by_region[loc["region"]] = by_region.get(loc["region"], 0) + 1
    print("locations by region:")
    for r, n in sorted(by_region.items()):
        print(f"  {r:12} {n}")

    by_class: dict[str, int] = {}
    for it in items:
        by_class[it["classification"]] = by_class.get(it["classification"], 0) + 1
    print(f"items by classification: {by_class}")

    if args.dry_run:
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "items.json").write_text(json.dumps(items, indent=2))
    (args.out_dir / "locations.json").write_text(json.dumps(locations, indent=2))
    (args.out_dir / "regions.json").write_text(json.dumps(regions, indent=2))
    print(f"wrote {args.out_dir}/items.json")
    print(f"wrote {args.out_dir}/locations.json")
    print(f"wrote {args.out_dir}/regions.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
