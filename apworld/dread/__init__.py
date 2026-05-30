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

    Triggered by clicking the "Dread Client" button in the Launcher, or by
    double-clicking a ``.dreadap`` file (the Component's
    ``SuffixIdentifier('.dreadap')`` registers the extension). When a
    ``.dreadap`` is passed, its slot_name (+ optional server/password) are
    expanded into CLI args so the client lands pre-filled; the ``.dreadap``
    arg itself is dropped (the client's argparser doesn't know it). A parse
    failure never blocks the launch — we log and open the client unfilled.
    """
    from worlds.LauncherComponents import launch as launch_or_subprocess
    from .client.main import launch as dread_client_launch

    final_args = list(args)
    dreadap_path = next((a for a in final_args if a.endswith(".dreadap")), None)
    if dreadap_path:
        final_args = [a for a in final_args if not a.endswith(".dreadap")]
        try:
            from .client.dreadap_file import parse_dreadap, dreadap_to_launch_args
            from pathlib import Path
            final_args = dreadap_to_launch_args(parse_dreadap(Path(dreadap_path))) + final_args
        except Exception as e:  # noqa: BLE001 — never block the launch
            import logging
            logging.getLogger(__name__).warning(
                "could not parse %s: %s; launching client without pre-fill",
                dreadap_path, e)

    launch_or_subprocess(dread_client_launch, name="DreadClient", args=final_args)


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
        # Matches DreadWorld.game so the Launcher groups the client under the
        # right game and a .dreadap file auto-routes to this Component.
        game_name="Metroid Dread",
    ))


add_client_to_launcher()
