"""Build the patched exlaunch sysmodule on the user's machine.

Pipeline (every step streams progress to an `on_line` callback so the
wizard's log box can render live):

  1. Ensure a checkout of open-dread-rando-exlaunch under
     %APPDATA%/dread_ap/build/exlaunch-checkout/. First run does
     `git clone --depth 1 <pinned ref>`. Subsequent runs `git fetch +
     reset --hard` so the user's local commits / stale apply don't
     accumulate.
  2. `git apply` the bundled Ryujinx-fix patch. Idempotent — we probe
     for the patch's marker strings in the working tree first; if they're
     present we skip. (This avoids the "patch does not apply" error on
     re-runs.)
  3. Run `./exlaunch.sh build` under devkitPro's bundled msys2 bash.
     Streams compiler output line-by-line.
  4. Harvest `subsdk9` + `main.npdm` from
     `<checkout>/src/open_dread_rando_exlaunch/deploy/` and return their
     paths via `collect_build_outputs`.

The patch file we apply ships next to this module
(`apworld/dread/_setup/exlaunch-ryujinx-fix.diff`). It's identical to
`scripts/patches/exlaunch-ryujinx-fix.diff` in the source tree —
`install_apworld.py` will sync it at apworld-zip time.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import build_dir
from .prereqs import _DEVKITPRO_DEFAULT_ROOTS, _devkitpro_msys2_bash_under, _prepend_path

# Suppress per-child console window under the AP Launcher (no parent console
# → Windows would otherwise open a fresh console for each CONSOLE-subsystem
# child, stealing focus from the wizard). No-op on non-Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# Hard fork: github.com/mdietz94/open-dread-rando-exlaunch.
# bridge-networking has been merged into main.
#
# Upstream sync is abandoned — the wire format is entirely different
# (Switch is now the TCP dialer with UDP discovery, line-delimited JSON
# replaces the binary frame; see docs/wire-protocol.md). The existing
# Ryujinx-compat patch is folded into the fork (no separate `git apply`
# step). Bump this hash when the fork lands new commits we want to ship.
PINNED_EXLAUNCH_COMMIT = "5a8d6d6"
EXLAUNCH_REPO = "https://github.com/mdietz94/open-dread-rando-exlaunch.git"


# ---------------------------------------------------------------------------
# Progress-streaming infrastructure (lifted intact from smo baseline)
# ---------------------------------------------------------------------------

ProgressFn = Callable[[str], None]


@dataclass
class BuildResult:
    """Outcome of a single subprocess invocation.

    `ok` is the green-light flag. `returncode` and `log` are surfaced so
    the wizard's "Copy log to clipboard" button has something to copy on
    failure.
    """
    ok: bool
    returncode: int
    log: str
    detail: str = ""
    outputs: dict[str, Path] = field(default_factory=dict)


# Timeouts (seconds). Picked from real-world build durations observed on
# this machine; clamp upward for slow networks / cold cache / weak CPUs.
_TIMEOUTS = {
    "git_clone": 300,
    "git_fetch": 180,
    "git_apply": 30,
    "build": 600,  # exlaunch full build took ~30s on dev machine; pad for cold caches
}


def _stream_subprocess(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int,
    on_line: ProgressFn | None,
) -> BuildResult:
    """Run `cmd`, stream stdout/stderr line-by-line to `on_line`, return
    a BuildResult with the captured log."""
    if on_line:
        on_line(f"[run] {' '.join(cmd)}")
    captured: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError as e:
        msg = f"command not found: {cmd[0]} ({e})"
        if on_line:
            on_line(msg)
        return BuildResult(ok=False, returncode=127, log=msg, detail=msg)
    except OSError as e:
        msg = f"failed to launch {cmd[0]}: {e}"
        if on_line:
            on_line(msg)
        return BuildResult(ok=False, returncode=1, log=msg, detail=msg)

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.rstrip()
            captured.append(line)
            if on_line:
                on_line(line)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        msg = f"{cmd[0]} timed out after {timeout}s"
        if on_line:
            on_line(msg)
        return BuildResult(ok=False, returncode=124, log="\n".join(captured),
                           detail=msg)

    log = "\n".join(captured)
    if proc.returncode == 0:
        return BuildResult(ok=True, returncode=0, log=log)
    return BuildResult(ok=False, returncode=proc.returncode, log=log,
                       detail=f"{cmd[0]} exited {proc.returncode}")


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _to_msys_path(p: Path) -> str:
    """C:\\foo\\bar → /c/foo/bar so msys2 bash accepts the path. POSIX paths
    pass through unchanged."""
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _exlaunch_checkout_dir() -> Path:
    return build_dir() / "exlaunch-checkout"


# Git-for-Windows default install locations probed when `shutil.which("git")`
# misses. The installer adds `C:\Program Files\Git\cmd` to PATH, but the
# running launcher process inherits its PATH snapshot from launch time, so a
# fresh install isn't visible until the user restarts. Probing the defaults
# and `_prepend_path`-ing the hit lets a manual-mode user install Git in a
# separate window and immediately hit Retry without a launcher restart —
# same pattern as `check_python312` / `check_devkitpro` in prereqs.py.
def _git_default_install_candidates() -> list[Path]:
    cands: list[Path] = [
        Path("C:/Program Files/Git/cmd/git.exe"),
        Path("C:/Program Files/Git/bin/git.exe"),
        Path("C:/Program Files (x86)/Git/cmd/git.exe"),
    ]
    localapp = os.environ.get("LOCALAPPDATA")
    if localapp:
        # winget per-user install of Git.Git
        cands.append(Path(localapp) / "Programs" / "Git" / "cmd" / "git.exe")
    return cands


def _resolve_git() -> str | None:
    """Locate `git` for the build pipeline. Returns the resolved path or None.

    Order: `shutil.which("git")` (covers any setup that already has it on PATH,
    including PATH mutated by an earlier call here), then well-known Git-for-
    Windows install locations. On a default-path hit, prepends the binary's
    parent dir to `os.environ["PATH"]` so subsequent `shutil.which` calls in
    this process see git too (mirrors prereqs.py:_prepend_path usage)."""
    found = shutil.which("git")
    if found:
        return found
    for cand in _git_default_install_candidates():
        if cand.is_file():
            _prepend_path(cand.parent)
            return str(cand)
    return None


def _build_output_dir() -> Path:
    """Where exlaunch.sh writes its outputs (subsdk9 + main.npdm)."""
    return _exlaunch_checkout_dir() / "src" / "open_dread_rando_exlaunch" / "deploy"


# ---------------------------------------------------------------------------
# Step 1 — fetch / refresh the upstream checkout
# ---------------------------------------------------------------------------

def ensure_exlaunch_checkout(on_line: ProgressFn | None = None) -> BuildResult:
    """Clone (first run) or fetch + reset (subsequent runs) the upstream
    open-dread-rando-exlaunch repo at PINNED_EXLAUNCH_COMMIT.

    The reset is forceful — it discards any local changes in the checkout
    so a previous failed `git apply` doesn't leave it in a state where
    subsequent applies fail with "patch already applied / conflicts".
    """
    git = _resolve_git()
    if git is None:
        msg = ("git not found on PATH or in the default Git-for-Windows "
               "install locations. Install git from "
               "https://git-scm.com/download/win and click Retry.")
        if on_line:
            on_line(msg)
        return BuildResult(ok=False, returncode=127, log=msg, detail=msg)

    checkout = _exlaunch_checkout_dir()
    if not (checkout / ".git").is_dir():
        if on_line:
            on_line(f"[exlaunch] cloning {EXLAUNCH_REPO} (main) into {checkout}")
        checkout.mkdir(parents=True, exist_ok=True)
        # Full clone (not --depth 1) so we can `reset --hard <pinned-sha>`
        # to a sha that may be older than the latest commit.
        r = _stream_subprocess(
            [git, "clone", "-b", "main", EXLAUNCH_REPO, str(checkout)],
            timeout=_TIMEOUTS["git_clone"],
            on_line=on_line,
        )
        if not r.ok:
            return r
    else:
        # Point origin at our fork in case this checkout predates the fork
        # switch (old checkouts pointed at randovania/open-dread-rando-exlaunch).
        # git remote set-url is idempotent when the URL is already correct.
        _stream_subprocess(
            [git, "remote", "set-url", "origin", EXLAUNCH_REPO],
            cwd=checkout,
            timeout=10,
            on_line=None,
        )
        if on_line:
            on_line(f"[exlaunch] fetching updates into {checkout}")
        r = _stream_subprocess(
            [git, "fetch", "origin", "main"],
            cwd=checkout,
            timeout=_TIMEOUTS["git_fetch"],
            on_line=on_line,
        )
        if not r.ok:
            return r

    # Hard-reset to the pinned sha to clear any local edits / stale apply.
    if on_line:
        on_line(f"[exlaunch] reset --hard {PINNED_EXLAUNCH_COMMIT}")
    r = _stream_subprocess(
        [git, "reset", "--hard", PINNED_EXLAUNCH_COMMIT],
        cwd=checkout,
        timeout=_TIMEOUTS["git_apply"],
        on_line=on_line,
    )
    if not r.ok:
        return BuildResult(
            ok=False, returncode=r.returncode, log=r.log,
            detail=(f"git reset --hard {PINNED_EXLAUNCH_COMMIT} failed — "
                    f"the sha does not exist in {EXLAUNCH_REPO} "
                    f"(main). Bump PINNED_EXLAUNCH_COMMIT in "
                    f"apworld/dread/_setup/build.py."),
        )

    if on_line:
        on_line(f"[exlaunch] checkout ready at {checkout}")
    return BuildResult(ok=True, returncode=0, log=r.log,
                       detail=f"checkout at {checkout}")


# ---------------------------------------------------------------------------
# Step 2 — run the build under devkitPro's msys2 bash
#
# The Ryujinx-fix patch is folded into the fork (no separate apply step).
# ---------------------------------------------------------------------------

def _resolve_msys2_bash() -> Path | None:
    """Find devkitPro's bundled msys2 bash. Mirrors the chain in
    `prereqs.check_devkitpro`."""
    env_val = os.environ.get("DEVKITPRO")
    if env_val:
        b = _devkitpro_msys2_bash_under(Path(env_val))
        if b is not None:
            return b
    for default in _DEVKITPRO_DEFAULT_ROOTS:
        b = _devkitpro_msys2_bash_under(default)
        if b is not None:
            return b
    return None


def _build_env_overrides() -> dict[str, str]:
    """BRIDGE_HOST + MOD_VERSION baked into the sysmodule at compile time.

    Both flow through config.mk as CXXFLAGS defines. BRIDGE_HOST is the /24
    seed the Switch sweeps to find this PC (auto-detected from the builder's
    LAN IP). MOD_VERSION lands verbatim in the HELLO envelope so the
    BridgeServer can validate compatibility."""
    from ..client.net_util import detect_lan_ip
    return {
        "BRIDGE_HOST": detect_lan_ip(),
        "MOD_VERSION": "dread-bridge-0.1.0",
    }


def run_exlaunch_build(on_line: ProgressFn | None = None) -> BuildResult:
    """`./exlaunch.sh build` under devkitPro's msys2 bash."""
    checkout = _exlaunch_checkout_dir()
    if not (checkout / "exlaunch.sh").is_file():
        msg = "exlaunch.sh missing — checkout may be incomplete"
        if on_line:
            on_line(msg)
        return BuildResult(ok=False, returncode=1, log=msg, detail=msg)

    overrides = _build_env_overrides()
    if on_line:
        on_line(f"[build] BRIDGE_HOST={overrides['BRIDGE_HOST']}")
        on_line(f"[build] MOD_VERSION={overrides['MOD_VERSION']}")

    if os.name == "nt":
        bash = _resolve_msys2_bash()
        if bash is None:
            msg = ("devkitPro's bundled msys2 bash not found. Re-run the "
                   "devkitPro installer with the 'msys2' component "
                   "selected, then click Re-check.")
            if on_line:
                on_line(msg)
            return BuildResult(ok=False, returncode=127, log=msg, detail=msg)
        msys_cwd = _to_msys_path(checkout)
        env = dict(os.environ)
        env["CHERE_INVOKING"] = "yes"
        env["MSYSTEM"] = "MSYS"
        env.update(overrides)
        cmd = [str(bash), "-lc",
               f"cd {msys_cwd} && ./exlaunch.sh build"]
    else:
        bash = shutil.which("bash") or "/bin/bash"
        env = dict(os.environ)
        env.update(overrides)
        cmd = [bash, "-lc",
               f"cd {checkout} && ./exlaunch.sh build"]

    return _stream_subprocess(
        cmd, env=env,
        timeout=_TIMEOUTS["build"],
        on_line=on_line,
    )


