"""Detect the tools the setup wizard needs.

Trimmed from the smo-archipelago baa0d85 baseline: dropped cmake / ninja /
hactool / prod.keys (we don't run an NSP extractor and the exlaunch build
uses devkitPro's bundled make, not cmake). Kept: PrereqResult, _run /
_safe_run wrappers, _winget_python312_commands + _prepend_path infrastructure,
check_python312, check_devkitpro. Added: check_open_dread_rando — the
per-seed romfs patcher needs `open_dread_rando` importable from the same
Python the user picked.

Detectors are pure-Python stdlib so this module imports on any Python 3.10+
— no Kivy, no third-party deps. The wizard module is the only thing that
pulls in Kivy.

For unit-testability every shell-out goes through `_run`, which is a thin
wrapper around `subprocess.run`. Tests monkeypatch `_run` to return scripted
results without touching the user's machine.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Suppress the per-child console window when running under the Launcher's
# windowed PyInstaller (no parent console → Windows opens a fresh console
# for each CONSOLE-subsystem child, which steals focus from the wizard).
# No-op on non-Windows.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Install pages we link to from the wizard. Kept in this module so the
# wizard layer is pure layout; copy-paste from devkitPro's wiki — the
# Getting Started page is the canonical entry that points at the Windows
# installer release.
INSTALL_URLS = {
    # #files anchor scrolls past the release-notes prose straight to the
    # download table. Without it, first-time users land at the top of the
    # page and reported being confused about what to click.
    "python312": "https://www.python.org/downloads/release/python-3120/#files",
    "devkitpro": "https://devkitpro.org/wiki/Getting_Started",
    # Empty string — open_dread_rando is a pip package; the wizard renders
    # a pip-install command rather than a clickable URL.
    "open_dread_rando": "",
}


@dataclass
class PrereqResult:
    """Outcome of a single detector.

    `key` is the stable identifier the wizard uses to map back into
    `INSTALL_URLS` and to render the right label. `detail` is the
    human-readable extra (version string, error message). `install_url`
    is non-empty when `ok=False` so the wizard can surface a clickable
    link.

    `picker_label` + `picker_filter` opt the row into a "Browse..." button.
    Non-empty `picker_label` tells the wizard to render the button with
    that label as the file-picker dialog title; the picked path is then
    persisted (typically into setup_state.json). Useful for tools that
    aren't really "installed" on Windows.

    `auto_installable` opts the row into the wizard's auto-install path
    (the "Install them for me" mode). The wizard's installer registry
    (`_setup.installers`) maps `key` → install function; setting True here
    tells the wizard to render an "Auto-install" button instead of just
    the "Install page..." link. Detectors with no silent-install path
    (devkitPro, because its Windows installer is interactive Inno Setup
    that wants admin and is fragile to script) leave this False; the
    wizard shows the link and prompts for re-check.
    """
    key: str
    name: str
    ok: bool
    detail: str
    install_url: str = ""
    picker_label: str = ""
    picker_filter: tuple[str, ...] = ()
    note: str = ""
    auto_installable: bool = False


def _run(cmd: list[str], *, timeout: float = 10.0) -> tuple[int, str, str]:
    """Subprocess wrapper that returns (returncode, stdout, stderr).

    Centralized so tests can monkeypatch one function instead of mocking
    `subprocess.run` per-detector. Non-zero exit codes are NOT exceptions
    — they're the normal "tool not found" signal.

    Raises `FileNotFoundError` only when the executable name itself can't
    be resolved (i.e. not on PATH); detectors catch this and treat it as
    "not installed".
    """
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=_NO_WINDOW,
    )
    return res.returncode, res.stdout or "", res.stderr or ""


def _safe_run(cmd: list[str]) -> tuple[int, str, str] | None:
    """`_run` that returns None instead of raising on FileNotFoundError /
    OSError. Use when a detector wants to treat 'executable missing' the
    same as 'executable exists but exited non-zero'."""
    try:
        return _run(cmd)
    except (FileNotFoundError, OSError):
        return None
    except subprocess.TimeoutExpired:
        return (1, "", "timeout")


def _winget_python312_commands() -> list[list[str]]:
    """`--version` invocations for Python at winget's deterministic install
    paths.

    `winget install Python.Python.3.12` lands py.exe at
    `%LOCALAPPDATA%/Programs/Python/Launcher/py.exe` and the 3.12
    interpreter itself at `%LOCALAPPDATA%/Programs/Python/Python312/python.exe`.
    Neither dir lands on PATH for an already-running process, so a
    manual-mode user who runs winget in their terminal and clicks
    Re-check would otherwise still see "not found" until they restart
    the wizard. Probing these paths directly fixes that.
    """
    localapp = os.environ.get("LOCALAPPDATA")
    if not localapp:
        return []
    cmds: list[list[str]] = []
    py = Path(localapp) / "Programs" / "Python" / "Launcher" / "py.exe"
    if py.is_file():
        cmds.append([str(py), "-3.12", "--version"])
    python = Path(localapp) / "Programs" / "Python" / "Python312" / "python.exe"
    if python.is_file():
        cmds.append([str(python), "--version"])
    return cmds


def _prepend_path(dir_path: Path) -> None:
    """Prepend `dir_path` to `os.environ["PATH"]` for the current process.

    Used after a detector resolves a tool via a winget-deterministic path
    that isn't on PATH yet — downstream subprocess invocations need to
    find the tool by bare name. Mutation is process-local; nothing
    persists to the user's environment.
    """
    cur = os.environ.get("PATH", "")
    s = str(dir_path)
    if s in cur.split(os.pathsep):
        return
    os.environ["PATH"] = s + os.pathsep + cur if cur else s


# ---------------------------------------------------------------------------
# Python 3.12 — needed by `open_dread_rando` (no wheel for 3.13+ yet)
# ---------------------------------------------------------------------------

def check_python312() -> PrereqResult:
    """Python 3.12 launcher availability.

    `open_dread_rando` (the romfs patcher we run at AP-connect) depends on
    `mercury_engine_data_structures`, which pins to Python 3.12 — there's
    no 3.13 wheel on PyPI yet. The wizard inherits this requirement.

    Probe order:
      1. winget's deterministic install paths (`%LOCALAPPDATA%/Programs/
         Python/{Launcher/py.exe, Python312/python.exe}`). Lets a
         manual-mode user run `winget install Python.Python.3.12` in a
         separate terminal and immediately hit Re-check without
         restarting the wizard.
      2. `py -3.12` on PATH (standard on a full Python install).
      3. Plain `python3.12` for users whose installer didn't register
         the launcher.

    Side effect: when a winget-deterministic path resolves, its parent dir
    is prepended to `os.environ["PATH"]` so the patcher subprocess's
    bare-name `shutil.which("py")` lookup finds it without a shell restart.
    """
    for cmd in _winget_python312_commands():
        r = _safe_run(cmd)
        if r is None:
            continue
        rc, out, err = r
        if rc == 0:
            _prepend_path(Path(cmd[0]).parent)
            ver = (out + err).strip()
            return PrereqResult("python312", "Python 3.12", True,
                                f"{ver} ({cmd[0]})",
                                auto_installable=True)
    for cmd in (["py", "-3.12", "--version"], ["python3.12", "--version"]):
        r = _safe_run(cmd)
        if r is None:
            continue
        rc, out, err = r
        if rc == 0:
            ver = (out + err).strip()
            return PrereqResult("python312", "Python 3.12", True, ver,
                                auto_installable=True)
    return PrereqResult(
        "python312", "Python 3.12", False,
        "not found — open_dread_rando (the per-seed romfs patcher) depends "
        "on mercury_engine_data_structures, which has no Python 3.13 wheel",
        INSTALL_URLS["python312"],
        auto_installable=True,
    )


# ---------------------------------------------------------------------------
# devkitPro — the cross-compiler toolchain for the exlaunch sysmodule build
# ---------------------------------------------------------------------------

# Default install roots probed when DEVKITPRO env var is missing. The
# Windows installer does NOT reliably set the system env var (devkitPro
# uses its msys2 shell to set it for that shell only), so a brand-new
# devkitPro install often shows up with no env var visible to a fresh
# Python process. We fall back to these well-known defaults so the wizard
# Just Works on a vanilla install. Order matters: most-specific first.
_DEVKITPRO_DEFAULT_ROOTS = (
    Path("C:/devkitPro"),
    Path("/opt/devkitpro"),
    Path("/usr/local/devkitpro"),
)


def _devkitpro_gxx_under(root: Path) -> Path | None:
    """Return the cross-compiler path under `root` if it exists. Tries
    both Windows (.exe) and POSIX layouts."""
    win = root / "devkitA64" / "bin" / "aarch64-none-elf-g++.exe"
    if win.exists():
        return win
    posix = root / "devkitA64" / "bin" / "aarch64-none-elf-g++"
    if posix.exists():
        return posix
    return None


def _devkitpro_msys2_bash_under(root: Path) -> Path | None:
    """devkitPro's bundled msys2 bash — the build script (`exlaunch.sh`)
    needs to run under it because the Makefile expects POSIX path conventions.
    Returns the bash binary path if found under `root`, else None."""
    win = root / "msys2" / "usr" / "bin" / "bash.exe"
    if win.exists():
        return win
    posix = root / "msys2" / "usr" / "bin" / "bash"
    if posix.exists():
        return posix
    return None


def check_devkitpro() -> PrereqResult:
    """devkitPro installation (devkitA64 cross-compiler + bundled msys2 bash).

    Probes a chain of candidate install roots in order; the first one
    with a working cross-compiler wins. The chain is:

      1. `DEVKITPRO` env var, if set.
      2. Well-known default install roots (`_DEVKITPRO_DEFAULT_ROOTS`).

    Important: the env var is checked AND the defaults are probed even
    when the env var IS set. On Windows the devkitPro installer often
    sets `DEVKITPRO=/opt/devkitpro` — its msys2-rooted convention path,
    which a native-Windows Python process resolves to a non-existent
    `\\opt\\devkitpro\\…`. Falling through to the defaults (where
    `C:/devkitPro` is the real install root) is how we recover.

    Side effect: on success, sets `os.environ["DEVKITPRO"]` to the
    resolved-working path so downstream build subprocess invocations
    inherit a value that actually resolves. This OVERRIDES a bogus
    env-var value (like the msys2-form `/opt/devkitpro` above) rather
    than passing the broken value through to make. The mutation is
    process-local; nothing persists to the user's environment.
    """
    candidates: list[tuple[Path, str]] = []
    env_val = os.environ.get("DEVKITPRO")
    if env_val:
        candidates.append((Path(env_val), f"DEVKITPRO env var ({env_val})"))
    for default in _DEVKITPRO_DEFAULT_ROOTS:
        if not any(str(c) == str(default) for c, _ in candidates):
            candidates.append((default, f"default install path ({default})"))

    tried: list[str] = []
    for root, source in candidates:
        gxx = _devkitpro_gxx_under(root)
        if gxx is not None:
            # Overwrite env so subsequent builds see the path that actually
            # works, even when the user's environment had a bogus value.
            os.environ["DEVKITPRO"] = str(root)
            return _verify_devkitpro_gxx(gxx, str(root))
        tried.append(source)

    return PrereqResult(
        "devkitpro", "devkitPro / devkitA64", False,
        f"aarch64-none-elf-g++ not found at any of: {'; '.join(tried)}. "
        f"Install devkitPro from {INSTALL_URLS['devkitpro']} (Switch-dev "
        f"package group), then click Re-check.",
        INSTALL_URLS["devkitpro"],
        # NOT auto_installable: devkitPro's Windows installer is an
        # interactive Inno Setup with admin prompts + registry writes
        # — scripting it silently is fragile. We surface the URL and
        # rely on the user to run it.
        auto_installable=False,
    )


def _verify_devkitpro_gxx(gxx: Path, root: str) -> PrereqResult:
    """Run `g++ --version` against a discovered cross-compiler; return the
    success/failure PrereqResult for it. Also verifies the bundled msys2 bash
    exists — the build can't run without it on Windows."""
    r = _safe_run([str(gxx), "--version"])
    if not (r and r[0] == 0):
        return PrereqResult(
            "devkitpro", "devkitPro / devkitA64", False,
            f"DEVKITPRO={root}; binary exists but failed to run --version",
            INSTALL_URLS["devkitpro"],
            auto_installable=False,
        )
    first_line = (r[1] or r[2]).splitlines()[0] if (r[1] or r[2]) else "ok"
    # On Windows we also need devkitPro's bundled msys2 bash for the build
    # script; absence is a clear "reinstall and pick the msys2 component"
    # rather than a mysterious "make: command not found" downstream.
    if os.name == "nt":
        bash = _devkitpro_msys2_bash_under(Path(root))
        if bash is None:
            return PrereqResult(
                "devkitpro", "devkitPro / devkitA64", False,
                f"{root} ({first_line}); but msys2/usr/bin/bash.exe is "
                f"missing. Re-run the devkitPro installer and make sure "
                f"the 'msys2' component is selected.",
                INSTALL_URLS["devkitpro"],
                auto_installable=False,
            )
    return PrereqResult(
        "devkitpro", "devkitPro / devkitA64", True,
        f"{root} ({first_line})",
        auto_installable=False,
    )


# ---------------------------------------------------------------------------
# open_dread_rando — the per-seed romfs patcher (runs at AP-connect)
# ---------------------------------------------------------------------------

# Probe source — kept as a single -c arg. Uses ``importlib.metadata`` (stdlib
# since 3.8) to read the pip-installed version, which is reliable regardless
# of whether the package exports ``__version__`` (open-dread-rando does not;
# the old probe used ``open_dread_rando.__version__`` and tripped
# ``AttributeError`` for users with the package actually installed).
_ODR_PROBE_SOURCE = (
    "import open_dread_rando, mercury_engine_data_structures\n"
    "try:\n"
    "    from importlib.metadata import version\n"
    "    print(version('open-dread-rando'))\n"
    "except Exception:\n"
    "    print('ok')\n"
)


def candidate_pythons() -> list[str]:
    """Enumerate Python interpreters worth probing for ``open-dread-rando``.

    Returns absolute, deduped paths to real ``python.exe`` files (NOT
    ``py.exe`` launchers — those are resolved through ``-3.x`` to the
    underlying interpreter so callers don't have to know about the
    launcher argv shape later). Order is preference: per-user winget
    installs first (newest version first), then PATH lookups, then
    ``py -3.12`` / ``py -3.13`` / ``py -3`` resolutions.

    **The frozen Archipelago launcher is excluded by design.** Its
    bundled site-packages never sees a user's ``pip install``, so it
    can't satisfy this prereq — including it in the candidate list
    would surface a false MISS even when the user has open-dread-rando
    installed in a real Python. Matches the
    ``patcher_pipeline._candidate_pythons`` filter so the wizard's
    prereq probe and the runtime patcher subprocess agree on what
    counts as "a Python that has it".
    """
    seen: set[str] = set()
    out: list[str] = []

    def _add(path: str | None) -> None:
        if not path:
            return
        try:
            resolved = str(Path(path).resolve())
        except OSError:
            return
        if not Path(resolved).is_file():
            return
        base = Path(resolved).name.lower()
        if "archipelagolauncher" in base or base in {"archipelago.exe", "archipelago"}:
            return
        if resolved in seen:
            return
        seen.add(resolved)
        out.append(resolved)

    # Winget-deterministic per-user installs, newest version first so 3.12
    # outranks 3.10 if both exist.
    localapp = os.environ.get("LOCALAPPDATA")
    if localapp:
        base = Path(localapp) / "Programs" / "Python"
        if base.is_dir():
            for child in sorted(base.glob("Python*"), reverse=True):
                _add(str(child / "python.exe"))
    # PATH lookups for direct interpreter names.
    for name in ("python3.12", "python3.13", "python3", "python"):
        _add(shutil.which(name))
    # The Windows py launcher can resolve specific versions — ask it to
    # name the underlying exe so we add the real interpreter, not the
    # launcher itself (which would force callers to track the -3.x arg).
    py = shutil.which("py")
    if py:
        for flag in ("-3.12", "-3.13", "-3"):
            try:
                proc = subprocess.run(
                    [py, flag, "-c", "import sys; print(sys.executable)"],
                    capture_output=True, text=True, timeout=5,
                    creationflags=_NO_WINDOW,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if proc.returncode == 0:
                _add(proc.stdout.strip())
    return out


def _probe_open_dread_rando(python: str) -> tuple[bool, str]:
    """Run the ``_ODR_PROBE_SOURCE`` against ``python``. Returns
    ``(ok, detail)`` — ``detail`` is the version string on success or
    the last error line on failure."""
    r = _safe_run([python, "-c", _ODR_PROBE_SOURCE])
    if r is None:
        return False, "failed to launch interpreter"
    rc, out, err = r
    if rc == 0:
        line = (out or "ok").strip().splitlines()
        return True, line[0] if line else "ok"
    lines = (err or out).strip().splitlines()
    return False, lines[-1] if lines else f"exit {rc}"


def check_open_dread_rando(python: Path | str | None = None) -> PrereqResult:
    """``open_dread_rando`` + ``mercury_engine_data_structures`` importable
    from at least one detected Python interpreter.

    When called with no explicit ``python``, enumerates all candidate
    interpreters via :func:`candidate_pythons` and probes each in turn.
    **OK if ANY candidate has it** — the user may have pip-installed
    open-dread-rando into a different real Python than the wizard
    happens to prefer for the Python 3.12 row, and that should still
    count as "installed". MISS only when no candidate has it.

    The install hint targets the first candidate (the highest-priority
    interpreter), and uses its full resolved exe path — copying the
    hint pastes a command that installs into the exact Python the next
    probe will check, avoiding the ``py.exe -m pip install`` /
    ``py.exe -3.12 -c`` mismatch where pip targets a default version
    that doesn't match the version the probe runs against.

    The single-Python form (``check_open_dread_rando(python)``) is kept
    for tests + the wizard's row-rerun path; it short-circuits the
    enumeration to one explicit interpreter.
    """
    if python is not None:
        ok, detail = _probe_open_dread_rando(str(python))
        if ok:
            return PrereqResult(
                "open_dread_rando", "open-dread-rando (Python patcher)", True,
                f"{detail} ({python})",
                auto_installable=True,
            )
        return PrereqResult(
            "open_dread_rando", "open-dread-rando (Python patcher)", False,
            f"not importable from {python}: {detail}",
            note=f"Install with:  {python} -m pip install open-dread-rando",
            auto_installable=True,
        )

    candidates = candidate_pythons()
    if not candidates:
        return PrereqResult(
            "open_dread_rando", "open-dread-rando (Python patcher)", False,
            "no Python interpreter found — install Python 3.12 first",
            auto_installable=True,
        )

    last_failure: tuple[str, str] | None = None
    for cand in candidates:
        ok, detail = _probe_open_dread_rando(cand)
        if ok:
            return PrereqResult(
                "open_dread_rando", "open-dread-rando (Python patcher)", True,
                f"{detail} ({cand})",
                auto_installable=True,
            )
        last_failure = (cand, detail)

    # None of the detected Pythons satisfied the import. Surface the
    # first candidate as the install target since it's the
    # highest-priority interpreter the user likely wants the package
    # in. Including the exact resolved path means the hint and the
    # auto-installer (which targets the same `candidates[0]`) install
    # into the same Python the next probe will check — no version /
    # launcher-flag mismatch.
    target = candidates[0]
    last_msg = f" — last failure on {last_failure[0]}: {last_failure[1]}" if last_failure else ""
    return PrereqResult(
        "open_dread_rando", "open-dread-rando (Python patcher)", False,
        f"not importable from any of {len(candidates)} detected Python "
        f"interpreter(s){last_msg}",
        note=f"Install with:  {target} -m pip install open-dread-rando",
        auto_installable=True,
    )


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

def check_all(
    *,
    hactool_override: Path | None = None,    # accepted for wizard-import
    prod_keys_override: Path | None = None,  # compat; ignored
) -> list[PrereqResult]:
    """Run every detector. Order is wizard-display order — heaviest /
    most-likely-missing first so the user isn't surprised at the end of
    the list.

    The two override kwargs (`hactool_override`, `prod_keys_override`) are
    accepted but ignored — they're vestigial smo-baseline parameters left
    in the signature so the lifted wizard.py call sites don't have to
    change. Drop them when wizard.py surgery removes their call sites.
    """
    del hactool_override, prod_keys_override  # vestigial; see docstring
    python_row = check_python312()
    return [
        check_devkitpro(),
        python_row,
        check_open_dread_rando(),
    ]


def all_ok(results: list[PrereqResult]) -> bool:
    return all(r.ok for r in results)
