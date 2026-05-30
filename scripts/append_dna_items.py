"""Append the 12 Metroid DNA goal items to the apworld item table.

Each ``Metroid DNA k`` (k = 1..12) maps to the patcher resource
``ITEM_RANDO_ARTIFACT_k``, so it flows through the normal own-item path
(World._build_placements_payload → patcher pickup resources) and the
client datapackage / receive path with NO special-casing. The
``RequiredArtifacts`` option decides how many (the first N) enter the
pool; the rest of the 12 are granted as starting items by World.py.

Run order matters: this must run AFTER scripts/append_event_data.py so the
DNA IDs land after the event range. IDs are assigned deterministically as
``max(non-DNA item ap_id) + k`` and the script is idempotent (an existing
``Metroid DNA k`` is left untouched), so re-running never shifts IDs.

The compiler (extract_dread_rules.py) excludes ``Metroid DNA*`` from its
event-ID-base computation, so appending these never renumbers events.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "apworld" / "dread" / "data"

DNA_COUNT = 12


def main() -> int:
    items = json.loads((DATA_DIR / "items.json").read_text())
    by_name = {it["name"]: it for it in items}

    base = max(
        it["ap_id"] for it in items if not it["name"].startswith("Metroid DNA")
    ) + 1

    added = 0
    for k in range(1, DNA_COUNT + 1):
        name = f"Metroid DNA {k}"
        if name in by_name:
            continue
        items.append({
            "name": name,
            "ap_id": base + (k - 1),
            "patcher_item_id": f"ITEM_RANDO_ARTIFACT_{k}",
            "quantity": 1,
            # DNA is added explicitly by World.create_items (exactly N copies
            # per RequiredArtifacts), so pool_count from the data table is
            # unused. 0 documents that intent.
            "pool_count": 0,
            "classification": "progression",
        })
        added += 1

    (DATA_DIR / "items.json").write_text(json.dumps(items, indent=2) + "\n")
    print(f"appended {added} Metroid DNA items (base ap_id {base}); total items {len(items)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