# ---------------------------------------------------------------------------
# Step 4 — harvest outputs
# ---------------------------------------------------------------------------

def collect_build_outputs() -> dict[str, Path]:
    """Return paths to the freshly-built sysmodule files.

    Empty dict if the build hasn't run yet or didn't produce outputs.
    Keys are stable identifiers the deploy module uses:
      - "subsdk9"  → the compiled sysmodule binary
      - "main.npdm" → the launch manifest

    Both files are required at deploy time; callers should check
    `len(result) == 2` to gate deploy.
    """
    deploy = _build_output_dir()
    out: dict[str, Path] = {}
    if (deploy / "subsdk9").is_file():
        out["subsdk9"] = deploy / "subsdk9"
    if (deploy / "main.npdm").is_file():
        out["main.npdm"] = deploy / "main.npdm"
    return out


def build_ready() -> bool:
    """True when both build outputs are present on disk. Used by the
    deploy page to gate the Deploy button."""
    return len(collect_build_outputs()) == 2


# ---------------------------------------------------------------------------
# Build staleness check
# ---------------------------------------------------------------------------

def _build_manifest_path() -> Path:
    return build_dir() / "build_manifest.json"


def _compute_build_inputs_hash() -> str | None:
    """SHA-256 of PINNED_EXLAUNCH_COMMIT + the baked env overrides.

    No patch file to hash since the fork bundles its own networking changes.
    A change in detect_lan_ip() (user moved networks) makes the digest
    change and the wizard triggers a fresh rebake.
    """
    h = hashlib.sha256()
    h.update(PINNED_EXLAUNCH_COMMIT.encode())
    overrides = _build_env_overrides()
    for k in sorted(overrides):
        h.update(k.encode())
        h.update(overrides[k].encode())
    return h.hexdigest()


