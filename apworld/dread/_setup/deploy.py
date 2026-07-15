"""Copy build outputs to the user's chosen deploy target.

Two targets exist:

  - **Real Switch (SD card)**: files land at
    `<drive>:/atmosphere/contents/010093801237c000/{exefs,romfs}/`
    The same layout `switch-mod/CMakeLists.txt`'s `install` target
    produces in `sd-overlay/`. The wizard probes for removable drives
    that already contain an `atmosphere/` directory (signal that this
    drive is currently a Switch SD card) and offers them as picks;
    the user can also browse to any path.

  - **Ryujinx (emulator)**: files land at
    `%APPDATA%/Ryujinx/mods/contents/010093801237c000/DreadRandovania/exefs/`
    (for `subsdk9` + `main.npdm`)
    and
    `%APPDATA%/Ryujinx/sdcard/atmosphere/contents/010093801237c000/romfs/`
    (for `ap_config.json`).
    Identical paths to the existing `-DRYU_PATH=...` post-build hook
    in `switch-mod/CMakeLists.txt`, so this is the well-known dev
    target.

Both deploy paths take the same `build_dir` argument (the
`%APPDATA%/dread_ap/build/cmake/` produced by `build.py`) so
switching between targets after a build doesn't require a rebuild —
the bytes are identical, only the destination differs.
"""

from __future__ import annotations

import json
import os
import shutil
import string
import sys
from dataclasses import dataclass
from pathlib import Path

# Dread's Atmosphere title id — fixed across retail patches (1.0.0 and
# 2.1.0 share the same title id; only the patch NCAs differ).
DREAD_TITLE_ID = "010093801237c000"
# Module name under Ryujinx's mods/contents — matches the directory the
# CMakeLists.txt `RYU_PATH` post-build hook writes into.
RYU_MOD_NAME = "DreadRandovania"


@dataclass
class DeployResult:
    """Per-deploy summary returned to the wizard for the "Copied N files
    to ..." summary line. `files` is in source→dest tuple form so the
    wizard can render a small table if it wants."""
    ok: bool
    target: str           # human-readable target description ("SD card at D:\\", "Ryujinx")
    files: list[tuple[Path, Path]]
    error: str = ""


def detect_sd_candidates() -> list[Path]:
    """Return all currently-mounted drive roots that look like a Switch
    SD card (i.e. have an `atmosphere/` directory at the root).

    Windows-only for v1 (the plan scopes Linux/Mac as a follow-up). On
    non-Windows we return [] — the user can still browse-to-path on the
    wizard's Deploy page.
    """
    if sys.platform != "win32":
        return []
    candidates: list[Path] = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:/")
        if not root.exists():
            continue
        atmo = root / "atmosphere"
        if atmo.is_dir():
            candidates.append(root)
    return candidates


