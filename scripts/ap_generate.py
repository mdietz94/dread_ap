"""Run Archipelago's Generate.py against our apworld.

Defaults to the sibling smo_archipelago's vendored Archipelago checkout
(dread_ap doesn't ship its own Archipelago vendor). Pass --ap-root to
override.

Generate.py's first action is ``ModuleUpdate.update()`` which iterates
through Archipelago's requirements.txt and pip-installs anything missing.
We short-circuit that to keep the dev venv minimal.

Usage:
    python scripts/ap_generate.py \\
        --player_files_path apworld/dread_archipelago/tests/seeds \\
        --outputpath apworld/dread_archipelago/tests/seeds/out
"""
from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_AP_ROOT = REPO.parent / "smo_archipelago" / "vendor" / "Archipelago"


def main(argv: list[str] | None = None) -> int:
    # Strip --ap-root from argv before handing the rest to Generate.py.
    # Resolve --player_files_path / --outputpath to absolute paths since
    # Generate.py runs from AP_ROOT (we chdir below), so relative paths
    # like "apworld/.../seeds" would resolve against the wrong root.
    ap_root = DEFAULT_AP_ROOT
    if argv is None:
        argv = sys.argv[1:]
    cleaned: list[str] = []
    path_flags = {"--player_files_path", "--outputpath"}
    i = 0
    while i < len(argv):
        if argv[i] == "--ap-root" and i + 1 < len(argv):
            ap_root = Path(argv[i + 1])
            i += 2
            continue
        if argv[i] in path_flags and i + 1 < len(argv):
            cleaned.append(argv[i])
            cleaned.append(str(Path(argv[i + 1]).resolve()))
            i += 2
            continue
        cleaned.append(argv[i])
        i += 1

    if not ap_root.exists():
        print(f"AP root not found: {ap_root}", file=sys.stderr)
        return 2

    os.chdir(ap_root)
    sys.path.insert(0, str(ap_root))

    import ModuleUpdate  # type: ignore[import-not-found]
    ModuleUpdate.update_ran = True

    sys.argv = [str(ap_root / "Generate.py")] + cleaned
    runpy.run_path(str(ap_root / "Generate.py"), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
