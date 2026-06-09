"""AP options for Metroid Dread.

Minimal for v0.1 — just enough to make generation work. Per-area opt-out
toggles and accessibility presets land later; DeathLink is wired (see
``DeathLink`` below + the client's death detect/kill path).
"""
from __future__ import annotations

from dataclasses import dataclass, make_dataclass

from Options import Choice, DefaultOnToggle, PerGameCommonOptions, Range, Toggle

from .Tricks import FOLLOW_GLOBAL, VISIBLE_TRICKS


class StartingArea(Choice):
    """Which Dread region Samus spawns in. 'artaria' is the vanilla start;
    every other region spawns at one of its save/navigation stations. A
    non-Artaria spawn uses the native region-graph logic and grants the minimal
    extra starting items needed to bootstrap from that location (mirrors
    Randovania's per-start starting items)."""
    display_name = "Starting Area"
    option_artaria = 0
    option_cataris = 1
    option_dairon = 2
    option_burenia = 3
    option_ghavoran = 4
    option_ferenia = 5
    option_hanubia = 6
    default = 0


class DoorLockRando(Choice):
    """Randomize the weapon/tool needed to open each door (Randovania-style).
    'off' keeps vanilla doors. 'randomized' reassigns every eligible door a
    random weapon type — both sides of a door always match. The access logic
    accounts for the new requirements (a door may now demand e.g. Wave Beam or
    Power Bombs), and items are placed so the seed stays solvable. Enabling this
    uses the native region-graph logic model."""
    display_name = "Door Lock Randomizer"
    option_off = 0
    option_randomized = 1
    default = 0


class TransportRando(Choice):
    """Randomize where elevators and shuttles go (Randovania-style). 'off' keeps
    vanilla transports. 'randomized' shuffles destinations as a two-way matching
    within type (elevator<->elevator, shuttle<->shuttle); teleporters stay
    vanilla. The access logic accounts for the new connections. Uses the native
    region-graph logic model."""
    display_name = "Transport Randomizer"
    option_off = 0
    option_randomized = 1
    default = 0


class IncludeBossPickups(Toggle):
    """Whether boss defeats (Corpius, Kraid, Drogyga, Experiment, Escue,
    Golzuna) and EMMI defeats grant AP items. ON by default — matches
    how Randovania places them."""
    display_name = "Include Boss & EMMI Pickups"
    default = 1


class TrickLevel(Choice):
    """Baseline permissiveness of the access logic — the difficulty assumed for
    every trick left on 'follow global' (see the per-trick 'Trick: …' options).
    Higher levels ASSUME the player can perform the trick (it may be REQUIRED to
    reach a check) — they do not merely allow it. Beginner assumes only the
    easiest tech; Mastery assumes everything Randovania classifies. Per-trick
    overrides let you raise or disable individual tricks relative to this."""
    display_name = "Trick Level"
    option_beginner = 1
    option_intermediate = 2
    option_advanced = 3
    option_expert = 4
    option_mastery = 5
    default = 1


class TrickOverride(Choice):
    """Base for the generated per-trick options. 'follow_global' (default) uses
    the global Trick Level; 'disabled' never assumes the trick; the named tiers
    pin it to that difficulty regardless of the global baseline. Mirrors
    Randovania's per-trick configuration."""
    option_follow_global = FOLLOW_GLOBAL
    option_disabled = 0
    option_beginner = 1
    option_intermediate = 2
    option_advanced = 3
    option_expert = 4
    option_mastery = 5
    default = FOLLOW_GLOBAL


