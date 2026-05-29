"""Inject AP telemetry Lua into the deployed mod's init.lc.

Thin wrapper around
``apworld/dread_archipelago/patcher_pipeline.inject_telemetry_into_init_lc``
— that module is the single source of truth so both this CLI and the
in-client ``/patch`` command share one implementation.

Run after the patcher and the overlay step. Idempotent (re-running
replaces the appended block rather than duplicating it).

Usage:
    python scripts/inject_ap_telemetry.py \\
        --init-lc "%APPDATA%/Ryujinx/mods/contents/010093801237c000/DreadRandovania/romfs/system/scripts/init.lc" \\
        --locations apworld/dread_archipelago/data/locations.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "apworld"))

from dread_archipelago.patcher_pipeline import inject_telemetry_into_init_lc  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--init-lc", type=Path, required=True,
                   help="Path to the deployed init.lc (under romfs/system/scripts/)")
    p.add_argument("--locations", type=Path, required=True,
                   help="Path to apworld/dread_archipelago/data/locations.json")
    args = p.parse_args(argv)

    if not args.init_lc.exists():
        raise SystemExit(f"init.lc not found at {args.init_lc}")
    if not args.locations.exists():
        raise SystemExit(f"locations.json not found at {args.locations}")

    locations = json.loads(args.locations.read_text())
    inject_telemetry_into_init_lc(args.init_lc, locations)
    n_actors = sum(1 for loc in locations if loc.get("pickup_index") is not None)
    print(f"injected AP telemetry block into {args.init_lc}")
    print(f"  actor->pickup_index entries: {n_actors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
