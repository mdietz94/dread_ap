"""AP options for Metroid Dread.

Minimal for v0.1 — just enough to make generation work. Per-area opt-out
toggles, accessibility presets, deathlink, etc. land later.
"""
from __future__ import annotations

from dataclasses import dataclass

from Options import Choice, DefaultOnToggle, PerGameCommonOptions, Range, Toggle


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


class TrickLevel(Choice):
    """How permissive the access logic is. Beginner assumes no tricks;
    Intermediate assumes basic shinesparks / bomb jumps; Advanced assumes
    everything Randovania classifies up through Advanced. Higher levels
    ASSUME the player can perform the trick (it may be required to reach a
    check) — they do not merely allow it. Selects one of three pre-baked
    logic files."""
    display_name = "Trick Level"
    option_beginner = 1
    option_intermediate = 2
    option_advanced = 3
    default = 1


# ---------------------------------------------------------------------------
# Cosmetic / combat passthrough — these flow straight to the patcher config
# and have NO effect on access logic. Defaults match the starter preset so a
# YAML that omits them reproduces today's patcher output exactly.
# ---------------------------------------------------------------------------

class ShowBossLifebar(DefaultOnToggle):
    """Show the boss life bar in the HUD. Template default: ON."""
    display_name = "Show Boss Lifebar"


class ShowEnemyLife(Toggle):
    """Show enemy health values in the HUD. Template default: OFF."""
    display_name = "Show Enemy Life"


class ShowEnemyDamage(Toggle):
    """Show damage numbers dealt to enemies. Template default: OFF."""
    display_name = "Show Enemy Damage"


class ShowPlayerDamage(DefaultOnToggle):
    """Show damage numbers Samus takes. Template default: ON."""
    display_name = "Show Player Damage"


class EnableDeathCounter(DefaultOnToggle):
    """Show a counter of how many times Samus has died. Template default: ON."""
    display_name = "Death Counter"


class RoomNameDisplay(Choice):
    """When to show the current room's name on-screen. 'never' hides it;
    'always' keeps it pinned; 'with_fade' shows it briefly on room entry
    then fades. Template default: never."""
    display_name = "Room Name Display"
    option_never = 0
    option_always = 1
    option_with_fade = 2
    default = 0


class RavenBeakDamageTable(Choice):
    """Raven Beak's beam/missile damage scaling. 'unmodified' keeps the
    vanilla per-weapon table; 'consistent_low'/'consistent_high' flatten
    every weapon to one low/high multiplier. Template default: consistent_low."""
    display_name = "Raven Beak Damage Table"
    option_unmodified = 0
    option_consistent_low = 1
    option_consistent_high = 2
    default = 1


class NerfPowerBombs(DefaultOnToggle):
    """Reduce Power Bomb strength against certain enemies/props. Template
    default: ON."""
    display_name = "Nerf Power Bombs"


# ---------------------------------------------------------------------------
# Goal — Metroid DNA collection (mirrors Randovania's objective system).
# ---------------------------------------------------------------------------

class RequiredArtifacts(Range):
    """How many Metroid DNA must be collected to unlock the goal. 0 disables
    the DNA objective (the goal is simply reaching the ship). Mirrors
    Randovania's objective.required_artifacts; max 12 (one per boss/EMMI)."""
    display_name = "Required Metroid DNA"
    range_start = 0
    range_end = 12
    default = 3


class ArtifactPlacement(Choice):
    """Where Metroid DNA may be placed. 'prefer_bosses' locks DNA to the
    boss/EMMI/cutscene pickups (Randovania's default flavor); 'anywhere'
    shuffles DNA into the full location pool."""
    display_name = "Metroid DNA Placement"
    option_prefer_bosses = 0
    option_anywhere = 1
    default = 0


@dataclass
class DreadOptions(PerGameCommonOptions):
    starting_area: StartingArea
    include_boss_pickups: IncludeBossPickups
    trick_level: TrickLevel
    show_boss_lifebar: ShowBossLifebar
    show_enemy_life: ShowEnemyLife
    show_enemy_damage: ShowEnemyDamage
    show_player_damage: ShowPlayerDamage
    enable_death_counter: EnableDeathCounter
    room_name_display: RoomNameDisplay
    raven_beak_damage_table: RavenBeakDamageTable
    nerf_power_bombs: NerfPowerBombs
    required_artifacts: RequiredArtifacts
    artifact_placement: ArtifactPlacement
