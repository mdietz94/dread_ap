"""Unit tests for ``_setup.build``.

Strategy: monkeypatch ``build._stream_subprocess`` so each detector takes
a scripted (BuildResult) instead of shelling out. ``build_dir`` is also
redirected at a ``tmp_path`` so the tests cannot accidentally hit a real
checkout / build artifact under ``%APPDATA%/dread_ap/build/``.

Each test stays focused on one branch of the build pipeline; the
integration test that actually runs ``./exlaunch.sh build`` lives outside
this file (manual / hardware check).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread._setup import build  # noqa: E402


@pytest.fixture
def build_root(tmp_path, monkeypatch):
    """Redirect ``build.build_dir`` at tmp_path/build/ so every helper
    that resolves paths under the build root lands here."""
    target = tmp_path / "build"
    target.mkdir()
    monkeypatch.setattr(build, "build_dir", lambda: target)
    return target


def _stub_stream(monkeypatch, results):
    """Replace _stream_subprocess with a generator of scripted results.

    Each call consumes one result. Tests that need to assert *which*
    commands ran wrap this and append to a list before yielding.
    """
    iter_results = iter(results)

    def fake(cmd, *, cwd=None, env=None, timeout, on_line):
        try:
            return next(iter_results)
        except StopIteration:
            raise AssertionError(
                f"_stream_subprocess called more times than scripted: cmd={cmd!r}"
            )

    monkeypatch.setattr(build, "_stream_subprocess", fake)


# ---- ensure_exlaunch_checkout ------------------------------------------

def test_ensure_checkout_clones_when_no_git_dir(build_root, monkeypatch):
    """First run: no ``.git`` dir under the checkout: should clone then
    reset to the pinned commit (two subprocess calls)."""
    monkeypatch.setattr(build.shutil, "which", lambda _name: "/fake/git")
    _stub_stream(monkeypatch, [
        build.BuildResult(ok=True, returncode=0, log="cloned"),  # git clone
        build.BuildResult(ok=True, returncode=0, log="HEAD now at " + build.PINNED_EXLAUNCH_COMMIT),  # git reset
    ])
    r = build.ensure_exlaunch_checkout()
    assert r.ok is True
    assert build.PINNED_EXLAUNCH_COMMIT in r.detail or "checkout at" in r.detail


def test_ensure_checkout_fetches_when_git_dir_exists(build_root, monkeypatch):
    """Subsequent runs: a ``.git`` dir under the checkout: should set-url,
    fetch the specific branch, then reset (three subprocess calls; NOT a
    clone).  The set-url step fixes stale checkouts that still point at the
    upstream randovania repo instead of our fork."""
    checkout = build_root / "exlaunch-checkout"
    (checkout / ".git").mkdir(parents=True)
    monkeypatch.setattr(build.shutil, "which", lambda _name: "/fake/git")
    commands_run: list[list[str]] = []

    def fake(cmd, *, cwd=None, env=None, timeout, on_line):
        commands_run.append(list(cmd))
        return build.BuildResult(ok=True, returncode=0, log="")

    monkeypatch.setattr(build, "_stream_subprocess", fake)
    r = build.ensure_exlaunch_checkout()
    assert r.ok is True
    # origin URL is updated to the fork before fetching
    assert any("set-url" in " ".join(c) and build.EXLAUNCH_REPO in c
               for c in commands_run)
    # fetch targets the specific branch, not a bare "fetch origin"
    assert any("fetch" in " ".join(c) and build.EXLAUNCH_BRANCH in c
               for c in commands_run)
    assert not any("clone" in " ".join(c) for c in commands_run)
    assert any("reset" in " ".join(c) and build.PINNED_EXLAUNCH_COMMIT in c
               for c in commands_run)


def test_ensure_checkout_git_missing_surfaces_install_hint(build_root, monkeypatch):
    """No git on PATH or default install locations: return a failure that
    names git as the missing tool and links the install page."""
    monkeypatch.setattr(build.shutil, "which", lambda _name: None)
    # Also neutralize the default-install-location fallback so the test is
    # deterministic on a Windows dev box that happens to have Git installed.
    monkeypatch.setattr(build, "_resolve_git", lambda: None)
    r = build.ensure_exlaunch_checkout()
    assert r.ok is False
    assert "git" in r.detail.lower()


def test_ensure_checkout_reset_failure_blames_pinned_sha(build_root, monkeypatch):
    """If ``git reset --hard <PINNED>`` fails, the actionable hint should
    name the constant the maintainer needs to bump."""
    monkeypatch.setattr(build.shutil, "which", lambda _name: "/fake/git")
    _stub_stream(monkeypatch, [
        build.BuildResult(ok=True, returncode=0, log=""),
        build.BuildResult(ok=False, returncode=1, log="bad object", detail="git reset --hard exited 1"),
    ])
    r = build.ensure_exlaunch_checkout()
    assert r.ok is False
    assert "PINNED_EXLAUNCH_COMMIT" in r.detail


# Patch-application tests removed: the Ryujinx-fix patch is folded into the
# fork (see PINNED_EXLAUNCH_COMMIT note in build.py). No separate apply step
# means no apply_ryujinx_patch / _locate_patch_file / _PATCH_SENTINEL to test.


# ---- collect_build_outputs / build_ready -------------------------------

def test_collect_build_outputs_empty_when_files_missing(build_root):
    """No subsdk9 + no main.npdm: empty dict."""
    assert build.collect_build_outputs() == {}


def test_collect_build_outputs_finds_both(build_root):
    """Both files present in the exlaunch deploy dir: dict has both keys
    mapping to file paths."""
    deploy = build._build_output_dir()
    deploy.mkdir(parents=True)
    (deploy / "subsdk9").write_bytes(b"\x00" * 1024)
    (deploy / "main.npdm").write_bytes(b"\x00" * 256)
    outputs = build.collect_build_outputs()
    assert set(outputs.keys()) == {"subsdk9", "main.npdm"}
    assert outputs["subsdk9"].is_file()
    assert outputs["main.npdm"].is_file()


def test_collect_build_outputs_partial_only_reports_present(build_root):
    """Only subsdk9 present: dict contains only that key (deploy gate
    asserts len == 2)."""
    deploy = build._build_output_dir()
    deploy.mkdir(parents=True)
    (deploy / "subsdk9").write_bytes(b"")
    outputs = build.collect_build_outputs()
    assert list(outputs.keys()) == ["subsdk9"]


def test_build_ready_requires_both_files(build_root):
    """build_ready is True iff both files are present (the wizard reads
    this to skip a no-op rebuild)."""
    deploy = build._build_output_dir()
    deploy.mkdir(parents=True)
    (deploy / "subsdk9").write_bytes(b"")
    assert build.build_ready() is False
    (deploy / "main.npdm").write_bytes(b"")
    assert build.build_ready() is True


# ---- run_build_pipeline orchestration ----------------------------------

def test_run_build_pipeline_short_circuits_on_first_failure(monkeypatch):
    """If ensure_exlaunch_checkout fails, the build step is never called."""
    monkeypatch.setattr(build, "ensure_exlaunch_checkout",
                        lambda on_line=None: build.BuildResult(
                            ok=False, returncode=1, log="no net", detail="net err"))
    monkeypatch.setattr(build, "run_exlaunch_build",
                        lambda on_line=None: pytest.fail("should not be called"))
    r = build.run_build_pipeline()
    assert r.ok is False
    assert r.detail == "net err"


def test_run_build_pipeline_returns_outputs_on_success(build_root, monkeypatch):
    """Both steps succeed AND collect_build_outputs reports both files:
    orchestrator returns ok with outputs dict populated."""
    monkeypatch.setattr(build, "ensure_exlaunch_checkout",
                        lambda on_line=None: build.BuildResult(ok=True, returncode=0, log=""))
    monkeypatch.setattr(build, "run_exlaunch_build",
                        lambda on_line=None: build.BuildResult(ok=True, returncode=0, log=""))
    deploy = build._build_output_dir()
    deploy.mkdir(parents=True)
    (deploy / "subsdk9").write_bytes(b"x" * 1024)
    (deploy / "main.npdm").write_bytes(b"y" * 256)
    r = build.run_build_pipeline()
    assert r.ok is True
    assert "subsdk9" in r.outputs
    assert "main.npdm" in r.outputs
    assert r.outputs["subsdk9"].stat().st_size == 1024


def test_run_build_pipeline_succeeds_but_outputs_missing_reports_failure(
    build_root, monkeypatch
):
    """Both steps return ok=True but the deploy dir is empty: orchestrator
    reports failure with the missing-files list."""
    monkeypatch.setattr(build, "ensure_exlaunch_checkout",
                        lambda on_line=None: build.BuildResult(ok=True, returncode=0, log=""))
    monkeypatch.setattr(build, "run_exlaunch_build",
                        lambda on_line=None: build.BuildResult(ok=True, returncode=0, log=""))
    r = build.run_build_pipeline()
    assert r.ok is False
    assert "subsdk9" in r.detail or "main.npdm" in r.detail


# ---- _to_msys_path --------------------------------------------------

def test_to_msys_path_windows_drive_letter_rewrites():
    """Drive-letter paths become msys2-form so the bash -lc cd target lands
    on the right path inside msys2."""
    p = Path("C:/Users/dread/build")
    assert build._to_msys_path(p) == "/c/Users/dread/build"


def test_to_msys_path_posix_pass_through():
    """POSIX paths go through unchanged (the Linux ``/setup`` flow doesn't
    need a drive-letter rewrite)."""
    p = Path("/home/dread/build")
    assert build._to_msys_path(p) == "/home/dread/build"
