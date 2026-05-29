"""AP options for Metroid Dread.

Minimal for v0.1 — just enough to make generation work. Per-area opt-out
toggles, accessibility presets, deathlink, etc. land later.
"""
from __future__ import annotations

from dataclasses import dataclass

from Options import Choice, PerGameCommonOptions, Toggle


class StartingArea(Choice):
    """Which Dread area Samus spawns in. v0.1 only supports Artaria
    (the vanilla start location); future versions will randomize."""
    display_name = "Starting Area"
    option_artaria = 0
    default = 0


class IncludeBossPickups(Toggle):
    """Whether boss defeats (Corpius, Kraid, Drogyga, Experiment, Escue,
    Golzuna) and EMMI defeats grant AP items. ON by default — matches
    how Randovania places them."""
    display_name = "Include Boss & EMMI Pickups"
    default = 1


@dataclass
class DreadOptions(PerGameCommonOptions):
    starting_area: StartingArea
    include_boss_pickups: IncludeBossPickups
