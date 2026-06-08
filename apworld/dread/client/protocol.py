"""Dread-side protocol dataclasses — small, replaces SMO's 600-line equivalent.

This is NOT the wire format; that lives in ``wire.py``. These are
the *semantic* item / location records that the rest of the client
shuffles around: a friendly normalization of what comes out of AP and
what we send into Lua via ``RL.ReceivePickup``.
"""
from __future__ import annotations

import re
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

    For Randovania-style PROGRESSIVE items, ``progression_stages`` carries the
    full multi-stage progression (a list of stages, each a list of
    ``{"item_id", "quantity"}`` dicts) and ``pickup_cls`` the Lua class to run
    (the first tier's specific class). They are ``None`` for ordinary items,
    which derive both from ``patcher_item_id`` at delivery time. The game grants
    the next *missing* tier from the full progression, so the same progression
    is sent on every delivery (no client-side tier counting) — see
    ``DreadContext._attempt_delivery``.
    """
    patcher_item_id: str
    quantity: int
    ap_item_name: str = ""
    progression_stages: Optional[list] = None
    pickup_cls: Optional[str] = None


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

    Most items are a single resource. Three pickups grant a paired resource:

    * **Main Power Bomb** must grant the weapon-unlock flag
      (``ITEM_WEAPON_POWER_BOMB``) AND the ammo capacity
      (``ITEM_WEAPON_POWER_BOMB_MAX``) together. Granting only
      ``ITEM_WEAPON_POWER_BOMB`` unlocks the weapon but leaves the player at 0/0
      capacity — ``RandomizerPowerup.IncreaseAmmo`` only feeds
      ``ITEM_WEAPON_POWER_BOMB_CURRENT`` off a ``..._MAX`` grant, never off the
      bare unlock item — so power bombs are unusable and the HUD/menu shows
      nothing. This mirrors open-dread-rando's canonical ``schema.json`` example
      (``{POWER_BOMB:1}`` + ``{POWER_BOMB_MAX:N}``). The pickup's ``quantity`` is
      the starting capacity ``N``.
    * **Main Flash Shift / Speed Booster** bake their "from main" chain / charge
      upgrades into the main pickup (see below). Here ``quantity`` is the
      chain/charge count."""
    if patcher_item_id == "ITEM_WEAPON_POWER_BOMB":
        return [
            {"item_id": "ITEM_WEAPON_POWER_BOMB", "quantity": 1},
            {"item_id": "ITEM_WEAPON_POWER_BOMB_MAX", "quantity": int(quantity)},
        ]
    # Main Flash Shift / Speed Booster: grant the ability-unlock flag plus the
    # chained-dash / charge capacity baked into the main pickup (Randovania's
    # "included" amount). Here ``quantity`` is the chain/charge count — the
    # "… From Main" option, NOT a copy count — so the main pickup alone satisfies
    # the access-logic chain gates with no upgrades shuffled. This mirrors how
    # upstream bundles the included FlashUpgrade / SpeedBoostUpgrade resources
    # into the main pickup: the unlock-flag class (RandomizerFlashShift /
    # RandomizerSpeedBooster) grants both on its first (only) pickup — the
    # "already has Flash Shift" branch that zeros chain resources never fires for
    # the main's own grant. Quantity 0 → ability only (no chains baked in).
    if patcher_item_id == "ITEM_GHOST_AURA":
        stage = [{"item_id": "ITEM_GHOST_AURA", "quantity": 1}]
        if int(quantity) > 0:
            stage.append({"item_id": "ITEM_UPGRADE_FLASH_SHIFT_CHAIN",
                          "quantity": int(quantity)})
        return stage
    if patcher_item_id == "ITEM_SPEED_BOOSTER":
        stage = [{"item_id": "ITEM_SPEED_BOOSTER", "quantity": 1}]
        if int(quantity) > 0:
            stage.append({"item_id": "ITEM_UPGRADE_SPEED_BOOST_CHARGE",
                          "quantity": int(quantity)})
        return stage
    return [{"item_id": patcher_item_id, "quantity": int(quantity)}]


def build_receive_pickup_lua(
    *,
    message: str,
    progression: list[list[dict]],
    received_pickup_index: int,
    inventory_index: int,
    cls: str = "RandomizerPowerup",
    popup_seconds: float | None = None,
    reschedule_seconds: float | None = None,
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
    resources, while per-item classes do the input-toggle / weapon setup
    (Speed Booster, Phantom Cloak, the beam/missile classes). For PROGRESSIVE
    items the caller sends the FULL multi-stage progression with the first
    tier's class — the game's ``HandlePickupResources`` grants the next missing
    stage, so the same progression is re-sent on every delivery (see
    ``DreadContext._attempt_delivery``).

    ``popup_seconds`` / ``reschedule_seconds`` are optional overrides for the
    on-Switch popup display duration and the delay before the bootstrap clears
    ``PendingPickup`` and re-sends its counters (which clocks the *next*
    delivery). Omitting both keeps the bootstrap's lone-item defaults
    (7.0s / 7.5s). The client passes short values while a backlog drains (an AP
    "release") so items aren't gated to one every ~7.5s. See ``_attempt_delivery``.
    """
    progression_src = _to_lua_table(progression)
    extra = ""
    if popup_seconds is not None or reschedule_seconds is not None:
        # Default either omitted value to the bootstrap's lone-item constant so
        # a partial override still produces a valid 7-arg call.
        popup = 7.0 if popup_seconds is None else float(popup_seconds)
        delay = 7.5 if reschedule_seconds is None else float(reschedule_seconds)
        extra = ", {p}, {d}".format(p=_lua_number(popup), d=_lua_number(delay))
    return "RL.ReceivePickup({msg}, {cls}, {prog}, {ri}, {ii}{extra})".format(
        msg=_lua_string(message),
        cls=cls,
        prog=_lua_string(progression_src),
        ri=int(received_pickup_index),
        ii=int(inventory_index),
        extra=extra,
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


# Boss-arena warp guard ------------------------------------------------------
#
# Warping out of a boss arena with ``Game.LoadScenario`` corrupts the encounter:
# the arena's intro / door-lock / checkpoint state half-commits, so the fight
# can't be re-entered or reset cleanly. A user hit exactly this with Kraid —
# after a mid-fight /warp the fight couldn't be re-entered the normal way, broke
# when entered from the exit, and bricked the game on the death-respawn. So
# /warp is BLOCKED while Samus's current subarea (collision camera) is a
# boss-fight room. The engine exposes no getter for the live collision camera,
# but fires ``Scenario.OnSubAreaChange`` on every transition — ``lua/warp_guard.lua``
# wraps it to record the live scenario+subarea, and the warp src calls
# ``RL.IsInBossArena()`` before firing.
#
# Keys are scenario ids; each maps a collision-camera id (the same id the engine
# passes to ``OnSubAreaChange`` and that the patcher's camera-names dict keys on)
# to its human room name. Only the key set is load-bearing — the name is a
# comment for reviewers. Curated from the published room-name table. EMMI zones
# are deliberately EXCLUDED: you can run from an EMMI and a /warp there is
# legitimate softlock recovery, not a corrupting mid-fight bail. Only set-piece
# bosses, Central Units, and Chozo-warrior/robot arenas — the rooms that lock
# you in and set a fight checkpoint — are listed.
BOSS_ARENA_CAMERAS: dict[str, dict[str, str]] = {
    "s010_cave": {
        "collision_camera_020": "Corpius Arena",
        "collision_camera_090": "Central Unit",
        "collision_camera_091": "Red Chozo Arena",
    },
    "s020_magma": {
        "collision_camera_063": "Kraid Arena",
        "collision_camera_037": "Central Unit",
        "collision_camera_CooldownX": "Experiment Z-57 Fight",
    },
    "s030_baselab": {
        "collision_camera_038": "Central Unit",
    },
    "s040_aqua": {
        "collision_camera_028": "Drogyga Arena",
    },
    "s050_forest": {
        "collision_camera_002": "Robot Fight Arena",
        "collision_camera_023": "Chozo Warrior Arena",
        "collision_camera_026": "Golzuna Arena",
        "collision_camera_017": "Central Unit",
    },
    "s060_quarantine": {
        "collision_camera_004": "Chozo Soldier Arena",
    },
    "s070_basesanc": {
        "collision_camera_017": "Twin Robot Arena",
        "collision_camera_027": "Escue Arena",
        "collision_camera_035": "Central Unit",
    },
    "s080_shipyard": {
        "collision_camera_005": "Gold Chozo Warrior Arena",
        "collision_camera_012": "Central Unit",
        "collision_camera_020": "Raven Beak X Arena",
    },
    "s090_skybase": {
        "collision_camera_004": "Raven Beak Arena",
    },
}

# Navigation ("Adam") rooms — the same per-scenario collision-camera map shape as
# BOSS_ARENA_CAMERAS, used by the same ``RL.IsInNavRoom()`` warp guard. Why block
# /warp here: a Navigation-room conversation with Adam keeps Samus controllable
# (interaction stays "enabled") yet renders a dialogue box that ``Game.LoadScenario``
# does NOT tear down — warping mid-conversation strands an undismissable text box
# (the reported bug). And unlike a softlock, a Nav room is a safe hub you can just
# walk out of, so refusing to warp there costs the player nothing.
#
# Detection has NO state flag: probing the live game during an Adam talk showed
# GetCurrentCutsceneStr()=nil, Scenario.ShowingPopup=false, IsUserInteractionEnabled
# (true)=true, and the box is not a reachable IngameMenuRoot GUI composition — so
# location is the only viable signal, exactly like boss arenas. Keys are scenario
# ids; values map a collision-camera id (the id ``OnSubAreaChange`` reports, tracked
# by lua/warp_guard.lua) to its room name. Curated from the patcher's published
# ``camera_names_dict`` (every "Navigation Station ..." entry). ``*_Access`` is the
# approach corridor, included because it's an equally-safe over-block.
NAV_ROOM_CAMERAS: dict[str, dict[str, str]] = {
    "s010_cave": {
        "collision_camera_065": "Navigation Station North",
        "collision_camera_068": "Navigation Station South",
    },
    "s020_magma": {
        "collision_camera_002": "Navigation Station Southeast",
        "collision_camera_058": "Navigation Station Northwest",
    },
    "s030_baselab": {
        "collision_camera_014": "Navigation Station South",
        "collision_camera_044": "Navigation Station North",
    },
    "s040_aqua": {
        "collision_camera_009": "Navigation Station North",
        "collision_camera_016": "Navigation Station South",
    },
    "s050_forest": {
        "collision_camera_005": "Navigation Station Access",
        "collision_camera_006": "Navigation Station",
    },
    "s070_basesanc": {
        "collision_camera_016": "Navigation Station",
    },
    "s080_shipyard": {
        "collision_camera_003": "Navigation Station",
    },
}

# Save Stations — blocked from /warp for the same reason as Nav rooms: the save
# dialog is a controllable-but-overlaid box that Game.LoadScenario won't dismiss,
# and a save room is a safe hub (just save + reload if stuck) where /warp is never
# needed. Same per-scenario collision-camera shape, same RL warp guard, same
# OnSubAreaChange tracking. Curated from the patcher's ``camera_names_dict`` (every
# "Save Station ..." entry). Verified disjoint from BOSS_ARENA_CAMERAS and
# NAV_ROOM_CAMERAS within each scenario (a camera id reused across DIFFERENT
# scenarios is fine — the guard keys on the live scenario).
SAVE_STATION_CAMERAS: dict[str, dict[str, str]] = {
    "s010_cave": {
        "collision_camera_012": "Save Station West",
        "collision_camera_025": "Save Station East",
        "collision_camera_063": "Save Station South",
        "collision_camera_076": "Save Station North",
    },
    "s020_magma": {
        "collision_camera_033": "Save Station East",
        "collision_camera_062": "Save Station West",
    },
    "s030_baselab": {
        "collision_camera_000": "Save Station East",
        "collision_camera_031": "Save Station West Tunnels",
        "collision_camera_032": "Save Station West",
    },
    "s040_aqua": {
        "collision_camera_011": "Save Station Middle",
        "collision_camera_026": "Save Station South Access",
        "collision_camera_027": "Save Station South",
        "collision_camera_030": "Save Station North",
    },
    "s050_forest": {
        "collision_camera_022": "Save Station Center",
        "collision_camera_040": "Save Station East",
    },
    "s060_quarantine": {
        "collision_camera_012": "Save Station",
    },
    "s070_basesanc": {
        "collision_camera_029": "Save Station North",
        "collision_camera_041": "Save Station Southeast",
    },
    "s090_skybase": {
        "collision_camera_000": "Save Station",
    },
}

# Lua bareword key pattern — scenario ids and collision-camera ids are emitted
# as table keys (`{s020_magma={collision_camera_063=true}}`), so both must be
# valid identifiers or the rendered Lua is malformed.
_LUA_BAREWORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")


def _build_camera_table_lua(cameras: dict[str, dict[str, str]], what: str) -> str:
    """Render a per-scenario collision-camera map as the nested Lua table literal
    the warp guard consumes, e.g. ``{s020_magma={collision_camera_063=true},...}``.
    Camera ids are valid Lua barewords, so they become keys with value ``true``.
    ``what`` names the table for the malformed-key error."""
    scen_parts = []
    for scenario, cams in cameras.items():
        for key in (scenario, *cams):
            if not _LUA_BAREWORD.match(key):
                raise ValueError(
                    f"{what} key {key!r} is not a valid Lua bareword; "
                    "the rendered warp-guard table would be malformed"
                )
        cam_parts = ",".join(f"{cam}=true" for cam in cams)
        scen_parts.append(f"{scenario}={{{cam_parts}}}")
    return "{" + ",".join(scen_parts) + "}"


def build_boss_arenas_lua_table() -> str:
    """Render :data:`BOSS_ARENA_CAMERAS` as the ``RL.BossArenas`` table literal
    (emitted as its own bootstrap block, read by ``lua/warp_guard.lua``)."""
    return _build_camera_table_lua(BOSS_ARENA_CAMERAS, "boss-arena")


def build_nav_rooms_lua_table() -> str:
    """Render :data:`NAV_ROOM_CAMERAS` as the ``RL.NavRooms`` table literal
    (emitted as its own bootstrap block)."""
    return _build_camera_table_lua(NAV_ROOM_CAMERAS, "nav-room")


def build_save_rooms_lua_table() -> str:
    """Render :data:`SAVE_STATION_CAMERAS` as the ``RL.SaveRooms`` table literal
    (emitted as its own bootstrap block)."""
    return _build_camera_table_lua(SAVE_STATION_CAMERAS, "save-room")


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


def _lua_number(value: float) -> str:
    """Render a number as a plain Lua numeric literal (no scientific notation,
    no trailing-zero noise) — used for the popup/reschedule second overrides."""
    return ("%g" % float(value))


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
