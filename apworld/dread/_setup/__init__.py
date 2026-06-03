"""Setup wizard for the Dread Archipelago client.

The user types `/setup` in DreadClient → DreadClient's `/setup` handler
spawns `_setup.wizard.run_setup_wizard` in a new window via
`launch_subprocess`. The same flow handles first-time setup and re-runs
(apworld update, switching deploy targets, devkitPro re-install).

The wizard's job is to turn a fresh machine into one that can:
  - check out upstream open-dread-rando-exlaunch, apply our Ryujinx fix
    patch, and build a working `subsdk9` + `main.npdm` against devkitPro
  - record the user's vanilla Dread romfs location (1.0.0 or 2.1.0) so the
    per-seed patcher (run automatically at AP-connect time) knows where to
    overlay
  - deploy the sysmodule either to a real Switch's SD card or to Ryujinx's
    mods directory.

The packages here are organized so each step is independently testable:

  - `dreadap_file.py` — `.dreadap` JSON metadata read/write (slot-id payload
                       the AP generator writes; double-clicked file launches
                       DreadClient with --name + --connect prefilled)
  - `net.py`         — LAN-IP autodetect (unused stub on dread; kept for
                       wizard import compatibility)
  - `prereqs.py`     — detect devkitPro, Python 3.12, open_dread_rando
  - `installers.py`  — minimal winget wrapper for Python 3.12 auto-install;
                       devkitPro is detection-only (their Windows installer
                       is interactive Inno Setup; a link points the user at
                       devkitpro.org and the wizard re-checks on retry).
  - `build.py`       — clone the open-dread-rando-exlaunch-bridge fork
                       (ours, networking-rewritten), run
                       `./exlaunch.sh build` under devkitPro's msys2 bash
                       with BRIDGE_HOST + MOD_VERSION baked from
                       detect_lan_ip() and the apworld version,
                       harvest the resulting subsdk9 + main.npdm.
  - `deploy.py`      — copy outputs to SD card / Ryujinx / custom folder.
  - `launcher_errors.py` — surface uncaught launch-time exceptions via Tk +
                       %APPDATA%/dread_ap/launch-crash.log so a broken
                       wizard doesn't fail silently under the AP Launcher.
  - `wizard.py`      — Kivy multi-page UI (imports Kivy lazily so the rest
                       of the package stays importable on AP-gen hosts).

Output destinations on the user's machine (all under %APPDATA%, off-repo so
nothing accidentally enters version control):

  %APPDATA%/dread_ap/build/{subsdk9,main.npdm,exlaunch-checkout/}
  %APPDATA%/dread_ap/setup_state.json   ← remembers last deploy target,
                                          romfs path, picked devkitPro root
  %APPDATA%/dread_ap/wizard.log         ← page-transition breadcrumbs
"""

from __future__ import annotations

import os
from pathlib import Path


def appdata_root() -> Path:
    """Per-user output root: `%APPDATA%/dread_ap/` on Windows,
    `~/.local/share/dread_ap/` elsewhere.

    Created on first access. Subdirs (`build/`) are created by the
    individual modules that write into them.
    """
    base = os.environ.get("APPDATA")
    if base:
        root = Path(base) / "dread_ap"
    else:
        root = Path.home() / ".local" / "share" / "dread_ap"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _space_free_build_root() -> Path:
    """A space-free directory to host the devkitPro build tree.

    Only consulted when the natural per-user location (`%APPDATA%/dread_ap`)
    contains a space — i.e. the Windows username has one ("Vod Kanakas").
    devkitPro's make-based build chain cannot tolerate a space *anywhere* in
    the build path (see `build_dir`), and every per-user dir (APPDATA,
    LOCALAPPDATA, USERPROFILE, TEMP) embeds the username, so we relocate to a
    shared, space-free, user-writable root instead.

    Tries, in order: `%PUBLIC%` (`C:\\Users\\Public`, designed for shared
    writable data), `%ProgramData%` (`C:\\ProgramData`), then the system-drive
    root (`C:\\`). Each is skipped if it (improbably) contains a space or
    can't be created. Falls back to the temp dir as a last resort — that path
    may itself contain a space, but it's the OS's best writable scratch space
    and beats failing outright."""
    candidates: list[Path] = []
    public = os.environ.get("PUBLIC")
    if public:
        candidates.append(Path(public) / "dread_ap")
    programdata = os.environ.get("ProgramData")
    if programdata:
        candidates.append(Path(programdata) / "dread_ap")
    sysdrive = os.environ.get("SystemDrive") or "C:"
    # Forward slash so `Path("C:/")` resolves to the drive *root* on Windows
    # (bare `Path("C:")` means the cwd on C:) and stays valid on POSIX tests.
    candidates.append(Path(sysdrive + "/") / "dread_ap")

    for cand in candidates:
        if " " in str(cand):
            continue
        try:
            cand.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        return cand

    import tempfile
    return Path(tempfile.gettempdir()) / "dread_ap"


def build_dir() -> Path:
    """Directory holding the exlaunch checkout + build outputs.

    Normally `%APPDATA%/dread_ap/build`. But devkitPro's make-based build
    chain (and make itself) cannot handle a space anywhere in the build path:
    the Makefile re-derives the cwd via `$(shell pwd)` / `$(CURDIR)` and feeds
    it *unquoted* into `include $(PWD)/config.mk`, VPATH, and the object-file
    lists. With a username like "Vod Kanakas", `include /c/Users/Vod
    Kanakas/.../config.mk` splits on the space and make tries to read
    `/c/Users/Vod` — a directory — dying with
    `make: *** /c/Users/Vod: Is a directory. Stop.` Quoting our own
    `cd '<path>'` shell command (commit 597107c) doesn't help, because the
    spaces are reconstructed *inside* the Makefile from the working directory.

    So when the natural location contains a space, relocate the whole build
    tree to a space-free root (see `_space_free_build_root`). Every build
    helper resolves through this one function, so the clone, compile, harvest,
    and manifest all follow the relocation in lockstep. When the username has
    no space (the common case), behaviour is unchanged."""
    default = appdata_root() / "build"
    d = default if " " not in str(default) else _space_free_build_root() / "build"
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_dir() -> Path:
    """Kept for back-compat with imports the smo-lifted wizard makes.
    Dread doesn't produce a shine/capture-map style dataset; the directory
    exists but is empty unless the per-seed patcher writes scratch state.
    """
    d = appdata_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def setup_state_path() -> Path:
    return appdata_root() / "setup_state.json"