def write_build_manifest() -> None:
    """Record a build_manifest.json alongside the build outputs.

    Called by the wizard after a successful build so subsequent wizard
    runs can skip the 30–60s compile when nothing has changed.
    """
    digest = _compute_build_inputs_hash()
    if digest is None:
        return  # can't produce a valid manifest; leave any existing one
    try:
        _build_manifest_path().write_text(
            json.dumps({
                "inputs_hash": digest,
                "pinned_commit": PINNED_EXLAUNCH_COMMIT,
            }),
            encoding="utf-8",
        )
    except OSError:
        pass


def build_current() -> bool:
    """True when build outputs exist AND were produced from the current inputs.

    "Current" means PINNED_EXLAUNCH_COMMIT + the bundled patch files match
    the hash recorded in build_manifest.json at the end of the last build.
    If the pinned commit or either diff changes (e.g. after an apworld
    update), this returns False and the wizard triggers a fresh compile.
    """
    if not build_ready():
        return False
    digest = _compute_build_inputs_hash()
    if digest is None:
        return False  # can't verify; treat as stale
    try:
        manifest = json.loads(_build_manifest_path().read_text(encoding="utf-8"))
        return manifest.get("inputs_hash") == digest
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Headless orchestrator (CLI entry point — wizard.py is the GUI wrapper)
# ---------------------------------------------------------------------------

