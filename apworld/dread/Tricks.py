"""Trick metadata — single source of truth for per-trick configuration.

Mirrors Randovania's Dread trick database (``resource_database.tricks`` in
``header.json``, pinned commit ``3559136dc4``). Each trick is referenced in the
access logic at a difficulty *level* (1=Beginner … 5=Mastery); a requirement
``trick T at level N`` is satisfied iff the seed's *effective* level for T is at
least N.

Unlike the old design — which collapsed every trick to Trivial/Impossible at
compile time against one global level and baked three files — tricks are now kept
SYMBOLIC in a single ``compiled_rules.json`` (atom ``("trick", short_name,
level)``) and resolved here at AP-generation time. That lets each trick be
configured independently (``Options.py`` generates one option per non-hidden
trick) while a global ``Trick Level`` supplies the baseline for any left on
"follow global".

The short_name / long_name / hide flags are functional Randovania identifiers,
not Nintendo IP — safe to commit (CLAUDE.md). ``Suitless`` (Heat/Cold runs) is
hidden in Randovania's own UI, so it gets no per-trick option and always follows
the global baseline.
"""
from __future__ import annotations

from typing import NamedTuple


class TrickDef(NamedTuple):
    short_name: str   # Randovania resource key, used in compiled trick atoms
    long_name: str    # human label (option display name)
    attr: str         # DreadOptions field / option key (without the "trick_" is
                      # NOT stripped — this IS the full field name)
    hidden: bool      # True → no per-trick option; always follows global


# Order mirrors header.json. ``attr`` is the stable YAML option key — keep it
# fixed even if a long_name changes, so existing YAMLs don't silently break.
DREAD_TRICKS: list[TrickDef] = [
    TrickDef("Knowledge", "Knowledge", "trick_knowledge", False),
    TrickDef("Movement", "Movement", "trick_movement", False),
    TrickDef("Combat", "Combat", "trick_combat", False),
    TrickDef("Pseudo", "Pseudo-Wave Beam", "trick_pseudo_wave_beam", False),
    TrickDef("IBJ", "Infinite Bomb Jump", "trick_infinite_bomb_jump", False),
    TrickDef("WBJ", "Water Bomb Jump", "trick_water_bomb_jump", False),
    TrickDef("WSJ", "Water Space Jump", "trick_water_space_jump", False),
    TrickDef("SWJ", "Single-wall Wall Jump", "trick_single_wall_jump", False),
    TrickDef("Slide", "Slide Jump", "trick_slide_jump", False),
    TrickDef("Speedbooster", "Speed Booster Conservation",
             "trick_speed_booster_conservation", False),
    TrickDef("Walljump", "Wall Jump", "trick_wall_jump", False),
    TrickDef("Suitless", "Heat/Cold Runs", "trick_suitless", True),
    TrickDef("RGrapple", "Reverse Grapple Block", "trick_reverse_grapple", False),
    TrickDef("DBoost", "Damage Boost", "trick_damage_boost", False),
    TrickDef("FrozenEnemy", "Stand on Frozen Enemy", "trick_frozen_enemy", False),
    TrickDef("GrappleMovement", "Grapple Movement", "trick_grapple_movement", False),
    TrickDef("CrossSkip", "Cross Bomb Skip", "trick_cross_bomb_skip", False),
    TrickDef("TunnelSlope", "Climb Sloped Tunnels", "trick_climb_sloped_tunnels", False),
    TrickDef("ShortBoost", "Short Boost", "trick_short_boost", False),
    TrickDef("DiffusionAbuse", "Diffusion Abuse", "trick_diffusion_abuse", False),
    TrickDef("FlashSkip", "Flash Shift Skip", "trick_flash_shift_skip", False),
    TrickDef("DBJ", "Diagonal Bomb Jump", "trick_diagonal_bomb_jump", False),
    TrickDef("LedgeWarp", "Ledge Warp", "trick_ledge_warp", False),
    TrickDef("CBL", "Cross Bomb Launch", "trick_cross_bomb_launch", False),
    TrickDef("FloorClip", "Floor Clip", "trick_floor_clip", False),
    TrickDef("SlopeClimb", "Climb Sloped Surfaces", "trick_climb_sloped_surfaces", False),
]

# Numeric level → name (mirrors Randovania's Dread difficulty tiers). 0 disables
# a trick entirely; the data references levels up through 5.
TRICK_LEVEL_NAMES: dict[int, str] = {
    0: "disabled",
    1: "beginner",
    2: "intermediate",
    3: "advanced",
    4: "expert",
    5: "mastery",
}

# Sentinel option value: "use the global Trick Level baseline for this trick".
FOLLOW_GLOBAL = -1

# Visible (configurable) tricks, in declaration order.
VISIBLE_TRICKS: list[TrickDef] = [t for t in DREAD_TRICKS if not t.hidden]


def effective_trick_levels(options) -> dict[str, int]:
    """Resolve every trick's effective level for one slot.

    A trick left on ``follow_global`` (or hidden, like Suitless) takes the
    global ``trick_level`` baseline; an explicit per-trick override wins. The
    returned ``{short_name: level}`` map is consumed by ``Rules.compile_to_lambda``
    to evaluate symbolic trick atoms (``level <= effective`` ⇒ assumed)."""
    global_level = int(options.trick_level.value)
    levels: dict[str, int] = {}
    for trick in DREAD_TRICKS:
        if trick.hidden:
            levels[trick.short_name] = global_level
            continue
        override = int(getattr(options, trick.attr).value)
        levels[trick.short_name] = (
            global_level if override == FOLLOW_GLOBAL else override
        )
    return levels
