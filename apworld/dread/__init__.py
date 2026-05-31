"""apworld root — registers ``DreadWorld`` and the "Dread Client" Launcher
Component with Archipelago.

Exposes the full apworld scaffolding (World subclass, items / locations /
regions tables, Rules, Options) plus the Kivy-free client Launcher entry
point, mirroring the smo_archipelago/__init__.py shape. The World import is
lazy so the Launcher Component still registers when the AP stack isn't on
sys.path (unit-test isolation).
"""
from __future__ import annotations


__version__ = "0.6.2"


# Re-export the World subclass so Archipelago's autodiscovery
# (``worlds.AutoWorld.AutoWorldRegister``) finds it. Lazy-imported so
# the Launcher Component still registers even when ``BaseClasses`` /
# the rest of the AP stack isn't on sys.path (unit-test isolation).
try:
    from .World import DreadWorld  # noqa: F401
except ImportError:
    pass
