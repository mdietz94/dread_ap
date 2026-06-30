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

Materialises ``logic_graph.json`` at session start if it is missing or
older than the compiler / its input cache. The artifact is gitignored, so
a fresh checkout has nothing on disk; this ensures tests can load it via
``_data_loader`` without the contributor running the regen command manually.
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


def _ensure_logic_graph() -> None:
    """Materialise ``logic_graph.json`` if missing or older than the extract
    script or the pinned Randovania logic cache (``PINNED_COMMIT.txt``).
    Skips silently if the cache is not available (CI fetches it first)."""
    repo_root = ROOT.parent.parent
    data_dir = ROOT / "data"
    extract = repo_root / "scripts" / "extract_dread_rules.py"
    cache = repo_root / ".dread-cache" / "randovania-logic"
    pinned = cache / "PINNED_COMMIT.txt"
    target = data_dir / "logic_graph.json"

    if not extract.exists() or not cache.exists():
        return

    input_mtime = max(extract.stat().st_mtime,
                      pinned.stat().st_mtime if pinned.exists() else 0)
    if target.exists() and target.stat().st_mtime >= input_mtime:
        return

    sys.stderr.write("\n[conftest] regenerating logic_graph.json...\n")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        [sys.executable, str(extract), "--all", "--out", str(target)],
        cwd=str(repo_root), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _, err = proc.communicate()
    if proc.returncode != 0:
        sys.stderr.write(
            f"\n[conftest] regen of logic_graph.json failed:\n"
            f"{err.decode('utf-8', errors='replace')}\n")


_ensure_logic_graph()


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
            self.team = 0
            self.slot_info: dict = {}
            # Real CommonContext exposes this; the backoff supervisor checks it.
            self.exit_event = asyncio.Event()
            # DataStorage surface (mirrors CommonContext). set_notify subscribes
            # keys + (in real AP) issues a Get/SetNotify; the stub just records
            # them so client code exercising the persisted-warp path doesn't crash.
            self.stored_data: dict = {}
            self.stored_data_notification_keys: set = set()
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

        def set_notify(self, *keys: str) -> None:
            self.stored_data_notification_keys.update(keys)

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
