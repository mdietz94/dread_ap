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

import contextlib
import importlib.resources
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import build_dir
from .prereqs import _DEVKITPRO_DEFAULT_ROOTS, _devkitpro_msys2_bash_under

# Suppress per-child console window under the AP Launcher (no parent console
# → Windows would otherwise open a fresh console for each CONSOLE-subsystem
# child, stealing focus from the wizard). No-op on non-Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Where the patch file lives at runtime. The patch is bundled into the
# package itself (install_apworld.py packs it via rglob, so it lands at
# ``apworld/dread/_setup/exlaunch-ryujinx-fix.diff`` inside the zip),
# AND we keep a repo-root copy at ``scripts/patches/`` for dev checkouts
# that consume the source directly. Resolution order matters:
#
#   1. ``importlib.resources`` on this package — works whether the apworld
#      is loaded from disk (folder install) OR from a ``.apworld`` zip.
#      ``git apply`` needs a real filesystem path, so for the zip case we
#      materialize the patch to a temp file via ``importlib.resources.
#      as_file`` (the canonical way to bridge a zip-loaded resource into
#      a tool that demands a real path).
#   2. Repo-root ``scripts/patches/exlaunch-ryujinx-fix.diff`` walk-up —
#      covers dev checkouts where the package directory itself was
#      stripped down or the patch was removed from _setup/ to avoid the
#      duplication.
_SETUP_ROOT = Path(__file__).resolve().parent
_PATCH_FILENAME = "exlaunch-ryujinx-fix.diff"


@contextlib.contextmanager
def _locate_patch_file():
    """Yield a real-filesystem ``Path`` to ``exlaunch-ryujinx-fix.diff``,
    or ``None`` if neither the package resource nor the repo-root fallback
    can be found.

    Context manager because the importlib.resources zip-extract path
    needs scoped cleanup of its temp file; the disk-resident cases just
    yield the existing path and tear down cheaply on exit.
    """
    try:
        resource = importlib.resources.files(__package__).joinpath(_PATCH_FILENAME)
        if resource.is_file():
            with importlib.resources.as_file(resource) as p:
                yield p
                return
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        pass
    cur = _SETUP_ROOT
    for _ in range(8):  # _setup → apworld/dread → apworld → repo-root → …
        cand = cur / "scripts" / "patches" / _PATCH_FILENAME
        if cand.is_file():
            yield cand
            return
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    yield None


# Pinned upstream commit. Chosen by the dread_ap maintainers — bump
# this when upstream lands a relevant change and we've re-validated the
# patch applies cleanly + the build still works on Ryujinx + real HW.
#
# As of the patch's initial landing: open-dread-rando-exlaunch HEAD was
# 6bc5222 ("Merge pull request #14 from steven11sjf/seed-save"). The
# pin lets a future Ryujinx-only-bug regression be triaged by diffing
# what the user's `git fetch` brought down vs this known-working ref.
PINNED_EXLAUNCH_COMMIT = "6bc5222"
EXLAUNCH_REPO = "https://github.com/randovania/open-dread-rando-exlaunch.git"


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
    git = shutil.which("git")
    if git is None:
        msg = ("git not found on PATH. Install git from "
               "https://git-scm.com/download/win and click Re-check.")
        if on_line:
            on_line(msg)
        return BuildResult(ok=False, returncode=127, log=msg, detail=msg)

    checkout = _exlaunch_checkout_dir()
    if not (checkout / ".git").is_dir():
        if on_line:
            on_line(f"[exlaunch] cloning {EXLAUNCH_REPO} into {checkout}")
        checkout.mkdir(parents=True, exist_ok=True)
        # Full clone (not --depth 1) so we can `reset --hard <pinned-sha>`
        # to a sha that may be older than the latest commit.
        r = _stream_subprocess(
            [git, "clone", EXLAUNCH_REPO, str(checkout)],
            timeout=_TIMEOUTS["git_clone"],
            on_line=on_line,
        )
        if not r.ok:
            return r
    else:
        if on_line:
            on_line(f"[exlaunch] fetching updates into {checkout}")
        r = _stream_subprocess(
            [git, "fetch", "origin"],
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
                    f"the pinned sha may have been force-pushed or rebased "
                    f"upstream. Bump PINNED_EXLAUNCH_COMMIT in "
                    f"apworld/dread/_setup/build.py."),
        )

    if on_line:
        on_line(f"[exlaunch] checkout ready at {checkout}")
    return BuildResult(ok=True, returncode=0, log=r.log,
                       detail=f"checkout at {checkout}")


# ---------------------------------------------------------------------------
# Step 2 — apply our Ryujinx-fix patch
# ---------------------------------------------------------------------------

# Sentinel string introduced by the patch. If we see it in remote_api.cpp
# we know the patch is already applied and skip re-applying.
_PATCH_SENTINEL = "nn::socket::Poll DOES work on Ryujinx"


