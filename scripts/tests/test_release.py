"""Tests for scripts/release.py — the one-input local release wrapper.

These cover the pure guard logic (version validation + preflight refusals)
without mutating git or building anything; the git plumbing is monkeypatched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import release  # noqa: E402


def test_invalid_version_returns_2_before_any_git(monkeypatch):
    calls = []
    monkeypatch.setattr(release, "_git", lambda *a, **k: calls.append(a))
    assert release.main(["1.2"]) == 2
    assert calls == []  # never touched git


def _fake_git(state):
    """Build a _git stub from a dict of {argv-prefix: stdout}."""
    def _git(*args, check=True):
        if args[0] == "status":
            return state.get("status", "")
        if args[0] == "rev-parse":
            return state.get("branch", "main")
        if args[0] == "tag" and "--list" in args:
            return state.get("existing_tag", "")
        return ""
    return _git


def test_preflight_rejects_dirty_tree(monkeypatch):
    monkeypatch.setattr(release, "_git",
                        _fake_git({"status": " M foo.py"}))
    with pytest.raises(SystemExit, match="dirty"):
        release._preflight("v0.19.0")


def test_preflight_rejects_detached_head(monkeypatch):
    monkeypatch.setattr(release, "_git",
                        _fake_git({"branch": "HEAD"}))
    with pytest.raises(SystemExit, match="detached"):
        release._preflight("v0.19.0")


def test_preflight_rejects_existing_tag(monkeypatch):
    monkeypatch.setattr(release, "_git",
                        _fake_git({"existing_tag": "v0.19.0"}))
    with pytest.raises(SystemExit, match="already exists"):
        release._preflight("v0.19.0")


def test_preflight_passes_clean_state(monkeypatch):
    monkeypatch.setattr(release, "_git", _fake_git({}))
    release._preflight("v0.19.0")  # no raise


def test_happy_path_orders_stamp_commit_tag_build(monkeypatch):
    """The single version input flows through stamp -> commit -> tag -> build
    in that order, and the tag name is derived from the version."""
    events: list[str] = []

    monkeypatch.setattr(release, "_preflight", lambda tag: events.append(f"preflight:{tag}"))
    monkeypatch.setattr(release.set_version, "set_version",
                        lambda v: events.append(f"stamp:{v}") or v)

    def _git(*args, check=True):
        events.append("git:" + " ".join(args[:2]))
        return ""
    monkeypatch.setattr(release, "_git", _git)
    monkeypatch.setattr(release.cache_randovania_logic, "cache_all",
                        lambda: events.append("cache"))
    monkeypatch.setattr(release.install_apworld, "main",
                        lambda argv: events.append(f"build:{argv}") or 0)

    assert release.main(["v0.19.0"]) == 0
    # Order matters: preflight, stamp, commit, tag, cache, build.
    assert events[0] == "preflight:v0.19.0"
    assert events[1] == "stamp:0.19.0"
    assert events[2] == "git:commit -am"
    assert events[3] == "git:tag v0.19.0"
    assert events[4] == "cache"
    assert events[5].startswith("build:")
    assert "--mode" in events[5] and "apworld" in events[5]


def test_build_failure_aborts_without_publish(monkeypatch):
    monkeypatch.setattr(release, "_preflight", lambda tag: None)
    monkeypatch.setattr(release.set_version, "set_version", lambda v: v)
    monkeypatch.setattr(release, "_git", lambda *a, **k: "")
    monkeypatch.setattr(release.cache_randovania_logic, "cache_all", lambda: None)
    monkeypatch.setattr(release.install_apworld, "main", lambda argv: 1)
    published = []
    monkeypatch.setattr(release, "_publish", lambda tag: published.append(tag))
    with pytest.raises(SystemExit, match="Unwind"):
        release.main(["0.19.0", "--publish"])
    assert published == []  # never published on a failed build
