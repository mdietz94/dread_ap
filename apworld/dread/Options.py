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


# ---------------------------------------------------------------------------
# Item pool composition — mirrors Dreadvania's per-pickup count knobs. None
# of these affect AP logic today (rules currently collapse ammo to >=1 and
# damage to suit ownership — CLAUDE.md v0.3 deferred), so they are purely
# pool-shape / difficulty options. Defaults match the Randovania starter
# preset, so omitting them in YAML reproduces vanilla behavior.
# ---------------------------------------------------------------------------

class EnergyTankCount(Range):
    """Number of Energy Tanks placed in the pool. Vanilla Randovania: 8."""
    display_name = "Energy Tank Count"
    range_start = 0
    range_end = 20
    default = 8


class EnergyPartCount(Range):
    """Number of Energy Parts placed in the pool (4 parts = +1 tank's worth of
    HP, granted immediately by default). Vanilla Randovania: 16."""
    display_name = "Energy Part Count"
    range_start = 0
    range_end = 64
    default = 16


class MissileTankCount(Range):
    """Number of Missile Tank pickups (each grants +2 missile capacity).
    Vanilla Randovania: 60."""
    display_name = "Missile Tank Count"
    range_start = 0
    range_end = 120
    default = 60


class MissilePlusTankCount(Range):
    """Number of Missile+ Tank pickups (each grants +10 missile capacity).
    Vanilla Randovania: 12."""
    display_name = "Missile+ Tank Count"
    range_start = 0
    range_end = 20
    default = 12


class PowerBombTankCount(Range):
    """Number of Power Bomb Tank pickups (each grants +1 PB capacity).
    Vanilla Randovania: 13. Setting this to 0 with starting_power_bombs=0
    is rejected at generation (PB gates become unreachable)."""
    display_name = "Power Bomb Tank Count"
    range_start = 0
    range_end = 20
    default = 13


class StartingPowerBombs(Range):
    """How many Power Bombs the main Power Bomb pickup grants on first
    collection (and Samus's starting PB capacity once the weapon unlocks).
    Vanilla Randovania: 2."""
    display_name = "Starting Power Bombs"
    range_start = 0
    range_end = 5
    default = 2


class StartingMissiles(Range):
    """Samus's starting missile capacity (and starting ammo count). Vanilla
    Randovania starter: 15. A pure difficulty knob — does not affect AP
    logic, which currently treats ammo as a binary (have/don't-have any
    missile-grant)."""
    display_name = "Starting Missiles"
    range_start = 0
    range_end = 99
    default = 15


class EnergyPerTank(Range):
    """How much energy a single Energy Tank grants (also Samus's base max HP
    before tanks). Vanilla Dread: 100. Lower values = harder; higher = easier.
    A pure difficulty knob — does not affect AP logic, which currently models
    damage as suit ownership."""
    display_name = "Energy Per Tank"
    range_start = 1
    range_end = 1499
    default = 100


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
    energy_tank_count: EnergyTankCount
    energy_part_count: EnergyPartCount
    missile_tank_count: MissileTankCount
    missile_plus_tank_count: MissilePlusTankCount
    power_bomb_tank_count: PowerBombTankCount
    starting_power_bombs: StartingPowerBombs
    starting_missiles: StartingMissiles
    energy_per_tank: EnergyPerTank
