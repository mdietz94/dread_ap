"""Locate the vendored ``open-dread-rando`` source for the patcher subprocess.

The patcher source ships in three forms depending on how the apworld was
deployed; this module is the single resolver for "where does
``import open_dread_rando`` find its source":

1. **Source tree (dev)** — pinned git submodule at
   ``<repo>/vendor/open-dread-rando/src/open_dread_rando/``.
2. **Folder install** — ``scripts/install_apworld.py --mode folder`` copies the
   patcher into ``<apworld>/_vendored_patcher/open_dread_rando/`` next to the
   apworld files.
3. **Zip install (.apworld)** — same layout as folder install, but inside the
   zip. We extract the bundled tree to a per-zip-mtime cache directory under
   the user's home cache the first time it's needed (the patcher subprocess
   cannot ``-m`` a module that lives inside a zip path).

The patcher subprocess receives ``PYTHONPATH`` pointed at the directory we
return, so its plain ``import open_dread_rando`` resolves to the bundled
copy with no ``pip install`` step.

Runtime dependencies of open-dread-rando (``mercury-engine-data-structures``,
``jsonschema``, ``json-delta``, ``open-dread-rando-exlaunch``) are still pip
packages and must be installed in the target Python. The wizard installs them.

If none of the three sources are present — the user is running from a clone
with no submodule init AND the apworld was assembled without the bundling
step — :func:`vendored_open_dread_rando_src` returns ``None``.
"""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path
from typing import Optional

_DREAD_PKG = Path(__file__).resolve().parent             # apworld/dread/
_APWORLD_DIR = _DREAD_PKG.parent                          # apworld/
_REPO_ROOT = _APWORLD_DIR.parent                          # repo root

# Relative path inside the apworld where install_apworld.py copies the
# vendored patcher source. Kept in sync with ``BUNDLED_PATCHER_REL`` in
# scripts/install_apworld.py.
_BUNDLED_REL = Path("_vendored_patcher")
_BUNDLED_PKG = _BUNDLED_REL / "open_dread_rando"


def _hosting_zip() -> Optional[Path]:
    """Return the ``.apworld`` / ``.zip`` file we were imported from, or
    ``None`` if this module lives on a real filesystem directory."""
    for ancestor in Path(__file__).resolve().parents:
        if ancestor.suffix in (".apworld", ".zip") and ancestor.is_file():
            return ancestor
    return None


def _extract_bundled_from_zip(zip_path: Path) -> Optional[Path]:
    """Extract ``dread/_vendored_patcher/`` from the hosting apworld zip to a
    per-zip-mtime cache directory under the user's home, then return the
    parent dir of ``open_dread_rando`` (the directory to put on PYTHONPATH).

    Cache key is ``sha1(zip_path | mtime | size)``; bumping the apworld
    invalidates it automatically. The previous cache dir is left in place —
    cheap to leave; cheap to rm-rf if the user wants — and re-extraction
    skips when the marker ``__init__.py`` already exists.

    Returns ``None`` if extraction failed (zip damaged, no bundled tree, etc.).
    """
    try:
        st = zip_path.stat()
    except OSError:
        return None
    digest = hashlib.sha1(
        f"{zip_path}|{int(st.st_mtime)}|{st.st_size}".encode("utf-8")
    ).hexdigest()[:16]
    cache_root = Path.home() / ".cache" / "dread_ap" / "vendored_patcher" / digest
    marker = cache_root / "open_dread_rando" / "__init__.py"
    if marker.is_file():
        return cache_root

    # Locate the bundled tree inside the zip. install_apworld.py writes
    # entries under ``dread/_vendored_patcher/`` (the ``dread/`` prefix is
    # the apworld package dir name); accept any ``<pkg>/_vendored_patcher/``
    # prefix so a rename of the apworld dir doesn't break extraction.
    prefix_tail = "/_vendored_patcher/"
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            matching_prefix: Optional[str] = None
            for name in names:
                idx = name.find(prefix_tail)
                if idx > 0:
                    matching_prefix = name[: idx + len(prefix_tail)]
                    break
            if matching_prefix is None:
                return None
            cache_root.mkdir(parents=True, exist_ok=True)
            for name in names:
                if not name.startswith(matching_prefix) or name.endswith("/"):
                    continue
                rel = name[len(matching_prefix):]
                if not rel:
                    continue
                target = cache_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(name) as fsrc, open(target, "wb") as fdst:
                    fdst.write(fsrc.read())
    except (OSError, zipfile.BadZipFile):
        return None
    return cache_root if marker.is_file() else None


def vendored_open_dread_rando_src() -> Optional[Path]:
    """Directory to prepend to ``PYTHONPATH`` so ``import open_dread_rando``
    resolves to the vendored copy. Checks three locations in order: the
    source-tree submodule, the apworld's bundled ``_vendored_patcher/``
    folder, and the bundled tree inside a hosting ``.apworld`` zip (extracted
    to cache on demand). Returns ``None`` if no source is found."""
    # 1. Source-tree submodule (dev).
    repo_src = _REPO_ROOT / "vendor" / "open-dread-rando" / "src"
    if (repo_src / "open_dread_rando").is_dir():
        return repo_src
    # 2. Bundled folder install — install_apworld.py copied the package in.
    bundled = _DREAD_PKG / _BUNDLED_REL
    if (bundled / "open_dread_rando").is_dir():
        return bundled
    # 3. Bundled inside a zipped apworld — extract once to a cache dir.
    zip_path = _hosting_zip()
    if zip_path is not None:
        return _extract_bundled_from_zip(zip_path)
    return None


def vendor_unavailable_diagnostic() -> str:
    """One-line user-readable explanation of why
    :func:`vendored_open_dread_rando_src` returned ``None``. Differs between
    a dev clone (submodule init missing) and an installed apworld (bundling
    step skipped or zip extract failed).

    Callers should embed this into a higher-level error message; the string
    has no trailing period."""
    if (_REPO_ROOT / "vendor").is_dir():
        return (
            "the vendored open-dread-rando submodule isn't checked out — run "
            "`git submodule update --init vendor/open-dread-rando`"
        )
    return (
        "the bundled open-dread-rando source wasn't found in this apworld — "
        "re-install the apworld; if you assembled it yourself, init the "
        "submodule then re-run `python scripts/install_apworld.py`"
    )


# Runtime deps the patcher Python must have pip-installed. open-dread-rando
# itself is vendored, so it's not in this list. Kept in sync with
# vendor/open-dread-rando/pyproject.toml [project.dependencies].
PATCHER_RUNTIME_DEPS: tuple[str, ...] = (
    "mercury-engine-data-structures",
    "jsonschema",
    "json-delta",
    "open-dread-rando-exlaunch",
)
