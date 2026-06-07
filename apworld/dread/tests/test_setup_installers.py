"""Unit tests for ``_setup.installers``.

Strategy mirrors test_setup_prereqs: monkeypatch the module's collaborators
(``candidate_pythons``, ``check_internet``, ``_probe_python_version``,
``vendored_open_dread_rando_src``) so the installer logic is exercised without
shelling out to pip or touching the network.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread._setup import installers  # noqa: E402


def _stub_preconditions(monkeypatch):
    """Make the two pre-flight gates (vendored submodule present, internet up)
    pass so a test can reach the interpreter-version guard."""
    monkeypatch.setattr(installers, "vendored_open_dread_rando_src",
                        lambda: Path("/fake/vendor/src"))
    monkeypatch.setattr(installers, "check_internet",
                        lambda on_line=None: installers.InstallResult(
                            ok=True, returncode=0, log="", detail="ok"))


def test_install_refuses_too_old_python(monkeypatch):
    """A sub-3.10 best-candidate is rejected up front with an actionable
    message — never handed to pip (which would emit the cryptic 'No matching
    distribution found' a stale Python 3.8 produced before the ordering fix)."""
    _stub_preconditions(monkeypatch)
    monkeypatch.setattr(installers, "candidate_pythons",
                        lambda: [r"C:\Python38-32\python.exe"])
    monkeypatch.setattr(installers, "_probe_python_version", lambda exe: (3, 8))

    # If the guard fails to fire, this would be invoked — make that a hard fail.
    def _boom(*_a, **_k):
        raise AssertionError("pip must not run for a sub-3.10 interpreter")
    monkeypatch.setattr(installers.subprocess, "Popen", _boom)

    result = installers.install_open_dread_rando()
    assert result.ok is False
    assert "3.8" in result.detail
    assert ">=3.10" in result.detail


def test_install_proceeds_for_supported_python(monkeypatch):
    """A >=3.10 target passes the guard and reaches the pip invocation."""
    _stub_preconditions(monkeypatch)
    monkeypatch.setattr(installers, "candidate_pythons",
                        lambda: [r"C:\Python312\python.exe"])
    monkeypatch.setattr(installers, "_probe_python_version", lambda exe: (3, 12))

    invoked: dict[str, list[str]] = {}

    class _FakeProc:
        stdout = iter(("Successfully installed mercury-engine-data-structures",))
        returncode = 0

        def wait(self):
            return 0

    def _fake_popen(cmd, **_kw):
        invoked["cmd"] = cmd
        return _FakeProc()
    monkeypatch.setattr(installers.subprocess, "Popen", _fake_popen)

    result = installers.install_open_dread_rando()
    assert result.ok is True
    assert invoked["cmd"][0] == r"C:\Python312\python.exe"
    assert invoked["cmd"][1:4] == ["-m", "pip", "install"]


def test_install_proceeds_when_version_unprobeable(monkeypatch):
    """If the interpreter version can't be probed (None), don't block — fall
    through to pip rather than refusing a possibly-fine Python."""
    _stub_preconditions(monkeypatch)
    monkeypatch.setattr(installers, "candidate_pythons",
                        lambda: [r"C:\Python\python.exe"])
    monkeypatch.setattr(installers, "_probe_python_version", lambda exe: None)

    class _FakeProc:
        stdout = iter(("Successfully installed",))
        returncode = 0

        def wait(self):
            return 0
    monkeypatch.setattr(installers.subprocess, "Popen",
                        lambda cmd, **_kw: _FakeProc())

    result = installers.install_open_dread_rando()
    assert result.ok is True