# One option per non-hidden Dread trick (Suitless is hidden in Randovania's own
# UI and always follows the global baseline). Generated from the single-source
# Tricks.VISIBLE_TRICKS table so the option set tracks the logic database.
_TRICK_OPTION_CLASSES: dict[str, type] = {}
for _trick in VISIBLE_TRICKS:
    _TRICK_OPTION_CLASSES[_trick.attr] = type(
        "".join(p.capitalize() for p in _trick.attr.split("_")),  # e.g. TrickWallJump
        (TrickOverride,),
        {
            "display_name": f"Trick: {_trick.long_name}",
            "__doc__": (
                f"Difficulty level assumed for the '{_trick.long_name}' trick. "
                f"'follow_global' uses the global Trick Level; 'disabled' never "
                f"requires or assumes it."
            ),
        },
    )


class DeathLink(Toggle):
    """When you die, everyone with Death Link enabled dies — and vice versa.
    On an incoming death the client force-kills Samus (deferred safely through
    cutscenes); a self-induced death from an incoming link is not re-broadcast,
    so the chain terminates rather than echoing. Off by default."""
    display_name = "Death Link"


class StartWithPulseRadar(DefaultOnToggle):
    """Whether Samus starts with Pulse Radar (the hidden-block / breakable
    reveal ability). ON reproduces the Randovania starter preset. Turning it
    OFF is fully safe: Pulse Radar gates nothing in the access logic (it has
    zero rule references), so seeds are equally solvable either way — the only
    effect is that Pulse Radar becomes a findable pickup you shuffle into the
    world instead of starting inventory."""
    display_name = "Start With Pulse Radar"


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


class XStartsReleased(Toggle):
    """Release the X Parasites from Elun at the very start of the game,
    matching Randovania's `default_x_released`. With this ON, the X are loose
    from the first frame, so X-gated encounters (e.g. Golzuna and Experiment
    Z-57 only appear once the X are released) are reachable without first
    triggering the Elun release event. Logic stays solvable either way — the
    compiled rules conservatively assume the release must be triggered, so
    turning this ON only relaxes requirements. Template default: OFF."""
    display_name = "X Starts Released"


# ---------------------------------------------------------------------------
# Progressive items — mirror Randovania's Dread progressive groups. Each toggle
# replaces a group's individual tier items with a single "Progressive X" item
# (shipped in as many copies as there are tiers) that grants the next tier on
# each collection. OFF by default: omitting them reproduces today's pool, where
# every tier is shuffled as its own findable item. Enabling a group is pool-size
# neutral — N tier items out, N progressive copies in — and does not change
# solvability (the access logic still sees the same tier atoms; see
# World.collect / World.remove).
# ---------------------------------------------------------------------------

class ProgressiveSuit(Toggle):
    """Shuffle one Progressive Suit (Varia Suit → Gravity Suit) instead of the
    two suits separately."""
    display_name = "Progressive Suit"


class ProgressiveSpin(Toggle):
    """Shuffle one Progressive Spin (Spin Boost → Space Jump) instead of the
    two spin upgrades separately."""
    display_name = "Progressive Spin"


class ProgressiveChargeBeam(Toggle):
    """Shuffle one Progressive Charge Beam (Charge Beam → Diffusion Beam)
    instead of the two separately."""
    display_name = "Progressive Charge Beam"


class ProgressiveBeam(Toggle):
    """Shuffle one Progressive Beam (Wide → Plasma → Wave Beam) instead of the
    three beams separately."""
    display_name = "Progressive Beam"


class ProgressiveMissile(Toggle):
    """Shuffle one Progressive Missile (Super Missile → Ice Missile) instead of
    the two separately."""
    display_name = "Progressive Missile"


class ProgressiveBomb(Toggle):
    """Shuffle one Progressive Bomb (Bomb → Cross Bomb) instead of the two
    separately."""
    display_name = "Progressive Bomb"


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