def _patch_already_applied(checkout: Path) -> bool:
    f = checkout / "source" / "program" / "remote_api.cpp"
    if not f.is_file():
        return False
    try:
        return _PATCH_SENTINEL in f.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def apply_ryujinx_patch(on_line: ProgressFn | None = None) -> BuildResult:
    """`git apply` the bundled exlaunch-ryujinx-fix.diff. Idempotent — if
    the patch's sentinel string is already present in the working tree we
    skip without erroring.
    """
    checkout = _exlaunch_checkout_dir()
    if not (checkout / ".git").is_dir():
        msg = ("exlaunch checkout missing — run ensure_exlaunch_checkout "
               "first")
        return BuildResult(ok=False, returncode=1, log=msg, detail=msg)

    if _patch_already_applied(checkout):
        msg = "patch already applied (sentinel found in remote_api.cpp)"
        if on_line:
            on_line(f"[patch] {msg}")
        return BuildResult(ok=True, returncode=0, log=msg, detail=msg)

    git = shutil.which("git")
    if git is None:
        msg = "git not found on PATH"
        if on_line:
            on_line(msg)
        return BuildResult(ok=False, returncode=127, log=msg, detail=msg)

    with _locate_patch_file() as patch:
        if patch is None:
            msg = (
                f"{_PATCH_FILENAME} not found in the apworld package "
                "(importlib.resources lookup) and no repo-root "
                "scripts/patches/ fallback. Rebuild the apworld with "
                "install_apworld.py so the patch is packed alongside "
                "_setup/."
            )
            if on_line:
                on_line(msg)
            return BuildResult(ok=False, returncode=1, log=msg, detail=msg)

        if on_line:
            on_line(f"[patch] applying {patch.name}")
        # --ignore-whitespace tolerates the line-ending normalization noise
        # that vendored-repo checkouts on Windows accumulate.
        r = _stream_subprocess(
            [git, "apply", "--ignore-whitespace", str(patch)],
            cwd=checkout,
            timeout=_TIMEOUTS["git_apply"],
            on_line=on_line,
        )

    if not r.ok:
        return BuildResult(
            ok=False, returncode=r.returncode, log=r.log,
            detail=(f"git apply failed — most likely the pinned upstream "
                    f"sha has diverged from what the patch was generated "
                    f"against. Regenerate the patch against "
                    f"{PINNED_EXLAUNCH_COMMIT} and re-bundle the apworld."),
        )
    return BuildResult(ok=True, returncode=0, log=r.log,
                       detail="patch applied")


# ---------------------------------------------------------------------------
# Step 3 — run the build under devkitPro's msys2 bash
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


def run_exlaunch_build(on_line: ProgressFn | None = None) -> BuildResult:
    """`./exlaunch.sh build` under devkitPro's msys2 bash, with
    CHERE_INVOKING=yes so the cd-target in the -lc string takes effect."""
    checkout = _exlaunch_checkout_dir()
    if not (checkout / "exlaunch.sh").is_file():
        msg = "exlaunch.sh missing — checkout may be incomplete"
        if on_line:
            on_line(msg)
        return BuildResult(ok=False, returncode=1, log=msg, detail=msg)

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
        # CHERE_INVOKING + MSYSTEM are required so the bash -lc command sees
        # the right /etc/profile.d sourcing (loads DEVKITPRO env, PATH).
        env = dict(os.environ)
        env["CHERE_INVOKING"] = "yes"
        env["MSYSTEM"] = "MSYS"
        cmd = [str(bash), "-lc",
               f"cd {msys_cwd} && ./exlaunch.sh build"]
    else:
        # POSIX: just invoke directly; devkitPro's env-source script handles
        # /etc/profile.d on shells with login behavior.
        bash = shutil.which("bash") or "/bin/bash"
        env = dict(os.environ)
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
# Headless orchestrator (CLI entry point — wizard.py is the GUI wrapper)
# ---------------------------------------------------------------------------

def run_build_pipeline(on_line: ProgressFn | None = None) -> BuildResult:
    """Single-shot fetch → patch → build orchestration.

    Useful from the CLI `/setup` command path (when the user opts out of
    or hasn't yet integrated the Kivy GUI). The wizard's BuildPage will
    eventually call the same three step functions individually so it can
    render per-step progress; this function just chains them.
    """
    if on_line:
        on_line("[build] step 1/3: ensure exlaunch checkout")
    r = ensure_exlaunch_checkout(on_line)
    if not r.ok:
        return r

    if on_line:
        on_line("[build] step 2/3: apply Ryujinx fix patch")
    r = apply_ryujinx_patch(on_line)
    if not r.ok:
        return r

    if on_line:
        on_line("[build] step 3/3: run ./exlaunch.sh build")
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

    if on_line:
        for k, p in outputs.items():
            on_line(f"[build] {k}: {p} ({p.stat().st_size} bytes)")
    return BuildResult(ok=True, returncode=0, log=r.log,
                       detail="subsdk9 + main.npdm ready",
                       outputs=outputs)
