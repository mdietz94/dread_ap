"""Install apworld/dread/ into an Archipelago checkout.

Two modes:

  * ``--mode folder`` (default): copy the package into ``<AP_ROOT>/worlds/
    dread/``. Lets the standard ``Path(__file__).parent /
    "data" / *.json`` data loads work, since the package lives on disk
    as files. Best for dev iteration.

  * ``--mode apworld``: zip into ``<AP_ROOT>/custom_worlds/
    dread.apworld``. The .apworld layout is what end users
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
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "apworld" / "dread"
DEFAULT_AP_ROOT = REPO.parent / "smo_archipelago" / "vendor" / "Archipelago"

# Vendored open-dread-rando source — submodule. We copy this into the
# apworld at install time under `_vendored_patcher/open_dread_rando/` so the
# zip is self-contained; the patcher subprocess doesn't need
# `pip install open-dread-rando` to run.
VENDORED_PATCHER_PKG = (
    REPO / "vendor" / "open-dread-rando" / "src" / "open_dread_rando"
)
# Path inside the apworld where the vendored copy lives. Read by
# `apworld/dread/_vendor.py` at runtime.
BUNDLED_PATCHER_REL = Path("_vendored_patcher") / "open_dread_rando"

SKIP_NAMES = {"__pycache__", ".mypy_cache", ".ruff_cache",
              ".pytest_cache", "tests"}


def _should_skip(path: Path) -> bool:
    return any(part in SKIP_NAMES for part in path.parts)


def _iter_vendored_patcher() -> list[tuple[Path, Path]]:
    """List ``(absolute_source_path, relative_path_within_open_dread_rando)``
    for every file we ship inside the apworld's bundled patcher copy.

    Returns an empty list if the submodule isn't checked out; callers should
    treat that as a fatal install error (the apworld is useless without it).
    """
    if not VENDORED_PATCHER_PKG.is_dir():
        return []
    out: list[tuple[Path, Path]] = []
    for path in sorted(VENDORED_PATCHER_PKG.rglob("*")):
        if path.is_dir():
            continue
        if any(part in SKIP_NAMES for part in path.parts):
            continue
        out.append((path, path.relative_to(VENDORED_PATCHER_PKG)))
    return out


def _ensure_compiled_rules() -> None:
    """Regenerate the compiled rule artifacts if missing or stale.

    The JSONs are gitignored (each is 150-240k lines), so a fresh checkout
    has nothing on disk. Both folder and zip installs need them at the
    destination, so we regen before copying. Total cost is ~10s on a cold
    cache; warm runs (everything up-to-date) are a no-op via the mtime check.
    """
    data_dir = SRC / "data"
    extract = REPO / "scripts" / "extract_dread_rules.py"
    cache = REPO / ".dread-cache" / "randovania-logic"
    pinned = cache / "PINNED_COMMIT.txt"

    if not extract.exists():
        return  # caller is running install from a stripped-down tree (unlikely)

    if not cache.exists():
        sys.stderr.write(
            "\n.dread-cache/randovania-logic/ is missing — install_apworld "
            "cannot regenerate compiled rules. Populate the cache first "
            "(see docs/randovania-logic-port.md for the fetch step).\n"
        )
        return

    input_mtime = max(extract.stat().st_mtime,
                      pinned.stat().st_mtime if pinned.exists() else 0)

    # Single compiled file (v3): tricks are kept symbolic and resolved per-trick
    # at AP-generation time. ``--graph`` also emits logic_graph.json (the native
    # region-graph model — the default logic path, see graph_logic.py) in the
    # same bake, reusing the item-only victory.
    target = data_dir / "compiled_rules.json"
    graph_target = data_dir / "logic_graph.json"
    if (target.exists() and graph_target.exists()
            and target.stat().st_mtime >= input_mtime
            and graph_target.stat().st_mtime >= input_mtime):
        return

    print("regenerating compiled_rules.json + logic_graph.json "
          "(~10 minutes; two-pass resolver)...")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        [sys.executable, str(extract), "--all", "--graph",
         "--out", str(target), "--graph-out", str(graph_target)],
        cwd=str(REPO), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _, err = proc.communicate()
    if proc.returncode != 0:
        sys.stderr.write(
            f"regen of {target.name} failed:\n"
            f"{err.decode('utf-8', errors='replace')}\n")
        sys.exit(1)


def _check_vendored_patcher_or_die() -> list[tuple[Path, Path]]:
    """Verify the vendored open-dread-rando submodule is checked out and
    return its file list. Exits with a clear message if not — the
    apworld is non-functional without it (the patcher subprocess imports
    from `_vendored_patcher/`)."""
    vendored = _iter_vendored_patcher()
    if not vendored:
        sys.stderr.write(
            "\nvendor/open-dread-rando/ is empty — the apworld bundles the\n"
            "patcher source into _vendored_patcher/ and cannot ship without it.\n"
            "Initialize the submodule:\n"
            "    git submodule update --init vendor/open-dread-rando\n"
        )
        sys.exit(1)
    return vendored


def build_apworld_zip(src: Path, dst: Path) -> int:
    vendored = _check_vendored_patcher_or_die()
    dst.parent.mkdir(parents=True, exist_ok=True)
    file_count = 0
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src.rglob("*")):
            if _should_skip(path) or path.is_dir():
                continue
            arcname = path.relative_to(src.parent).as_posix()
            zf.write(path, arcname)
            file_count += 1
        # Bundle the vendored patcher so the .apworld is self-contained.
        bundled_base = Path(src.name) / BUNDLED_PATCHER_REL
        for source, rel in vendored:
            arcname = (bundled_base / rel).as_posix()
            zf.write(source, arcname)
            file_count += 1
    return file_count


def install_folder(src: Path, dst: Path) -> int:
    vendored = _check_vendored_patcher_or_die()
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
    # Bundle the vendored patcher so the install dir is self-contained.
    bundled_root = dst / BUNDLED_PATCHER_REL
    for source, rel in vendored:
        target = bundled_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
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
        default="dread",
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

    _ensure_compiled_rules()

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
