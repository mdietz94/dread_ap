"""Trimmed installer surface — the dread setup ships with one auto-install.

Drastically smaller than the smo baseline. Reasons:

  - **devkitPro**: their Windows installer is interactive Inno Setup with
    admin prompts, registry writes, and a multi-page component picker.
    Scripting it silently is fragile (every devkitPro update breaks
    silent-install assumptions). We surface a link instead and rely on
    the user — see `prereqs.check_devkitpro`'s detail-string.
  - **cmake / ninja / hactool**: not needed by dread's build (devkitPro's
    bundled make is the only build tool; we never run an NSP extractor).
  - **prod.keys**: same as smo — keys come from the user's hardware,
    not from an installer.

That leaves **Python 3.12**, installable via winget. Everything that the
smo-lifted wizard.py imports from this module is still exported (`INSTALLERS`,
`INSTALL_ORDER`, `check_internet`, `check_winget`) so wizard.py imports
succeed without surgery — the dict / tuple are just shorter.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .._vendor import (
    PATCHER_RUNTIME_DEPS,
    vendor_unavailable_diagnostic,
    vendored_open_dread_rando_src,
)
from .prereqs import (
    _prepend_path,
    _winget_python312_commands,
    candidate_pythons,
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Callback receiving one rstripped line of installer output per call.
ProgressFn = Callable[[str], None]


@dataclass
class InstallResult:
    """Outcome of one install attempt.

    `ok` is the green-light flag. `returncode` is the underlying tool's
    exit code. `log` is the full captured stream for the wizard's "Copy
    log" button. `detail` is a short human-readable summary for the
    row's status flip.
    """
    ok: bool
    returncode: int
    log: str
    detail: str = ""


# ---------------------------------------------------------------------------
# Preflight checks called before any installer runs
# ---------------------------------------------------------------------------

def check_winget(on_line: ProgressFn | None = None) -> InstallResult:
    """Verify winget is present on PATH.

    winget ships with Windows 10 1809+ via the App Installer package,
    but LTSC images and stripped Win11 setups can lack it.
    """
    exe = shutil.which("winget")
    msg = (
        "winget not found on PATH — install \"App Installer\" from the "
        "Microsoft Store, or install Python 3.12 manually from "
        "https://www.python.org/downloads/release/python-3120/#files."
    )
    if exe is None:
        if on_line:
            on_line(msg)
        return InstallResult(ok=False, returncode=127, log=msg, detail=msg)
    if on_line:
        on_line(f"[winget] resolved to {exe}")
    return InstallResult(ok=True, returncode=0, log=exe, detail=exe)


def check_internet(on_line: ProgressFn | None = None) -> InstallResult:
    """Single connectivity probe before bulk install.

    Hits `https://github.com` with a HEAD request. We don't actually care
    if GitHub is up — we care that *some* HTTPS host on the network
    responds, because winget itself depends on HTTPS. Surface ONE clear
    "no internet" error instead of a confusing timeout deep inside the
    installer.
    """
    msg_fail = (
        "no internet connectivity (HEAD https://github.com timed out / "
        "failed). Connect to the internet and try again, or install "
        "Python 3.12 manually."
    )
    try:
        req = urllib.request.Request("https://github.com", method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            if on_line:
                on_line(f"[internet] github.com HEAD -> {resp.status}")
            return InstallResult(ok=True, returncode=0,
                                 log=f"HEAD https://github.com -> {resp.status}",
                                 detail="internet reachable")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log = f"{type(e).__name__}: {e}"
        if on_line:
            on_line(f"[internet] FAIL: {log}")
        return InstallResult(ok=False, returncode=1, log=log, detail=msg_fail)


# ---------------------------------------------------------------------------
# Python 3.12 via winget — the only thing we auto-install
# ---------------------------------------------------------------------------

def install_python312(on_line: ProgressFn | None = None) -> InstallResult:
    """`winget install Python.Python.3.12 --accept-source-agreements
    --accept-package-agreements`. Streams stdout/stderr to `on_line`.

    Post-install, probes the deterministic winget paths
    (`%LOCALAPPDATA%/Programs/Python/{Launcher/py.exe,Python312/python.exe}`)
    and `_prepend_path`'s their dir so a Re-check in the same wizard run
    finds the new Python without a shell restart.
    """
    pre = check_winget(on_line)
    if not pre.ok:
        return pre
    net = check_internet(on_line)
    if not net.ok:
        return net

    cmd = [
        "winget", "install",
        "Python.Python.3.12",
        "--accept-source-agreements",
        "--accept-package-agreements",
        "--silent",
    ]
    if on_line:
        on_line(f"[install_python312] {' '.join(cmd)}")
    captured: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as e:
        msg = f"winget vanished mid-install: {e}"
        if on_line:
            on_line(msg)
        return InstallResult(ok=False, returncode=127, log=msg, detail=msg)

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        captured.append(line)
        if on_line:
            on_line(line)
    proc.wait()

    # Probe known post-install paths and update PATH for this process.
    for cmd_probe in _winget_python312_commands():
        if Path(cmd_probe[0]).is_file():
            _prepend_path(Path(cmd_probe[0]).parent)

    log = "\n".join(captured)
    if proc.returncode == 0:
        return InstallResult(ok=True, returncode=0, log=log,
                             detail="Python 3.12 installed via winget")
    return InstallResult(
        ok=False, returncode=proc.returncode, log=log,
        detail=(f"winget exited {proc.returncode}; install Python 3.12 "
                f"manually from https://www.python.org/downloads/release/"
                f"python-3120/#files"),
    )


# ---------------------------------------------------------------------------
# open_dread_rando runtime deps via pip into the first detected real Python.
# open-dread-rando itself is vendored as a git submodule; only its pip-only
# runtime deps (mercury-engine-data-structures, jsonschema, json-delta,
# open-dread-rando-exlaunch) need installing.
# ---------------------------------------------------------------------------

def install_open_dread_rando(on_line: ProgressFn | None = None) -> InstallResult:
    """``{first_detected_python} -m pip install <runtime-deps>``.

    Targets the same first ``candidate_pythons()`` entry that
    ``check_open_dread_rando`` uses as its install hint, so the install
    lands in the exact interpreter the next probe will check — no
    py.exe-default-version drift. Frozen Archipelago launcher is
    excluded from the candidate list, so we never try to ``pip install``
    into its bundled site-packages.

    Refuses to run when the vendored ``open-dread-rando`` submodule isn't
    checked out: pip-installing the deps would succeed but the patcher
    would still fail to import. The user needs to
    ``git submodule update --init vendor/open-dread-rando`` first.

    Streams pip output to ``on_line``. Re-uses ``check_internet`` for a
    crisp "no internet" error instead of pip's deep timeout.
    """
    if vendored_open_dread_rando_src() is None:
        msg = (
            f"Cannot install patcher deps: {vendor_unavailable_diagnostic()}.\n"
            "(Pip-installing the deps wouldn't help — the patcher source is "
            "missing.) Fix that, then re-run this install."
        )
        if on_line:
            on_line(msg)
        return InstallResult(ok=False, returncode=2, log=msg, detail=msg)

    net = check_internet(on_line)
    if not net.ok:
        return net

    candidates = candidate_pythons()
    if not candidates:
        msg = (
            "no Python interpreter detected to install open-dread-rando deps "
            "into. Install Python 3.12 first (auto-install row above), "
            "then re-run this install."
        )
        if on_line:
            on_line(msg)
        return InstallResult(ok=False, returncode=127, log=msg, detail=msg)

    target = candidates[0]
    cmd = [target, "-m", "pip", "install", "--upgrade", *PATCHER_RUNTIME_DEPS]
    if on_line:
        on_line(f"[install_open_dread_rando] {' '.join(cmd)}")
    captured: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as e:
        msg = f"Python interpreter vanished mid-install: {e}"
        if on_line:
            on_line(msg)
        return InstallResult(ok=False, returncode=127, log=msg, detail=msg)

    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        captured.append(line)
        if on_line:
            on_line(line)
    proc.wait()

    log = "\n".join(captured)
    if proc.returncode == 0:
        return InstallResult(
            ok=True, returncode=0, log=log,
            detail=f"open-dread-rando runtime deps installed into {target}",
        )
    deps_pip = " ".join(PATCHER_RUNTIME_DEPS)
    return InstallResult(
        ok=False, returncode=proc.returncode, log=log,
        detail=(
            f"pip exited {proc.returncode}; try running manually:\n"
            f"    {target} -m pip install {deps_pip}"
        ),
    )


# ---------------------------------------------------------------------------
# Registry consumed by wizard.py
# ---------------------------------------------------------------------------

# Maps PrereqResult.key → installer function (key has auto_installable=True
# in prereqs.py to opt into this code path). The wizard's "Install all
# missing" button iterates INSTALL_ORDER, skipping any key not in this dict.
INSTALLERS: dict[str, Callable[[ProgressFn | None], InstallResult]] = {
    "python312": install_python312,
    "open_dread_rando": install_open_dread_rando,
    # Notably absent: devkitpro (interactive installer; user runs manually).
}

# Install order — Python 3.12 first because the open_dread_rando install
# needs a real Python to target, and INSTALL_ORDER is consumed as a
# dependency-respecting sequence by the wizard's "Install all missing".
INSTALL_ORDER: tuple[str, ...] = (
    "python312",
    "open_dread_rando",
)
