"""Dread-side protocol dataclasses — small, replaces SMO's 600-line equivalent.

This is NOT the wire format; that lives in ``wire.py``. These are
the *semantic* item / location records that the rest of the client
shuffles around: a friendly normalization of what comes out of AP and
what we send into Lua via ``RL.ReceivePickup``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class DreadItem:
    """A Dread inventory item, identified by its patcher item_id.

    ``patcher_item_id`` is one of the ``ITEM_*`` strings from the
    open-dread-rando schema (e.g. ``"ITEM_WEAPON_MISSILE_MAX"``). It is
    the key the Lua ``RL.ReceivePickup`` ultimately routes through.

    ``ap_item_name`` is the human display name the apworld uses (e.g.
    ``"Missile Tank"``).
    """
    patcher_item_id: str
    quantity: int
    ap_item_name: str = ""


@dataclass(frozen=True)
class DreadPickupLocation:
    """A pickup node in Dread, identified by ``<scenario>/<actor>``.

    Mirrors the ``pickup_actor`` shape in the patcher JSON, plus the
    AP-side ``location_name`` for human display.
    """
    scenario: str
    actor: str
    location_name: str = ""

    @property
    def key(self) -> str:
        return f"{self.scenario}/{self.actor}"


@dataclass
class ReceivedItemEvent:
    """One inbound AP item, after we've matched it to its DreadItem."""
    item: DreadItem
    sender: str = "self"
    inventory_index: int = 0  # position in AP's items_received list
    received_at_ms: int = 0


@dataclass
class CollectedLocationEvent:
    """One location we observed the Switch report as collected."""
    location_id: int
    pickup: Optional[DreadPickupLocation] = None
    checked_at_ms: int = 0


# ---- Lua call construction helpers ----------------------------------------

# Map patcher_item_id -> the Lua class whose OnPickedUp must run when this item
# is delivered live via RL.ReceivePickup. Mirrors SPECIFIC_CLASSES from upstream
# open_dread_rando/pickups/lua_editor.py — the same table the seed-baked
# patcher uses, so seed-time and live-delivery paths grant identically. Items
# not listed here fall back to RandomizerPowerup, whose OnPickedUp only does
# additive resource grants — fine for pure-resource items (Space Jump, Varia,
# Energy Tank, missile/PB tanks, DNA, ...) but WRONG for input-toggle and
# progressive items, whose specific classes do the additional blackboard /
# input-handler setup the base class doesn't.
PATCHER_ITEM_ID_TO_CLASS: dict[str, str] = {
    "ITEM_WEAPON_POWER_BOMB": "RandomizerPowerBomb",
    "ITEM_OPTIC_CAMOUFLAGE": "RandomizerPhantomCloak",
    "ITEM_SPEED_BOOSTER": "RandomizerSpeedBooster",
    "ITEM_MULTILOCKON": "RandomizerStormMissile",
    "ITEM_LIFE_SHARDS": "RandomizerEnergyPart",
    "ITEM_GHOST_AURA": "RandomizerFlashShift",
    # Note: ITEM_UPGRADE_FLASH_SHIFT_CHAIN deliberately NOT mapped here — upstream
    # SPECIFIC_CLASSES doesn't either, so it falls through to RandomizerPowerup.
    # The tunable handler in RandomizerPowerup._ApplyTunableChanges reads its
    # total quantity and writes iChainDashMax. Mapping it to RandomizerFlashShift
    # would silently zero its quantity once the player has Flash Shift (the
    # `hasFlashShift` branch in RandomizerFlashShift.OnPickedUp).
    "ITEM_WEAPON_POWER_BEAM": "RandomizerPowerBeam",
    "ITEM_WEAPON_WIDE_BEAM": "RandomizerWideBeam",
    "ITEM_WEAPON_PLASMA_BEAM": "RandomizerPlasmaBeam",
    "ITEM_WEAPON_WAVE_BEAM": "RandomizerWaveBeam",
    "ITEM_WEAPON_MISSILE_LAUNCHER": "RandomizerMissileLauncher",
    "ITEM_WEAPON_SUPER_MISSILE": "RandomizerSuperMissile",
    "ITEM_WEAPON_ICE_MISSILE": "RandomizerIceMissile",
}


