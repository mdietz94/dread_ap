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


def pickup_resource_stage(patcher_item_id: str, quantity: int) -> list[dict]:
    """Expand one ``(item_id, quantity)`` into the resource stage the game
    grants for that pickup (a list of ``{"item_id", "quantity"}`` dicts).

    Most items are a single resource. The **Main Power Bomb** is the lone
    exception: it must grant the weapon-unlock flag (``ITEM_WEAPON_POWER_BOMB``)
    AND the ammo capacity (``ITEM_WEAPON_POWER_BOMB_MAX``) together. Granting
    only ``ITEM_WEAPON_POWER_BOMB`` unlocks the weapon but leaves the player at
    0/0 capacity — ``RandomizerPowerup.IncreaseAmmo`` only feeds
    ``ITEM_WEAPON_POWER_BOMB_CURRENT`` off a ``..._MAX`` grant, never off the
    bare unlock item — so power bombs are unusable and the HUD/menu shows
    nothing. This mirrors open-dread-rando's canonical ``schema.json`` example
    (``{POWER_BOMB:1}`` + ``{POWER_BOMB_MAX:N}``). The pickup's ``quantity`` is
    the starting capacity ``N``."""
    if patcher_item_id == "ITEM_WEAPON_POWER_BOMB":
        return [
            {"item_id": "ITEM_WEAPON_POWER_BOMB", "quantity": 1},
            {"item_id": "ITEM_WEAPON_POWER_BOMB_MAX", "quantity": int(quantity)},
        ]
    return [{"item_id": patcher_item_id, "quantity": int(quantity)}]


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


# Warp inventory rewind ------------------------------------------------------
#
# ``/warp`` issues ``Game.LoadScenario`` from anywhere in the world, which
# reloads Samus from the last SAVE (not checkpoint-continuously like a door
# transition). That reverts every pickup collected since the last save. The
# client snapshots inventory across the warp and re-grants the delta; these
# three helpers are the Lua primitives for that. See
# ``DreadContext._warp_to_start`` / ``_restore_after_warp``.


def build_read_inventory_amounts_lua() -> str:
    """Read live per-item amounts straight from the game as a self-describing
    ``ITEM_A=amount;ITEM_B=amount;...`` string (returned via the run_lua reply,
    not the async NEW_INVENTORY push). Keyed by ``ITEM_*`` id so the caller can
    diff before/after a warp without depending on ``RL.InventoryItems`` order."""
    return (
        "local r={} "
        "for i,n in ipairs(RL.InventoryItems) do "
        'r[#r+1]=n.."="..tostring(RandomizerPowerup.GetItemAmount(n)) '
        "end "
        'return table.concat(r,";")'
    )


def build_read_received_pickups_lua() -> str:
    """Read the game's ``ReceivedPickups`` delivery cursor as a string."""
    return "return tostring(RL.ReceivedPickups())"


def build_restore_grant_lua(item_id: str, quantity: int,
                            cls: str = "RandomizerPowerup") -> str:
    """Directly re-grant ``quantity`` of ``item_id`` by calling the item's
    ``OnPickedUp`` (the same call ``RL.ConfirmPickup`` makes). This is the
    *non*-idempotent additive grant — used only to rewind a warp revert, where
    we've already diffed exactly how much was lost, so no index gating is
    wanted. It does NOT bump ``ReceivedPickups`` (these aren't AP deliveries)."""
    progression = [[{"item_id": item_id, "quantity": int(quantity)}]]
    return "{cls}.OnPickedUp(nil, {prog}); return ''".format(
        cls=cls, prog=_to_lua_table(progression))


def build_read_collected_indices_lua() -> str:
    """Read the set of pickup indices currently marked collected, as a
    comma-separated string (``"0,5,7"``). Mirrors ``GetCollectedIndicesAndSend``
    but returns the indices directly so the caller can snapshot them before a
    warp. ``RL.Pickups[i]`` holds the ``Location_Collected_*`` prop name (set by
    the bootstrap; it lives in the Lua VM and is NOT reverted by LoadScenario)."""
    return (
        "-- RL_READ_COLLECTED\n"
        "local p=Game.GetPlayerBlackboardSectionName() "
        "local r={} "
        "for i,t in ipairs(RL.Pickups) do "
        "if t~='' and Blackboard.GetProp(p,t) then r[#r+1]=i-1 end "
        "end "
        'return table.concat(r,",")'
    )


def build_mark_collected_lua(pickup_indices: list[int]) -> str:
    """Re-assert the ``Location_Collected_*`` prop for each pickup index — the
    same ``Blackboard.SetProp(playerSection, prop, "b", true)`` the game's
    ``MarkLocationCollected`` does. After a warp reverts the collected bits, this
    restores them so the pickup actors don't respawn (and can't be re-collected
    for a duplicate) when the player returns to that scenario. ``OnPickedUp``
    itself does NOT gate on the prop, so re-asserting is what prevents the dupe —
    by stopping the respawn at the next scenario load."""
    idx_list = "{" + ",".join(str(int(i)) for i in pickup_indices) + "}"
    return (
        "-- RL_MARK_COLLECTED\n"
        "local p=Game.GetPlayerBlackboardSectionName() "
        "for _,idx in ipairs(" + idx_list + ") do "
        "local t=RL.Pickups[idx+1] "
        'if t and t~="" then Blackboard.SetProp(p,t,"b",true) end '
        "end "
        "return ''"
    )


def build_set_received_pickups_lua(count: int) -> str:
    """Rewrite the game's ``ReceivedPickups`` Blackboard prop to ``count`` —
    the same write ``RL.ConfirmPickup`` does, used to rewind the delivery cursor
    to its pre-warp value so remote items restored by ``build_restore_grant_lua``
    are not also re-delivered by ``_attempt_delivery``."""
    return ('Scenario.WriteToPlayerBlackboard("ReceivedPickups","f",{}); '
            "return ''").format(int(count))


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
