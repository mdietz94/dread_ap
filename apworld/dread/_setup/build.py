"""Build the patched exlaunch sysmodule on the user's machine.

Pipeline (every step streams progress to an `on_line` callback so the
wizard's log box can render live):

  1. Ensure a checkout of open-dread-rando-exlaunch under
     %APPDATA%/dread_ap/build/exlaunch-checkout/. First run does
     `git clone --depth 1 <pinned ref>`. Subsequent runs `git fetch +
     reset --hard` so the user's local commits / stale apply don't
     accumulate.
  2. `git apply` each bundled patch in order. Idempotent per-patch — we
     probe for that patch's sentinel string in the working tree first;
     if it's present we skip just that patch. Apply order matters:
     Ryujinx-fix introduces the ``nn::socket::Poll`` declaration the
     tcp-client patch relies on.
  3. Run `./exlaunch.sh build` under devkitPro's bundled msys2 bash.
     Streams compiler output line-by-line.
  4. Harvest `subsdk9` + `main.npdm` from
     `<checkout>/src/open_dread_rando_exlaunch/deploy/` and return their
     paths via `collect_build_outputs`.

The patch files ship next to this module (``apworld/dread/_setup/
*.diff``). They're identical to ``scripts/patches/*.diff`` in the source
tree — ``install_apworld.py`` packs the apworld/dread/_setup/ copies via
``rglob``, and the repo-root mirrors are kept in sync so dev checkouts
that consume the source directly still work.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.resources
import json
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

# Where the patch files live at runtime. Each patch is bundled into the
# package itself (install_apworld.py packs them via rglob, so they land at
# ``apworld/dread/_setup/<patch>.diff`` inside the zip), AND we keep a
# repo-root copy at ``scripts/patches/<patch>.diff`` for dev checkouts
# that consume the source directly. Resolution order matters:
#
#   1. ``importlib.resources`` on this package — works whether the apworld
#      is loaded from disk (folder install) OR from a ``.apworld`` zip.
#      ``git apply`` needs a real filesystem path, so for the zip case we
#      materialize the patch to a temp file via ``importlib.resources.
#      as_file`` (the canonical way to bridge a zip-loaded resource into
#      a tool that demands a real path).
#   2. Repo-root ``scripts/patches/<patch>`` walk-up — covers dev
#      checkouts where the package directory itself was stripped down or
#      the patch was removed from _setup/ to avoid the duplication.
_SETUP_ROOT = Path(__file__).resolve().parent

# Ordered list of patches to apply. The tcp-client patch references the
# ``nn::socket::Poll`` declaration that the Ryujinx-fix patch adds, so
# Ryujinx MUST be applied first. Each entry:
#   - filename: the diff filename in _setup/ AND scripts/patches/
#   - sentinel: a unique string the patch introduces in the working tree;
#     if present, the patch is already applied and we skip it.
#   - sentinel_path: file (relative to the checkout) to probe for the
#     sentinel. Use a header / file the patch is GUARANTEED to touch even
#     when only part of the patch is being re-applied (so a partial state
#     in the working tree doesn't fool the idempotency check).
@dataclass(frozen=True)
class _Patch:
    filename: str
    sentinel: str
    sentinel_path: str
    label: str
    # Files the patch creates fresh (new-file hunks). If the sentinel is
    # absent (patch not yet applied) but one of these files exists in the
    # working tree — from a prior partial / aborted apply — git apply
    # refuses ("already exists in working directory"). We delete those
    # leftovers BEFORE applying so the patch lands cleanly. Empty for
    # patches that only modify existing files.
    creates: tuple[str, ...] = ()


_PATCHES: tuple[_Patch, ...] = (
    _Patch(
        filename="exlaunch-ryujinx-fix.diff",
        sentinel="nn::socket::Poll DOES work on Ryujinx",
        sentinel_path="source/program/remote_api.cpp",
        label="Ryujinx-safe non-blocking socket loop",
    ),
    _Patch(
        # Inverts the topology: PC binds UDP :17779 + TCP :17777 and the
        # Switch UDP-discovers the bridge before TCP-dialing. Adds
        # SendTo/RecvFrom wrapper decls, resolveBridge + DialBridge, and
        # creates source/program/bridge_config.hpp with a default seed
        # IP that the wizard's BridgeIpPage can overwrite.
        filename="exlaunch-tcp-client-and-discovery.diff",
        sentinel="bool ResolveBridge",
        sentinel_path="source/program/remote_api.cpp",
        label="TCP-client + UDP-discovery topology",
        creates=("source/program/bridge_config.hpp",),
    ),
)


@contextlib.contextmanager
def _locate_patch_file(filename: str):
    """Yield a real-filesystem ``Path`` to ``<filename>``, or ``None`` if
    neither the package resource nor the repo-root fallback can be found.

    Context manager because the importlib.resources zip-extract path
    needs scoped cleanup of its temp file; the disk-resident cases just
    yield the existing path and tear down cheaply on exit.
    """
    try:
        resource = importlib.resources.files(__package__).joinpath(filename)
        if resource.is_file():
            with importlib.resources.as_file(resource) as p:
                yield p
                return
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        pass
    cur = _SETUP_ROOT
    for _ in range(8):  # _setup → apworld/dread → apworld → repo-root → …
        cand = cur / "scripts" / "patches" / filename
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
# Step 2 — apply the ordered patch list
# ---------------------------------------------------------------------------


def _patch_sentinel_present(checkout: Path, patch: _Patch) -> bool:
    f = checkout / patch.sentinel_path
    if not f.is_file():
        return False
    try:
        return patch.sentinel in f.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False


def _apply_one_patch(
    patch: _Patch,
    *,
    on_line: ProgressFn | None,
) -> BuildResult:
    """`git apply` a single patch. Skip if its sentinel is already present.

    Per-patch sentinel means a partial state (Ryujinx applied but not
    tcp-client) advances cleanly — the Ryujinx step is skipped, the
    tcp-client step runs.
    """
    checkout = _exlaunch_checkout_dir()
    if not (checkout / ".git").is_dir():
        msg = ("exlaunch checkout missing — run ensure_exlaunch_checkout "
               "first")
        return BuildResult(ok=False, returncode=1, log=msg, detail=msg)

    if _patch_sentinel_present(checkout, patch):
        msg = (f"already applied (sentinel found in "
               f"{patch.sentinel_path})")
        if on_line:
            on_line(f"[patch:{patch.filename}] {msg}")
        return BuildResult(ok=True, returncode=0, log=msg, detail=msg)

    # Clear leftover files the patch creates (from a prior partial apply
    # or aborted wizard run). git apply's new-file hunk requires the
    # target path NOT to exist; without this step a retry after a
    # partial failure surfaces as "already exists in working directory".
    for relpath in patch.creates:
        leftover = checkout / relpath
        if leftover.exists():
            try:
                leftover.unlink()
                if on_line:
                    on_line(f"[patch:{patch.filename}] removed leftover "
                            f"{relpath}")
            except OSError as e:
                msg = (f"[patch:{patch.filename}] could not remove leftover "
                       f"{relpath}: {e}")
                if on_line:
                    on_line(msg)
                return BuildResult(ok=False, returncode=1, log=msg, detail=msg)

    git = shutil.which("git")
    if git is None:
        msg = "git not found on PATH"
        if on_line:
            on_line(msg)
        return BuildResult(ok=False, returncode=127, log=msg, detail=msg)

    with _locate_patch_file(patch.filename) as patch_path:
        if patch_path is None:
            msg = (
                f"{patch.filename} not found in the apworld package "
                "(importlib.resources lookup) and no repo-root "
                "scripts/patches/ fallback. Rebuild the apworld with "
                "install_apworld.py so the patch is packed alongside "
                "_setup/."
            )
            if on_line:
                on_line(msg)
            return BuildResult(ok=False, returncode=1, log=msg, detail=msg)

        if on_line:
            on_line(f"[patch:{patch.filename}] applying ({patch.label})")
        # --ignore-whitespace tolerates the line-ending normalization noise
        # that vendored-repo checkouts on Windows accumulate.
        r = _stream_subprocess(
            [git, "apply", "--ignore-whitespace", str(patch_path)],
            cwd=checkout,
            timeout=_TIMEOUTS["git_apply"],
            on_line=on_line,
        )

    if not r.ok:
        return BuildResult(
            ok=False, returncode=r.returncode, log=r.log,
            detail=(f"git apply failed for {patch.filename} — most likely "
                    f"the pinned upstream sha has diverged from what the "
                    f"patch was generated against, or a previously-applied "
                    f"patch in the pipeline left an unexpected working "
                    f"state. Regenerate against {PINNED_EXLAUNCH_COMMIT} "
                    f"and re-bundle the apworld."),
        )
    return BuildResult(ok=True, returncode=0, log=r.log,
                       detail=f"applied {patch.filename}")


def apply_patches(on_line: ProgressFn | None = None) -> BuildResult:
    """`git apply` every patch in ``_PATCHES`` in declared order.

    Stops at the first failing patch and returns its BuildResult so the
    wizard can surface a specific remediation. Each patch is independently
    idempotent (per-patch sentinel), so re-running after a partial success
    only retries the patches that didn't land.
    """
    last: BuildResult | None = None
    for patch in _PATCHES:
        last = _apply_one_patch(patch, on_line=on_line)
        if not last.ok:
            return last
    assert last is not None
    return last


# Back-compat alias. Old wizard call sites + the headless CLI entry point
# imported `apply_ryujinx_patch` directly; the new pipeline subsumes its
# behavior + the tcp-client patch. Keeping the name as an alias is a
# one-line bridge so existing imports don't break mid-rollout.
apply_ryujinx_patch = apply_patches


# ---------------------------------------------------------------------------
# Step 2b — write generated bridge_config.hpp (wizard-driven, optional)
# ---------------------------------------------------------------------------

# Bracketed marker lines around the auto-generated #define so the wizard
# can rewrite the override block without touching the rest of the file.
# Anything inside the markers is owned by write_bridge_config(); anything
# outside (the #pragma once, comments, fallback #ifndef block) is owned
# by the patch.
_BRIDGE_CONFIG_MARK_BEGIN = "// === wizard-generated bridge config (begin) ==="
_BRIDGE_CONFIG_MARK_END   = "// === wizard-generated bridge config (end) ==="


def _bridge_config_path() -> Path:
    return (_exlaunch_checkout_dir() / "source" / "program"
            / "bridge_config.hpp")


def write_bridge_config(seed_ip: str | None,
                        on_line: ProgressFn | None = None) -> BuildResult:
    """Overwrite the ``DEFAULT_BRIDGE_SUBNET`` macro in bridge_config.hpp
    with ``seed_ip`` (the user's PC LAN IP from BridgeIpPage).

    ``seed_ip=None`` means "no wizard customization" — leave the file
    exactly as the patch produced it (with the ``#ifndef`` fallback
    default). Idempotent: a second call with the same IP is a no-op
    against the file content.

    The patch creates bridge_config.hpp with a ``#ifndef
    DEFAULT_BRIDGE_SUBNET ... #endif`` fallback. This helper inserts an
    UNCONDITIONAL ``#define`` block ABOVE the fallback, wrapped in
    sentinel comments so it can be rewritten in place on subsequent
    wizard runs.
    """
    cfg = _bridge_config_path()
    if not cfg.is_file():
        msg = (f"bridge_config.hpp missing — apply_patches must run "
               f"before write_bridge_config (expected at {cfg})")
        if on_line:
            on_line(msg)
        return BuildResult(ok=False, returncode=1, log=msg, detail=msg)

    if seed_ip is None:
        # Strip any prior wizard block so re-running without an IP returns
        # the file to its patch-default state.
        seed_ip = ""  # signal: emit no override

    # Read+write bytes (with explicit \n newlines on write) so the
    # no-op detection holds across Windows + git's autocrlf antics —
    # otherwise text-mode write would convert \n back to \r\n and the
    # next call would always "differ" from disk even with the same IP.
    try:
        original = cfg.read_bytes().decode("utf-8")
    except OSError as e:
        return BuildResult(ok=False, returncode=1, log=str(e),
                           detail=f"failed to read {cfg}: {e}")
    # Normalize CRLF → LF for comparison; write keeps LF only.
    original = original.replace("\r\n", "\n")

    # Excise any prior wizard block.
    lines = original.splitlines(keepends=True)
    out: list[str] = []
    skipping = False
    for ln in lines:
        if _BRIDGE_CONFIG_MARK_BEGIN in ln:
            skipping = True
            continue
        if skipping:
            if _BRIDGE_CONFIG_MARK_END in ln:
                skipping = False
            continue
        out.append(ln)
    stripped = "".join(out)

    if seed_ip:
        # Insert the override block immediately after the #pragma once.
        # NO leading newline — the existing post-#pragma-once blank line
        # in the file provides the separation; adding one here would
        # accumulate an extra blank line on every wizard re-run.
        insertion = (
            f"{_BRIDGE_CONFIG_MARK_BEGIN}\n"
            f"// Set by the /setup wizard's BridgeIpPage. Re-run /setup to\n"
            f"// change. Removing this block restores the fallback default\n"
            f"// from the #ifndef DEFAULT_BRIDGE_SUBNET below.\n"
            f"#define DEFAULT_BRIDGE_SUBNET \"{seed_ip}\"\n"
            f"{_BRIDGE_CONFIG_MARK_END}\n"
        )
        idx = stripped.find("#pragma once")
        if idx < 0:
            # File got mangled — write the override at the top anyway.
            new = insertion + stripped
        else:
            after = stripped.find("\n", idx)
            new = stripped[: after + 1] + insertion + stripped[after + 1 :]
    else:
        new = stripped

    if new == original:
        msg = f"bridge_config.hpp unchanged ({'no override' if not seed_ip else seed_ip})"
        if on_line:
            on_line(f"[bridge-config] {msg}")
        return BuildResult(ok=True, returncode=0, log=msg, detail=msg)

    try:
        # Explicit LF newlines — see read_bytes comment above.
        cfg.write_bytes(new.encode("utf-8"))
    except OSError as e:
        return BuildResult(ok=False, returncode=1, log=str(e),
                           detail=f"failed to write {cfg}: {e}")
    msg = (f"wrote DEFAULT_BRIDGE_SUBNET={seed_ip!r} into {cfg}"
           if seed_ip else
           f"cleared wizard override from {cfg}")
    if on_line:
        on_line(f"[bridge-config] {msg}")
    return BuildResult(ok=True, returncode=0, log=msg, detail=msg)


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
# Build staleness check
# ---------------------------------------------------------------------------

def _build_manifest_path() -> Path:
    return build_dir() / "build_manifest.json"


def _compute_build_inputs_hash() -> str | None:
    """SHA-256 of PINNED_EXLAUNCH_COMMIT + bundled patch file contents.

    Returns None if a patch file can't be read (e.g. resources not yet
    resolved in a fresh install) — callers treat None as "hash unknown,
    assume stale."

    Bridge IP is intentionally NOT included: it's per-user configuration
    that doesn't affect whether the apworld-provided source is current.
    Users who change their LAN IP can click Retry on the Build page.
    """
    h = hashlib.sha256()
    h.update(PINNED_EXLAUNCH_COMMIT.encode())
    for patch in _PATCHES:
        with _locate_patch_file(patch.filename) as p:
            if p is None:
                return None
            try:
                h.update(p.read_bytes())
            except OSError:
                return None
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

def run_build_pipeline(on_line: ProgressFn | None = None,
                       bridge_seed_ip: str | None = None) -> BuildResult:
    """Single-shot fetch → patch → write-config → build orchestration.

    Useful from the CLI ``/setup`` command path (when the user opts out
    of or hasn't yet integrated the Kivy GUI). The wizard's BuildPage
    calls the four step functions individually so it can render per-step
    progress; this function chains them.

    ``bridge_seed_ip`` is the user's PC LAN IP (passed through from
    BridgeIpPage). Passing ``None`` leaves the patch-default seed in
    bridge_config.hpp — adequate for Ryujinx-on-same-host (loopback
    discovery covers it) but the /24 sweep won't find a real Switch on
    the LAN until the wizard bakes in the right IP.
    """
    if on_line:
        on_line("[build] step 1/4: ensure exlaunch checkout")
    r = ensure_exlaunch_checkout(on_line)
    if not r.ok:
        return r

    if on_line:
        on_line("[build] step 2/4: apply patches (Ryujinx-fix + tcp-client)")
    r = apply_patches(on_line)
    if not r.ok:
        return r

    if on_line:
        on_line("[build] step 3/4: write bridge_config.hpp "
                f"(seed_ip={bridge_seed_ip!r})")
    r = write_bridge_config(bridge_seed_ip, on_line)
    if not r.ok:
        return r

    if on_line:
        on_line("[build] step 4/4: run ./exlaunch.sh build")
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
