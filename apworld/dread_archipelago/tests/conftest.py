"""Test config — puts the apworld root on sys.path so ``from
dread_archipelago.client.X import …`` works without an Archipelago
install (the apworld package is normally loaded by AP's worlds.X
machinery, which we don't have in unit-test isolation).

Also stubs ``CommonClient`` and ``NetUtils`` so client modules that
import the AP runtime can load in unit-test isolation. The stubs are
intentionally minimal — they expose just enough surface (``CommonContext``
base class, ``ClientCommandProcessor``, ``ClientStatus``) for our code
to import, without pulling in the full AP repo. Tests that exercise
behavior beyond that surface should monkey-patch ``send_msgs``/``send_connect``
on the constructed context as needed.
"""
from __future__ import annotations

import sys
import types
from enum import IntEnum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))


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

        async def server_auth(self, password_requested: bool = False) -> None:
            pass

        async def send_connect(self) -> None:
            pass

        async def send_msgs(self, msgs) -> None:
            pass

        async def shutdown(self) -> None:
            pass

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
