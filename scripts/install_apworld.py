"""Install apworld/dread_archipelago/ into an Archipelago checkout.

Two modes:

  * ``--mode folder`` (default): copy the package into ``<AP_ROOT>/worlds/
    dread_archipelago/``. Lets the standard ``Path(__file__).parent /
    "data" / *.json`` data loads work, since the package lives on disk
    as files. Best for dev iteration.

  * ``--mode apworld``: zip into ``<AP_ROOT>/custom_worlds/
    dread_archipelago.apworld``. The .apworld layout is what end users
    install but JSON-via-Path data loads fail inside a zip — only use
    once Items/Locations/Rules switch to ``importlib.resources``.

Default target is the sibling smo_archipelago's vendored Archipelago
checkout (dread_ap doesn't ship its own vendor yet). Pass --ap-root to
point elsewhere.

Idempotent: re-running overwrites the destination.

Usage:
    python scripts/install_apworld.py                              # folder mode
    python scripts/install_apworld.py --mode apworld               # zip mode
    python scripts/install_apworld.py --ap-root path/to/Archipelago
"""
from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "apworld" / "dread_archipelago"
DEFAULT_AP_ROOT = REPO.parent / "smo_archipelago" / "vendor" / "Archipelago"

SKIP_NAMES = {"__pycache__", ".mypy_cache", ".ruff_cache",
              ".pytest_cache", "tests"}


def _should_skip(path: Path) -> bool:
    return any(part in SKIP_NAMES for part in path.parts)


def build_apworld_zip(src: Path, dst: Path) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    file_count = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src.rglob("*")):
            if _should_skip(path) or path.is_dir():
                continue
            arcname = path.relative_to(src.parent).as_posix()
            zf.write(path, arcname)
            file_count += 1
    return file_count


def install_folder(src: Path, dst: Path) -> int:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    file_count = 0
    for path in sorted(src.rglob("*")):
        if _should_skip(path):
            continue
        rel = path.relative_to(src)
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            file_count += 1
    return file_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ap-root",
        type=Path,
        default=DEFAULT_AP_ROOT,
        help="Path to Archipelago checkout",
    )
    parser.add_argument(
        "--mode",
        choices=("folder", "apworld"),
        default="folder",
        help="folder = install under worlds/; apworld = zip into custom_worlds/",
    )
    parser.add_argument(
        "--name",
        default="dread_archipelago",
        help="package / file name (folder mode uses worlds/<name>/; "
             "apworld mode uses custom_worlds/<name>.apworld)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write directly to this path instead of <ap-root>/custom_worlds/. "
             "Bypasses the AP-root check entirely — used by CI to build the "
             "release artifact without needing an Archipelago checkout. "
             "Only valid with --mode apworld.",
    )
    args = parser.parse_args(argv)

    if args.output is not None:
        if args.mode != "apworld":
            print("--output only valid with --mode apworld", file=sys.stderr)
            return 2
        args.output.parent.mkdir(parents=True, exist_ok=True)
        n = build_apworld_zip(SRC, args.output)
        print(f"wrote {args.output} ({n} files)")
        return 0

    if not args.ap_root.exists():
        print(f"AP root not found: {args.ap_root}", file=sys.stderr)
        print("Pass --ap-root pointing at your Archipelago checkout, or use "
              "--output to write to a direct path (CI mode).",
              file=sys.stderr)
        return 2

    if args.mode == "folder":
        dst = args.ap_root / "worlds" / args.name
        n = install_folder(SRC, dst)
    else:
        dst = args.ap_root / "custom_worlds" / f"{args.name}.apworld"
        n = build_apworld_zip(SRC, dst)
    print(f"wrote {dst} ({n} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