# ---------------------------------------------------------------------------
# Per-pickup grant amounts — mirror Randovania's `ammo_pickup_configuration`
# `ammo_count` knob (one per ammo-style pickup). Like the count knobs above,
# these are logic-inert today (compiled rules collapse ammo to "have >=1 of the
# granting item", CLAUDE.md v0.3 deferred) — pure difficulty/flavor. Defaults
# match the Randovania starter preset, so omitting them in YAML reproduces
# today's patcher output exactly. The chosen amount flows to BOTH the seed-baked
# patcher path (per-placement `quantity`) AND the live wire path (via slot_data
# `item_amounts`), so own and remotely-delivered copies grant the same amount.
# ---------------------------------------------------------------------------

class MissileTankAmmo(Range):
    """How much missile capacity each Missile Tank grants. Randovania
    `ammo_count` default: 2."""
    display_name = "Missile Tank Ammo"
    range_start = 0
    range_end = 99
    default = 2


class MissilePlusTankAmmo(Range):
    """How much missile capacity each Missile+ Tank grants. Randovania
    `ammo_count` default: 10."""
    display_name = "Missile+ Tank Ammo"
    range_start = 0
    range_end = 99
    default = 10


class PowerBombTankAmmo(Range):
    """How much Power Bomb capacity each Power Bomb Tank grants. Randovania
    `ammo_count` default: 1."""
    display_name = "Power Bomb Tank Ammo"
    range_start = 0
    range_end = 10
    default = 1


class FlashShiftUpgradeAmount(Range):
    """How many extra Flash Shift chain dashes each Flash Shift Upgrade grants.
    Randovania `ammo_count` default: 1 (one additional dash per upgrade)."""
    display_name = "Flash Shift Upgrade Amount"
    range_start = 1
    range_end = 9
    default = 1


# Note: there is deliberately NO speed_booster_upgrade_amount option. In
# Randovania, Speed Booster Upgrade is a STANDARD pickup (not ammo), so each
# copy grants exactly one charge upgrade with no per-pickup `ammo_count` knob —
# unlike Flash Shift Upgrade, which is an ammo pickup (see FlashShiftUpgradeAmount).


# ---------------------------------------------------------------------------
# Flash Shift / Speed Booster chain upgrades. Neither is a base-game item;
# Randovania adds them as custom pickups and, by default, shuffles NONE of them.
# The two are modelled DIFFERENTLY in Randovania (so they expose different
# knobs — mirrored 1:1 below):
#
#   * "Flash Shift Upgrade" is an AMMO pickup of the Flash Shift major. RDV
#     config (starter preset): `pickup_count: 0`, `ammo_count: [1]`,
#     `requires_main_item: true`; the Flash Shift major carries
#     `included_ammo: [2]` — i.e. the MAIN Flash Shift bundles 2 FlashUpgrade
#     (vanilla: "2 flashes after the first" = 3 total dashes). So the main alone
#     reproduces vanilla; the FlashUpgrade>=3 route needs one shuffled upgrade
#     (or an alternative path).
#   * "Speed Booster Upgrade" is a STANDARD pickup (NOT ammo of Speed Booster).
#     RDV config: defaults to 0 shuffled. The Speed Booster major includes
#     NOTHING — there is no "from main" charge amount (vanilla has no speed
#     charge upgrade). SpeedBoostUpgrade>=N routes rely on shuffled copies or
#     alternatives.
#
# The "included" amount feeds ACCESS LOGIC: collecting a main credits its
# included ammo onto the upgrade item in `state` (see World.collect), so
# state.has("Flash Shift Upgrade", 2) clears once the main is reachable, with no
# upgrades shuffled.
# ---------------------------------------------------------------------------

class FlashShiftUpgradeCount(Range):
    """How many Flash Shift Upgrade pickups to shuffle into the item pool
    (Randovania ammo `pickup_count`). Each grants `Flash Shift Upgrade Amount`
    extra chained dashes. Randovania does NOT shuffle these by default — default
    0; the vanilla chains come bundled in the main Flash Shift (see Flash Shift
    Included Ammo)."""
    display_name = "Flash Shift Upgrade Count"
    range_start = 0
    range_end = 16
    default = 0


