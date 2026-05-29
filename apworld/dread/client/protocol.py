"""Dread-side protocol dataclasses — small, replaces SMO's 600-line equivalent.

This is NOT the wire format; that lives in ``lua_packets.py``. These are
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