def run_build_pipeline(on_line: ProgressFn | None = None) -> BuildResult:
    """Single-shot fetch → build orchestration.

    The Ryujinx-fix patch is folded into the fork itself — no separate
    apply step. BRIDGE_HOST + MOD_VERSION are auto-detected and baked
    into the sysmodule via config.mk.
    """
    if on_line:
        on_line("[build] step 1/2: ensure exlaunch fork checkout")
    r = ensure_exlaunch_checkout(on_line)
    if not r.ok:
        return r

    if on_line:
        on_line("[build] step 2/2: run ./exlaunch.sh build")
    r = run_exlaunch_build(on_line)
    if not r.ok:
        return r

    outputs = collect_build_outputs()
    if len(outputs) != 2:
        missing = [k for k in ("subsdk9", "main.npdm") if k not in outputs]
        msg = f"build claimed success but {missing} missing from {_build_output_dir()}"
        if on_line:
            on_line(msg)
        return BuildResult(ok=False, returncode=1, log=r.log, detail=msg)

    write_build_manifest()
    if on_line:
        for k, p in outputs.items():
            on_line(f"[build] {k}: {p} ({p.stat().st_size} bytes)")
    return BuildResult(ok=True, returncode=0, log=r.log,
                       detail="subsdk9 + main.npdm ready",
                       outputs=outputs)