class SpeedBoosterUpgradeCount(Range):
    """How many Speed Booster Upgrade pickups to shuffle into the item pool
    (Randovania standard `num_shuffled_pickups`). Each reduces Speed Booster
    charge time. Randovania does NOT shuffle these by default — default 0. The
    main Speed Booster grants none (vanilla has no charge upgrade), so
    speed-charge routes need shuffled copies (or an alternative path)."""
    display_name = "Speed Booster Upgrade Count"
    range_start = 0
    range_end = 16
    default = 0


class FlashShiftIncludedAmmo(Range):
    """How many Flash Shift Upgrades the MAIN Flash Shift pickup bundles in
    (Randovania's `included_ammo` for the Flash Shift major). Vanilla = 2 ("2
    flashes after the first" → 3 total dashes), so default 2. This feeds the
    access logic (collecting the main credits this many in `state`); the
    FlashUpgrade>=3 route therefore needs one shuffled Flash Shift Upgrade or an
    alternative. Raise it to bundle more dashes into the main; if the main plus
    any shuffled upgrades can't reach a route's requirement, that route drops out
    of logic."""
    display_name = "Flash Shift Included Ammo"
    range_start = 0
    range_end = 9
    default = 2


class FlashShiftUpgradeRequiresMainItem(DefaultOnToggle):
    """Whether Flash Shift Upgrades require the main Flash Shift to function
    (Randovania's ammo `requires_main_item`, default ON). In this world this is
    effectively always ON regardless: every compiled rule that references a Flash
    Shift Upgrade also requires Flash Shift, and the extra dashes are inert
    in-game without the ability. Exposed for parity with Randovania; turning it
    OFF does not relax the access logic (the requirement is baked into the
    rules)."""
    display_name = "Flash Shift Upgrade Requires Main Item"


@dataclass
class _DreadOptionsBase(PerGameCommonOptions):
    starting_area: StartingArea
    door_lock_rando: DoorLockRando
    transport_rando: TransportRando
    include_boss_pickups: IncludeBossPickups
    trick_level: TrickLevel
    death_link: DeathLink
    start_with_pulse_radar: StartWithPulseRadar
    show_boss_lifebar: ShowBossLifebar
    show_enemy_life: ShowEnemyLife
    show_enemy_damage: ShowEnemyDamage
    show_player_damage: ShowPlayerDamage
    enable_death_counter: EnableDeathCounter
    room_name_display: RoomNameDisplay
    raven_beak_damage_table: RavenBeakDamageTable
    nerf_power_bombs: NerfPowerBombs
    x_starts_released: XStartsReleased
    progressive_suit: ProgressiveSuit
    progressive_spin: ProgressiveSpin
    progressive_charge_beam: ProgressiveChargeBeam
    progressive_beam: ProgressiveBeam
    progressive_missile: ProgressiveMissile
    progressive_bomb: ProgressiveBomb
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
    missile_tank_ammo: MissileTankAmmo
    missile_plus_tank_ammo: MissilePlusTankAmmo
    power_bomb_tank_ammo: PowerBombTankAmmo
    flash_shift_upgrade_amount: FlashShiftUpgradeAmount
    flash_shift_upgrade_count: FlashShiftUpgradeCount
    speed_booster_upgrade_count: SpeedBoosterUpgradeCount
    flash_shift_included_ammo: FlashShiftIncludedAmmo
    flash_shift_upgrade_requires_main_item: FlashShiftUpgradeRequiresMainItem


# Final options dataclass = the explicit base above + one generated field per
# non-hidden trick (kept out of the hand-written list so the per-trick set stays
# driven by Tricks.VISIBLE_TRICKS). The trick fields sort after every explicit
# option in the YAML template.
DreadOptions = make_dataclass(
    "DreadOptions",
    [(attr, cls) for attr, cls in _TRICK_OPTION_CLASSES.items()],
    bases=(_DreadOptionsBase,),
)
