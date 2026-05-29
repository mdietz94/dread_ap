"""Fetch + pin Randovania's Dread logic JSON.

Pulls header.json + 9 area JSONs from a pinned upstream commit into
``.dread-cache/randovania-logic/``. The cache is gitignored (under the
existing ``.dread-cache/`` rule); the pinned commit is recorded so the
compiler emits it into ``compiled_rules.json`` for traceability.

Usage:
    python scripts/cache_randovania_logic.py             # idempotent re-fetch
    python scripts/cache_randovania_logic.py --force     # force re-download

The 10 files total ~7.2 MB and live in
randovania/games/dread/logic_database/ in upstream.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path


# Pin to a specific commit so the compiler output is reproducible. Bump by
# re-running `gh api repos/randovania/randovania/commits/main --jq .sha[0:12]`
# and updating; the compiler's pinned_commit field will follow.
PINNED_COMMIT = "3559136dc445fcceee1f05831e4334443e7d7640"

LOGIC_FILES = [
    "header.json",
    "Artaria.json",
    "Burenia.json",
    "Cataris.json",
    "Dairon.json",
    "Elun.json",
    "Ferenia.json",
    "Ghavoran.json",
    "Hanubia.json",
    "Itorash.json",
]

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / ".dread-cache" / "randovania-logic"
COMMIT_FILE = CACHE_DIR / "PINNED_COMMIT.txt"

BASE_URL = (
    "https://raw.githubusercontent.com/randovania/randovania/"
    "{commit}/randovania/games/dread/logic_database/{filename}"
)


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


def _file_sha1(path: Path) -> str:
    # Git-style blob sha so it could be matched against the API listing if
    # ever needed. Not strictly required for cache freshness.
    h = hashlib.sha1()
    data = path.read_bytes()
    h.update(f"blob {len(data)}\0".encode())
    h.update(data)
    return h.hexdigest()


def cache_all(*, force: bool = False) -> tuple[int, int]:
    """Fetch missing logic files. Returns (downloaded, skipped)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    pinned_marker_ok = (
        COMMIT_FILE.exists() and COMMIT_FILE.read_text().strip() == PINNED_COMMIT
    )
    downloaded = 0
    skipped = 0
    for fname in LOGIC_FILES:
        dest = CACHE_DIR / fname
        if dest.exists() and pinned_marker_ok and not force:
            skipped += 1
            continue
        url = BASE_URL.format(commit=PINNED_COMMIT, filename=fname)
        print(f"  downloading {fname} ...", flush=True)
        data = _download(url)
        dest.write_bytes(data)
        downloaded += 1
    COMMIT_FILE.write_text(PINNED_COMMIT + "\n")
    return downloaded, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-download even if files exist and pin matches",
    )
    args = parser.parse_args(argv)

    print(f"Caching Randovania logic_database @ {PINNED_COMMIT[:12]}")
    print(f"  destination: {CACHE_DIR}")
    downloaded, skipped = cache_all(force=args.force)
    print(f"Done. downloaded={downloaded} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
