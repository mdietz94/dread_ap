"""Build an open-dread-rando input JSON from AP slot_data.

Thin wrapper around
``apworld/dread_archipelago/patcher_pipeline.merge_overrides`` — that
module is the single source of truth so both this CLI and the in-client
``/patch`` command share one implementation.

Usage:

    python scripts/build_patcher_json.py \\
        --template vendor/open-dread-rando/tests/test_files/patcher_files/starter_preset_patcher.json \\
        --ap-overrides path/to/dread_overrides.json \\
        --output build/dread_patcher_input.json

    python -m open_dread_rando \\
        --input-path /abs/path/to/dread/romfs \\
        --output-path /abs/path/to/output \\
        --input-json /abs/path/to/build/dread_patcher_input.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "apworld"))

from dread_archipelago.patcher_pipeline import _pickup_key, merge_overrides  # noqa: E402


def list_pickups(template_path: Path) -> int:
    template = json.loads(template_path.read_text())
    keys = []
    for pickup in template.get("pickups", []):
        key = _pickup_key(pickup)
        if key:
            keys.append(key)
    print(f"{len(keys)} pickup keys in {template_path.name}:")
    for k in sorted(keys):
        print(f"  {k}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Build open-dread-rando input JSON from AP slot data")
    parser.add_argument("--template", type=Path, required=True,
                        help="Path to a vanilla Randovania patcher JSON")
    parser.add_argument("--ap-overrides", type=Path,
                        help="Path to the AP-side overrides JSON")
    parser.add_argument("--output", type=Path,
                        help="Where to write the merged patcher input")
    parser.add_argument("--list-pickups", action="store_true",
                        help="Print all pickup keys in the template and exit")
    args = parser.parse_args(argv)

    if args.list_pickups:
        return list_pickups(args.template)

    if not args.ap_overrides or not args.output:
        parser.error("--ap-overrides and --output are required unless --list-pickups is given")

    template = json.loads(args.template.read_text())
    overrides = json.loads(args.ap_overrides.read_text())
    try:
        merged = merge_overrides(template, overrides)
    except ValueError as exc:
        raise SystemExit(f"build_patcher_json: {exc}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(merged, indent=2))
    print(f"wrote {args.output} ({args.output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
