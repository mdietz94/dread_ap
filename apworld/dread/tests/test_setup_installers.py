"""Unit tests for ``_setup.installers``.

Strategy mirrors test_setup_prereqs: monkeypatch the module's collaborators
(``candidate_pythons``, ``check_internet``, ``_probe_python_version``,
``vendored_open_dread_rando_src``) so the installer logic is exercised without
shelling out to pip or touching the network.
"""
from __future__ import annotations

import ssl
import urllib.error
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


# ---------------------------------------------------------------------------
# check_internet SSL fallback (Steam Deck / frozen bundle cert fix)
# ---------------------------------------------------------------------------

def test_check_internet_succeeds_on_200(monkeypatch):
    """Normal path: GitHub returns 200, probe is ok."""
    class _FakeResp:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *_): pass

    monkeypatch.setattr(installers.urllib.request, "urlopen",
                        lambda req, timeout, context: _FakeResp())
    monkeypatch.setattr(installers, "_best_ssl_context",
                        lambda: ssl.create_default_context())
    result = installers.check_internet()
    assert result.ok is True
    assert result.detail == "internet reachable"


def test_check_internet_ssl_cert_error_treated_as_reachable(monkeypatch):
    """SSL CERTIFICATE_VERIFY_FAILED means the host responded — treat as
    internet reachable so the preflight passes on Steam Deck / frozen bundles
    where _best_ssl_context still can't find a cert bundle."""
    ssl_err = ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED")
    url_err = urllib.error.URLError(ssl_err)

    def _fake_urlopen(req, timeout, context):
        raise url_err

    monkeypatch.setattr(installers.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(installers, "_best_ssl_context",
                        lambda: ssl.create_default_context())
    lines: list[str] = []
    result = installers.check_internet(on_line=lines.append)
    assert result.ok is True
    assert "SSL cert not verified" in result.detail or "unverified" in result.detail
    assert any("SSL" in ln for ln in lines)


def test_check_internet_network_failure_is_not_ok(monkeypatch):
    """A genuine network failure (not an SSL cert issue) returns ok=False."""
    url_err = urllib.error.URLError("timed out")

    def _fake_urlopen(req, timeout, context):
        raise url_err

    monkeypatch.setattr(installers.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(installers, "_best_ssl_context",
                        lambda: ssl.create_default_context())
    result = installers.check_internet()
    assert result.ok is False


# ---------------------------------------------------------------------------
# install_open_dread_rando version / interpreter guard
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# install_python312 winget exit-code handling
# ---------------------------------------------------------------------------

def _stub_winget_preconditions(monkeypatch, *, probe_paths=()):
    """Make check_winget + check_internet pass and pin the post-install probe
    set so a test controls whether a Python is "found on disk" without touching
    the real filesystem."""
    monkeypatch.setattr(installers, "check_winget",
                        lambda on_line=None: installers.InstallResult(
                            ok=True, returncode=0, log="winget", detail="winget"))
    monkeypatch.setattr(installers, "check_internet",
                        lambda on_line=None: installers.InstallResult(
                            ok=True, returncode=0, log="", detail="ok"))
    monkeypatch.setattr(installers, "_winget_python312_commands",
                        lambda: [[p] for p in probe_paths])
    monkeypatch.setattr(installers, "_prepend_path", lambda _p: None)


def _fake_winget_proc(returncode):
    class _FakeProc:
        stdout = iter(("Found an existing package already installed.",))

        def __init__(self):
            self.returncode = returncode

        def wait(self):
            return self.returncode
    return lambda cmd, **_kw: _FakeProc()


def test_python312_already_installed_is_success(monkeypatch):
    """winget exits 0x8A15002B (UPDATE_NOT_APPLICABLE) when Python is already
    present and there's nothing newer to upgrade to. That is NOT a failure —
    we only need a working 3.12, not the latest. (Regression: a healthy machine
    was reported as a failed install.)"""
    _stub_winget_preconditions(monkeypatch)  # nothing found on disk
    monkeypatch.setattr(installers.subprocess, "Popen",
                        _fake_winget_proc(0x8A15002B))

    result = installers.install_python312()
    assert result.ok is True
    assert result.returncode == 0
    assert "already present" in result.detail


def test_python312_package_already_installed_code_is_success(monkeypatch):
    """The sibling code 0x8A150061 (PACKAGE_ALREADY_INSTALLED) is also OK."""
    _stub_winget_preconditions(monkeypatch)
    monkeypatch.setattr(installers.subprocess, "Popen",
                        _fake_winget_proc(0x8A150061))

    result = installers.install_python312()
    assert result.ok is True
    assert "already present" in result.detail


def test_python312_found_on_disk_overrides_nonzero_exit(monkeypatch, tmp_path):
    """An unrecognized non-zero exit is forgiven if a Python 3.12 actually
    landed on disk — the post-install probe is the ground truth."""
    py = tmp_path / "python.exe"
    py.write_bytes(b"")
    _stub_winget_preconditions(monkeypatch, probe_paths=[str(py)])
    monkeypatch.setattr(installers.subprocess, "Popen",
                        _fake_winget_proc(1))

    result = installers.install_python312()
    assert result.ok is True
    assert result.returncode == 0


def test_python312_genuine_failure_still_fails(monkeypatch):
    """A real non-zero exit with no Python on disk stays a failure, with the
    manual-download hint."""
    _stub_winget_preconditions(monkeypatch)  # nothing found on disk
    monkeypatch.setattr(installers.subprocess, "Popen",
                        _fake_winget_proc(1))

    result = installers.install_python312()
    assert result.ok is False
    assert result.returncode == 1
    assert "python.org" in result.detail