def _ryujinx_candidate_roots() -> list[Path]:
    """Platform-ordered list of directories where Ryujinx stores its data.

    The wizard only uses the FIRST that exists as an auto-detect hint; the
    user can always override via "Browse for Ryujinx folder". Covering all
    platforms (not just Windows) matters because a blank hint made the wizard
    fall back to ``Path(".")`` — a relative, non-writable cwd — and every
    Ryujinx deploy failed with ``PermissionError: 'mods'`` until the user
    hand-typed the folder.
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return [Path(appdata) / "Ryujinx"] if appdata else []

    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "Ryujinx"]

    # Linux (and other POSIX): the portable/native install honors
    # $XDG_CONFIG_HOME (default ~/.config); the Flatpak sandboxes its own
    # config under ~/.var/app. Newer forks (Ryubing) keep the "Ryujinx" leaf.
    roots: list[Path] = []
    xdg = os.environ.get("XDG_CONFIG_HOME")
    roots.append(Path(xdg) / "Ryujinx" if xdg else home / ".config" / "Ryujinx")
    roots.append(home / ".var" / "app" / "org.ryujinx.Ryujinx" / "config" / "Ryujinx")
    return roots


def detect_ryujinx_path() -> Path | None:
    """Return the first existing Ryujinx data dir for this platform, else None.

    Matches the location Ryujinx itself defaults to: ``%APPDATA%/Ryujinx`` on
    Windows, ``~/Library/Application Support/Ryujinx`` on macOS, and
    ``$XDG_CONFIG_HOME/Ryujinx`` (≈ ``~/.config/Ryujinx``) or the Flatpak
    config dir on Linux. The wizard's Deploy page also lets the user browse to
    a non-default install via "Browse for Ryujinx folder"; this is just the
    auto-detect hint.
    """
    for p in _ryujinx_candidate_roots():
        if p.is_dir():
            return p
    return None


def _sd_layout(sd_root: Path) -> dict[str, Path]:
    """Destination paths for the three artifacts on a Switch SD card."""
    base = sd_root / "atmosphere" / "contents" / DREAD_TITLE_ID
    return {
        "subsdk9": base / "exefs" / "subsdk9",
        "main.npdm": base / "exefs" / "main.npdm",
        "ap_config.json": base / "romfs" / "ap_config.json",
    }


def _ryujinx_layout(ryujinx_root: Path) -> dict[str, Path]:
    """Destination paths under a Ryujinx install root."""
    mods = ryujinx_root / "mods" / "contents" / DREAD_TITLE_ID / RYU_MOD_NAME
    sd = ryujinx_root / "sdcard" / "atmosphere" / "contents" / DREAD_TITLE_ID
    return {
        "subsdk9": mods / "exefs" / "subsdk9",
        "main.npdm": mods / "exefs" / "main.npdm",
        "ap_config.json": sd / "romfs" / "ap_config.json",
    }


class DeployCopyError(OSError):
    """`_copy_files` raises this in place of a bare OSError so the wizard's
    error handler can display the source/destination context the user
    needs to diagnose the failure (and so the OSError catch in the deploy
    wrappers can dispatch on it without losing context)."""


def _copy_files(
    sources: dict[str, Path],
    dests: dict[str, Path],
) -> list[tuple[Path, Path]]:
    """Copy each (source, dest) pair, creating parent dirs.

    Returns the list of (source, dest) actually copied for the wizard
    summary. Raises `DeployCopyError` on any IO error, with the failing
    pair embedded in the message — `shutil.copy2`'s default OSError
    sometimes elides the destination path, which is the most useful
    diagnostic on a Switch SD card deploy (wrong drive picked, drive
    yanked mid-copy, AV write block).

    After a successful copy each destination's size is asserted equal
    to the source's size — `shutil.copy2` doesn't fsync and some Windows
    file system filters can return early before the bytes have landed.
    A size mismatch is treated as a copy failure so the wizard reports
    the partial write instead of marking deploy "complete".
    """
    copied: list[tuple[Path, Path]] = []
    for key, src in sources.items():
        dst = dests[key]
        try:
            src_size = src.stat().st_size
        except OSError as e:
            raise DeployCopyError(
                f"Source file unreadable before copy: {src} ({e}). "
                f"Re-run the Build step to regenerate it."
            ) from e
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise DeployCopyError(
                f"Could not create destination directory {dst.parent} "
                f"for {key}: {e}. Check that the target drive is "
                f"writable and has free space."
            ) from e
        try:
            shutil.copy2(src, dst)
        except OSError as e:
            raise DeployCopyError(
                f"Failed to copy {src.name} to {dst}: {e}. "
                f"If this is an SD card, check it's still inserted and "
                f"not write-protected; if Ryujinx, check it's not "
                f"running with the mod file locked."
            ) from e
        try:
            actual = dst.stat().st_size
        except OSError as e:
            raise DeployCopyError(
                f"Copied {src.name} to {dst} but couldn't stat the "
                f"result: {e}. The destination may have been deleted "
                f"or the drive disconnected during the copy."
            ) from e
        if actual != src_size:
            raise DeployCopyError(
                f"Partial write: {src.name} is {src_size} bytes but "
                f"{dst} is {actual} bytes. The drive ran out of space "
                f"mid-copy or was disconnected. Free space (or "
                f"reconnect the SD card) and re-run Deploy."
            )
        copied.append((src, dst))
    return copied


def _resolve_bridge_host(explicit: str | None) -> str:
    """Pick the LAN IP the Switch should sweep to find this PC.

    `explicit` (when the wizard passes one) wins; otherwise we auto-detect
    via the same `detect_lan_ip()` the build-time bake uses. Lazy-imported so
    `deploy` stays a pure-stdlib module that loads without the client package.
    """
    if explicit:
        return explicit
    from ..client.net_util import detect_lan_ip
    return detect_lan_ip()


def _write_ap_config(dest: Path, bridge_host: str) -> None:
    """Write `rom:/ap_config.json` = {"bridge_host": <ip>} into the romfs
    layer. The sysmodule reads this at connect time (resolveBridge) as the
    SOLE /24 sweep seed — there is no compile-time bake, so one prebuilt
    `subsdk9` targets any LAN. Raises `DeployCopyError` on a short write —
    same partial-write guard `_copy_files` applies to the binaries.
    """
    data = json.dumps({"bridge_host": bridge_host}, separators=(",", ":")).encode("utf-8")
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    except OSError as e:
        raise DeployCopyError(
            f"Failed to write {dest}: {e}. Check the target drive is "
            f"writable and has free space."
        ) from e
    if dest.stat().st_size != len(data):
        raise DeployCopyError(
            f"Partial write: {dest} is {dest.stat().st_size} bytes but "
            f"ap_config.json is {len(data)} bytes. Free space (or reconnect "
            f"the SD card) and re-run Deploy."
        )


def deploy_to_sd(
    sd_root: Path,
    build_outputs: dict[str, Path],
    bridge_host: str | None = None,
) -> DeployResult:
    """Copy build outputs to a Switch SD card root.

    `sd_root` should be the drive root (e.g. `D:/`), not a deeper path.
    Caller validates the path; we just lay the files out under it.
    """
    try:
        dests = _sd_layout(sd_root)
        copied = _copy_files(build_outputs, dests)
        _write_ap_config(dests["ap_config.json"], _resolve_bridge_host(bridge_host))
        copied.append((dests["ap_config.json"], dests["ap_config.json"]))
        return DeployResult(
            ok=True,
            target=f"SD card at {sd_root}",
            files=copied,
        )
    except (OSError, PermissionError) as e:
        # Preserve the underlying exception class name (PermissionError,
        # FileNotFoundError, OSError) alongside the DeployCopyError
        # context so the user sees both "what kind of OS failure" and
        # "which copy step it was".
        cause = e.__cause__ if isinstance(e, DeployCopyError) and e.__cause__ else e
        return DeployResult(
            ok=False,
            target=f"SD card at {sd_root}",
            files=[],
            error=f"{type(cause).__name__}: {e}",
        )


def deploy_to_ryujinx(
    ryujinx_root: Path,
    build_outputs: dict[str, Path],
    bridge_host: str | None = None,
) -> DeployResult:
    """Copy build outputs to a Ryujinx install root."""
    try:
        dests = _ryujinx_layout(ryujinx_root)
        copied = _copy_files(build_outputs, dests)
        _write_ap_config(dests["ap_config.json"], _resolve_bridge_host(bridge_host))
        copied.append((dests["ap_config.json"], dests["ap_config.json"]))
        return DeployResult(
            ok=True,
            target=f"Ryujinx at {ryujinx_root}",
            files=copied,
        )
    except (OSError, PermissionError) as e:
        cause = e.__cause__ if isinstance(e, DeployCopyError) and e.__cause__ else e
        return DeployResult(
            ok=False,
            target=f"Ryujinx at {ryujinx_root}",
            files=[],
            error=f"{type(cause).__name__}: {e}",
        )


def deploy_to_custom_folder(
    custom_root: Path,
    build_outputs: dict[str, Path],
    bridge_host: str | None = None,
) -> DeployResult:
    """Copy build outputs to an arbitrary folder using the SD-card layout.

    Useful when the user wants to manage SD-card sync themselves —
    e.g. UMS later, or copy via DBI / Goldleaf, or stage on a Dropbox
    folder before a manual transfer. We write the same `atmosphere/
    contents/010093801237c000/{exefs,romfs}/` subtree the SD-card
    deploy produces, just under the user's chosen folder root, so they
    can drop the entire subtree onto a Switch SD card and have it work
    without any path-rewriting.
    """
    try:
        dests = _sd_layout(custom_root)
        copied = _copy_files(build_outputs, dests)
        _write_ap_config(dests["ap_config.json"], _resolve_bridge_host(bridge_host))
        copied.append((dests["ap_config.json"], dests["ap_config.json"]))
        return DeployResult(
            ok=True,
            target=f"Custom folder at {custom_root}",
            files=copied,
        )
    except (OSError, PermissionError) as e:
        cause = e.__cause__ if isinstance(e, DeployCopyError) and e.__cause__ else e
        return DeployResult(
            ok=False,
            target=f"Custom folder at {custom_root}",
            files=[],
            error=f"{type(cause).__name__}: {e}",
        )
