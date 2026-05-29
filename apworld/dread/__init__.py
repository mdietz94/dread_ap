"""apworld root — registers ``DreadWorld`` and the "Dread Client" Launcher
Component with Archipelago.

Exposes the full apworld scaffolding (World subclass, items / locations /
regions tables, Rules, Options) plus the Kivy-free client Launcher entry
point, mirroring the smo_archipelago/__init__.py shape. The World import is
lazy so the Launcher Component still registers when the AP stack isn't on
sys.path (unit-test isolation).
"""
from __future__ import annotations


__version__ = "0.0.1-phase4-resolver-itemsfull"


# Re-export the World subclass so Archipelago's autodiscovery
# (``worlds.AutoWorld.AutoWorldRegister``) finds it. Lazy-imported so
# the Launcher Component still registers even when ``BaseClasses`` /
# the rest of the AP stack isn't on sys.path (unit-test isolation).
try:
    from .World import DreadWorld  # noqa: F401
except ImportError:
    pass


def launch_dread_client(*args: str) -> None:
    """Archipelago Launcher entry point for the Dread Client.

    Triggered by clicking the "Dread Client" button in the Launcher, or
    (eventually) by double-clicking a ``.dreadap`` file once we define
    that file format. For now only the button path is wired.
    """
    from worlds.LauncherComponents import launch as launch_or_subprocess
    from .client.main import launch as dread_client_launch
    launch_or_subprocess(dread_client_launch, name="DreadClient", args=args)


def add_client_to_launcher() -> None:
    """Register the "Dread Client" Component with the Archipelago Launcher.

    Idempotent. Silently skips when the ``worlds`` module isn't available
    (unit-test isolation — pytest imports this package without a full
    Archipelago install on sys.path).
    """
    try:
        from worlds.LauncherComponents import (
            Component, SuffixIdentifier, Type, components,
        )
    except ImportError:
        return
    for c in components:
        if c.display_name == "Dread Client":
            return
    components.append(Component(
        "Dread Client",
        func=launch_dread_client,
        component_type=Type.CLIENT,
        file_identifier=SuffixIdentifier('.dreadap'),
        # game_name will be set once Phase 4's World subclass lands; until
        # then a missing game_name just means the Component won't auto-
        # associate with a seed file. The Launcher button still works.
    ))


add_client_to_launcher()
