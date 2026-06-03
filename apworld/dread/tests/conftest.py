"""Test config — puts the apworld root on sys.path so ``from
dread.client.X import …`` works without an Archipelago
install (the apworld package is normally loaded by AP's worlds.X
machinery, which we don't have in unit-test isolation).

Also stubs ``CommonClient`` and ``NetUtils`` so client modules that
import the AP runtime can load in unit-test isolation. The stubs are
intentionally minimal — they expose just enough surface (``CommonContext``
base class, ``ClientCommandProcessor``, ``ClientStatus``) for our code
to import, without pulling in the full AP repo. Tests that exercise
behavior beyond that surface should monkey-patch ``send_msgs``/``send_connect``
on the constructed context as needed.

Regenerates the compiled rule artifacts at session start if they are
missing or older than the compiler / its input cache. The artifacts are
gitignored (each is 150-240k lines), so a fresh checkout has nothing on
disk; this ensures tests can still load them via ``_data_loader`` without
the contributor running the regen command manually.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import types
from enum import IntEnum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))


def _ensure_compiled_rules() -> None:
    """Run ``scripts/extract_dread_rules.py`` for each trick level if the
    on-disk artifact is missing or stale.

    Stale = older than the compiler script itself, or older than the pinned
    Randovania logic cache (``PINNED_COMMIT.txt``). Either signal means
    something upstream changed and the on-disk JSON no longer matches.

    Total regen cost is ~10s for all three levels; only pays it when the
    artifacts actually need updating, so warm runs are free."""
    repo_root = ROOT.parent.parent
    data_dir = ROOT / "data"
    extract = repo_root / "scripts" / "extract_dread_rules.py"
    cache = repo_root / ".dread-cache" / "randovania-logic"
    pinned = cache / "PINNED_COMMIT.txt"

    if not extract.exists():
        return  # not in the dev tree (e.g. running from an installed apworld)

    if not cache.exists():
        # Without the cache we cannot regen; let the individual tests fail with
        # a clear FileNotFoundError pointing at the missing artifact.
        return

    # Newest input mtime — if any output is older than this, regen.
    input_mtime = max(extract.stat().st_mtime,
                      pinned.stat().st_mtime if pinned.exists() else 0)

    targets = {
        1: data_dir / "compiled_rules.json",
        2: data_dir / "compiled_rules_l2.json",
        3: data_dir / "compiled_rules_l3.json",
    }

    stale = [
        (level, path) for level, path in targets.items()
        if not path.exists() or path.stat().st_mtime < input_mtime
    ]
    if not stale:
        return

    # Each level takes ~3.5 minutes on a single core. The three are
    # independent (no shared state, separate output files), so launch all
    # three in parallel and the wall-clock cost is ~3.5 min instead of ~10.
    sys.stderr.write(
        f"\n[conftest] regenerating {len(stale)} compiled-rule artifact(s) "
        f"in parallel (~3-4 minutes; cached afterward)...\n"
    )
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    procs = []
    for level, path in stale:
        cmd = [sys.executable, str(extract), "--all",
               "--trick-level", str(level), "--out", str(path)]
        procs.append((level, path, subprocess.Popen(
            cmd, cwd=str(repo_root), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)))

    failed = []
    for level, path, proc in procs:
        _, err = proc.communicate()
        if proc.returncode != 0:
            failed.append((path.name, err.decode("utf-8", errors="replace")))

    if failed:
        for name, err in failed:
            sys.stderr.write(f"\n[conftest] regen of {name} failed:\n{err}\n")
        # Don't raise — let the individual tests fail loudly with their own
        # assertion (focused error vs. bare CalledProcessError).


_ensure_compiled_rules()


def _install_common_client_stub() -> None:
    """Provide enough of ``CommonClient`` for client modules to import."""
    if "CommonClient" in sys.modules:
        return

    class ClientCommandProcessor:  # noqa: D401
        """Stub matching AP's ``ClientCommandProcessor`` base."""

        def __init__(self, ctx=None):
            self.ctx = ctx

        def output(self, msg: str) -> None:
            pass

    class CommonContext:  # noqa: D401
        """Stub matching the parts of AP's ``CommonContext`` we touch."""

        items_handling = 0
        game = ""
        command_processor = ClientCommandProcessor

        def __init__(self, server_address=None, password=None):
            self.server_address = server_address
            self.password = password
            self.username = ""
            self.auth = None
            self.slot = 0
            self.slot_info: dict = {}
            # Real CommonContext exposes this; the backoff supervisor checks it.
            self.exit_event = asyncio.Event()
            # DeathLink surface (mirrors CommonContext). Instance-level tags so
            # tests don't bleed into each other via a shared class set.
            self.tags = {"AP"}
            self.last_death_link = 0.0
            self.player_names = {0: "Samus"}

        async def server_auth(self, password_requested: bool = False) -> None:
            pass

        async def send_connect(self) -> None:
            pass

        async def send_msgs(self, msgs) -> None:
            pass

        async def shutdown(self) -> None:
            pass

        # ---- DeathLink (faithful to CommonContext semantics) ----

        async def update_death_link(self, death_link: bool) -> None:
            if death_link:
                self.tags.add("DeathLink")
            else:
                self.tags.discard("DeathLink")

        async def send_death(self, death_text: str = "") -> None:
            self.last_death_link = 1.0
            await self.send_msgs([{
                "cmd": "Bounce", "tags": ["DeathLink"],
                "data": {"time": self.last_death_link,
                         "source": self.player_names.get(self.slot, "Samus"),
                         "cause": death_text},
            }])

        def on_deathlink(self, data: dict) -> None:
            self.last_death_link = max(data.get("time", 0.0), self.last_death_link)

    module = types.ModuleType("CommonClient")
    module.CommonContext = CommonContext  # type: ignore[attr-defined]
    module.ClientCommandProcessor = ClientCommandProcessor  # type: ignore[attr-defined]
    sys.modules["CommonClient"] = module


def _install_netutils_stub() -> None:
    """Provide ``NetUtils.ClientStatus`` for the goal-reporting path."""
    if "NetUtils" in sys.modules:
        return

    class ClientStatus(IntEnum):
        CLIENT_UNKNOWN = 0
        CLIENT_CONNECTED = 5
        CLIENT_READY = 10
        CLIENT_PLAYING = 20
        CLIENT_GOAL = 30

    module = types.ModuleType("NetUtils")
    module.ClientStatus = ClientStatus  # type: ignore[attr-defined]
    sys.modules["NetUtils"] = module


_install_common_client_stub()
_install_netutils_stub()