def pickup_class_for(patcher_item_id: str) -> str:
    """Return the Lua pickup class that should run OnPickedUp for this item."""
    return PATCHER_ITEM_ID_TO_CLASS.get(patcher_item_id, "RandomizerPowerup")


def build_receive_pickup_lua(
    *,
    message: str,
    progression: list[list[dict]],
    received_pickup_index: int,
    inventory_index: int,
    cls: str = "RandomizerPowerup",
) -> str:
    """Construct the Lua call that delivers one received item via the
    bootstrap's ``RL.ReceivePickup`` — the idempotent, cutscene-safe path.

    ``RL.ReceivePickup(msg, cls, progression_string, receivedPickupIndex,
    inventoryIndex)`` (bootstrap_part_2.lua) only acts when no pickup is
    pending AND ``receivedPickupIndex == RL.ReceivedPickups()`` and
    ``inventoryIndex == RL.InventoryIndex()`` — the exact-index match dedups
    duplicate/out-of-order sends. It then defers the grant through any cutscene
    (``RL.GivePendingPickup`` reschedules until ``Scenario.IsUserInteractionEnabled``)
    and, on confirm, calls ``cls.OnPickedUp`` then bumps ``ReceivedPickups``.

    So the caller delivers the AP item at position ``received_pickup_index ==
    game's ReceivedPickups`` tagged with the game's current ``inventory_index``;
    the counter advancing (next push) clocks the next delivery. This replaces
    the old ``OnPickedUp``-direct path, which moved ``InventoryIndex`` but never
    ``ReceivedPickups`` (so it was non-idempotent and could drop a mid-cutscene
    grant). See CLAUDE.md risk #1 and [[dread-delivery-protocol]].

    ``progression`` is a list of stages; each stage a list of resource dicts
    ``{"item_id": "ITEM_X", "quantity": N}``. ``cls`` is the Lua pickup class
    (bareword); ``RandomizerPowerup`` is the generic path that grants additive
    resources — per-item classes (input-toggle for Speed Booster / Phantom
    Cloak, progressive beam/missile models) are a follow-up.
    """
    progression_src = _to_lua_table(progression)
    return "RL.ReceivePickup({msg}, {cls}, {prog}, {ri}, {ii})".format(
        msg=_lua_string(message),
        cls=cls,
        prog=_lua_string(progression_src),
        ri=int(received_pickup_index),
        ii=int(inventory_index),
    )


# DeathLink ------------------------------------------------------------------
#
# Blackboard "GAME" property holding the running death count. Official stat on
# game 2.0.0+ (we target 2.1.0); open-dread-rando's death_counter.lua reads the
# same prop. We poll it and edge-detect increments to know the player died.
DEATH_COUNT_PROP = "ProgressStat_PlayerDeaths"


def build_read_death_count_lua() -> str:
    """Lua that returns the current death count as a string (``"0"`` if the
    prop is absent, e.g. at the main menu). Safe to call any time."""
    return (
        f'return tostring(Blackboard.GetProp("GAME", "{DEATH_COUNT_PROP}") or 0)'
    )


def build_kill_player_lua() -> str:
    """Lua that force-kills Samus via the bootstrap's ``RL.KillPlayer`` (defined
    in our non-vendored ``lua/deathlink.lua``). The function self-defers through
    cutscenes and is a no-op outside INGAME, so this is safe to fire any time."""
    return "RL.KillPlayer(); return ''"


def _lua_string(value: str) -> str:
    """Render a Python string as a double-quoted Lua string literal (escaping
    backslashes and quotes — same convention as ``_to_lua_table``)."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _to_lua_table(obj) -> str:
    """Render a Python obj (lists/dicts/scalars) as a Lua table literal."""
    if isinstance(obj, list):
        return "{" + ", ".join(_to_lua_table(x) for x in obj) + "}"
    if isinstance(obj, dict):
        return "{" + ", ".join(
            f"{k}={_to_lua_table(v)}" for k, v in obj.items()
        ) + "}"
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if obj is None:
        return "nil"
    if isinstance(obj, (int, float)):
        return repr(obj)
    if isinstance(obj, str):
        # Lua quoted string with double quotes — escape backslashes and quotes
        return '"' + obj.replace("\\", "\\\\").replace('"', '\\"') + '"'
    raise TypeError(f"can't render {type(obj).__name__} as Lua")
