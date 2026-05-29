"""Convert AP-generated per-slot placements into the patcher overrides JSON.

Now a thin wrapper around
``apworld/dread_archipelago/patcher_pipeline.placements_to_overrides`` —
that module is the single source of truth so both this CLI and the
in-client ``/patch`` command share one implementation.

The pipeline:

    ap_generate.py  →  AP_<id>.zip
                              ↓
                       (placements JSON inside)
                              ↓
              seed_to_patcher_overrides.py  (this file)
                              ↓
                  build/dread_overrides.json
                              ↓
                    build_patcher_json.py
                              ↓
                  build/dread_patcher_input.json
                              ↓
                       open_dread_rando
                              ↓
                     Switch-ready RomFS

Usage:

    # Extract + convert from a seed zip
    python scripts/seed_to_patcher_overrides.py \\
        apworld/dread_archipelago/tests/seeds/out/AP_<id>.zip \\
        --slot Samus \\
        --output build/dread_overrides.json

    # Or feed an already-extracted placements JSON
    python scripts/seed_to_patcher_overrides.py \\
        --placements-json path/to/placements.json \\
        --output build/dread_overrides.json
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from typing import Any

# Make the apworld importable when running from the project root.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "apworld"))

from dread_archipelago.patcher_pipeline import (  # noqa: E402
    CROSS_SLOT_PLACEHOLDER,
    STARTING_AREA_INDEX_TO_LOCATION,
    placements_to_overrides,
    layout_uuid_from_seed as _layout_uuid_from_seed,
)


def find_placements_in_zip(zip_path: Path, slot_name: str) -> dict[str, Any]:
    """Locate the placements JSON for the given slot inside an AP seed zip."""
    with zipfile.ZipFile(zip_path) as zf:
        json_names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        for name in json_names:
            try:
                data = json.loads(zf.read(name).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            if data.get("slot_name") == slot_name and "placements" in data:
                return data
    raise SystemExit(
        f"no Dread placements JSON for slot {slot_name!r} in {zip_path.name}. "
        f"Available JSONs: {json_names}"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Convert AP-generated Dread placements to patcher overrides."
    )
    parser.add_argument("seed_zip", type=Path, nargs="?", default=None,
                        help="AP seed zip (e.g. AP_<id>.zip). Mutually exclusive with --placements-json.")
    parser.add_argument("--placements-json", type=Path, default=None,
                        help="Direct path to a placements JSON (skips zip extraction).")
    parser.add_argument("--slot", required=False,
                        help="Dread slot name (required with seed_zip; ignored with --placements-json).")
    parser.add_argument("--output", type=Path, required=True,
                        help="Where to write the overrides JSON.")
    parser.add_argument("--layout-uuid", default=None,
                        help="Override the derived layout_uuid (rarely needed; mainly for tests).")
    args = parser.parse_args(argv)

    if args.placements_json is None and args.seed_zip is None:
        parser.error("either seed_zip or --placements-json is required")
    if args.placements_json is not None and args.seed_zip is not None:
        parser.error("seed_zip and --placements-json are mutually exclusive")

    if args.placements_json is not None:
        placements = json.loads(args.placements_json.read_text())
    else:
        if not args.slot:
            parser.error("--slot is required when reading from a seed zip")
        placements = find_placements_in_zip(args.seed_zip, args.slot)

    overrides = placements_to_overrides(placements, layout_uuid=args.layout_uuid)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(overrides, indent=2))
    print(f"wrote {args.output} ({args.output.stat().st_size} bytes)")
    print(
        f"  pickups: {len(overrides['pickup_resources'])} resource overrides, "
        f"{len(overrides['pickup_captions'])} cross-slot captions"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
