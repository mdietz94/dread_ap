"""Append event items + event locations to apworld data tables.

Reads ``apworld/dread_archipelago/data/compiled_rules.json`` (produced
by extract_dread_rules.py) and APPENDS one event item + one event
location per event into ``items.json`` / ``locations.json``.

Idempotent: if an event item / location with the same name already
exists, it's left alone (so re-running this script after a recompile
adds only NEW events without shifting existing AP IDs).

AP IDs come from the compiler's stable assignment
(``e["item_ap_id"]`` / ``e["location_ap_id"]``) and must stay disjoint
from the existing pickup ranges — see
``docs/randovania-logic-port-m2plumbing.md`` §"AP-ID stability".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "apworld" / "dread_archipelago" / "data"

# Hanubia is the in-game name for the s080_shipyard scenario where the
# Ship event lives. Itorash events use s090_skybase, which has no
# pickup locations in the starter preset (per the M2 pitfall doc), so
# event locations there don't gate any actor pickups — they just keep
# the data tables consistent.
REGION_TO_SCENARIO = {
    "Artaria":  "s010_cave",
    "Cataris":  "s020_magma",
    "Dairon":   "s030_baselab",
    "Burenia":  "s040_aqua",
    "Ghavoran": "s050_forest",
    "Elun":     "s060_quarantine",
    "Ferenia":  "s070_basesanc",
    "Hanubia":  "s080_shipyard",
    "Itorash":  "s090_skybase",
}


def main() -> int:
    compiled = json.loads((DATA_DIR / "compiled_rules.json").read_text())
    items = json.loads((DATA_DIR / "items.json").read_text())
    locations = json.loads((DATA_DIR / "locations.json").read_text())

    existing_item_names = {it["name"] for it in items}
    existing_loc_names = {l["name"] for l in locations}

    new_items = 0
    new_locs = 0
    for event in compiled["events"]:
        item_name = f"Event: {event['name']}"
        loc_name = f"Event: {event['name']}"

        if item_name not in existing_item_names:
            items.append({
                "name": item_name,
                "ap_id": event["item_ap_id"],
                "patcher_item_id": "",
                "quantity": 1,
                "classification": "progression",
            })
            new_items += 1

        if loc_name not in existing_loc_names:
            # Fold Itorash events under Hanubia: regions.json only has 8
            # regions (Itorash has no actor pickups in the starter
            # preset; see M2 pitfall doc), and synthetic event locations
            # need to live in a region we already know about so the
            # current star-topology Regions.py picks them up.
            region = event["region"] or "Hanubia"
            if region == "Itorash":
                region = "Hanubia"
            scenario = REGION_TO_SCENARIO.get(region, "")
            locations.append({
                "name": loc_name,
                "ap_id": event["location_ap_id"],
                "region": region,
                "scenario": scenario,
                "actor": "",
                "pickup_type": "event",
                "vanilla_item": item_name,
            })
            new_locs += 1

    (DATA_DIR / "items.json").write_text(json.dumps(items, indent=2) + "\n")
    (DATA_DIR / "locations.json").write_text(json.dumps(locations, indent=2) + "\n")
    print(f"appended {new_items} event items, {new_locs} event locations")
    print(f"total items: {len(items)}, total locations: {len(locations)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
