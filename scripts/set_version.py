"""Stamp the apworld's release version into the two files that carry it.

The apworld version lives in two places that must agree and must match the
git release tag (``vX.Y.Z``):

  * ``apworld/dread/archipelago.json`` — ``world_version`` (the manifest field
    Archipelago surfaces to the host/client).
  * ``apworld/dread/__init__.py`` — ``__version__``.

These drift because nothing tied them to the tag: releases are cut locally
(``gh release create vX.Y.Z``), and the two fields were bumped by hand on
separate cadences. This module is the single source of truth for writing them,
so the local release process can stamp both from one string.

Both a library (``set_version`` / ``read_versions``) and a CLI:

    python scripts/set_version.py 0.19.0     # accepts a leading 'v' too

Edits are targeted line rewrites (regex), so the JSON/py formatting around them
is preserved and the diff stays minimal.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "apworld" / "dread" / "archipelago.json"
INIT = REPO / "apworld" / "dread" / "__init__.py"

# MAJOR.MINOR.PATCH with an optional ``-`` pre-release suffix (e.g. 0.19.0,
# 1.2.3-rc1). The suffix must lead with '-', so a stray fourth segment
# ("1.2.3.4") is rejected rather than silently swallowed as a suffix. A leading
# 'v' is stripped by normalize_version before this runs.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")

_MANIFEST_RE = re.compile(r'("world_version"\s*:\s*")[^"]*(")')
_INIT_RE = re.compile(r'(__version__\s*=\s*")[^"]*(")')


def normalize_version(version: str) -> str:
    """Strip a leading ``v`` and validate ``MAJOR.MINOR.PATCH[-suffix]``.

    Accepts the same string you pass to ``git tag`` / ``gh release create``
    (``v0.19.0``) or the bare form (``0.19.0``); returns the bare form.
    """
    v = version.strip()
    if v.startswith("v"):
        v = v[1:]
    if not _VERSION_RE.match(v):
        raise ValueError(
            f"not a valid MAJOR.MINOR.PATCH version: {version!r} "
            "(expected e.g. 0.19.0 or v0.19.0)"
        )
    return v


def _replace_once(text: str, pattern: re.Pattern[str], version: str,
                  where: str) -> str:
    new, n = pattern.subn(rf"\g<1>{version}\g<2>", text)
    if n != 1:
        raise ValueError(
            f"expected exactly one version field to rewrite in {where}, "
            f"found {n}"
        )
    return new


def read_versions(manifest: Path = MANIFEST,
                  init: Path = INIT) -> tuple[str | None, str | None]:
    """Return ``(world_version, __version__)`` as currently on disk.

    Either element is ``None`` if its field can't be found (a malformed file);
    callers can compare the two for the drift check.
    """
    m = _MANIFEST_RE.search(manifest.read_text(encoding="utf-8"))
    i = _INIT_RE.search(init.read_text(encoding="utf-8"))
    return (m.group().split('"')[3] if m else None,
            i.group().split('"')[1] if i else None)


def set_version(version: str, manifest: Path = MANIFEST,
                init: Path = INIT) -> str:
    """Stamp ``version`` into both files. Returns the normalized version.

    Raises ``ValueError`` on a malformed version or if either file doesn't
    contain exactly one version field to rewrite.
    """
    v = normalize_version(version)

    manifest_text = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        _replace_once(manifest_text, _MANIFEST_RE, v, manifest.name),
        encoding="utf-8",
    )

    init_text = init.read_text(encoding="utf-8")
    init.write_text(
        _replace_once(init_text, _INIT_RE, v, init.name),
        encoding="utf-8",
    )
    return v


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "version",
        help="Release version to stamp, e.g. 0.19.0 (a leading 'v' is ok)",
    )
    args = parser.parse_args(argv)
    try:
        v = set_version(args.version)
    except ValueError as exc:
        print(f"set_version: {exc}", file=sys.stderr)
        return 2
    print(f"stamped world_version + __version__ = {v}")
    print(f"  {MANIFEST.relative_to(REPO)}")
    print(f"  {INIT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
