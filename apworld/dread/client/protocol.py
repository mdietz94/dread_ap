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
    parent_ref: int,
    progression: list[list[dict]],
    num_pickups: int,
    inventory_index: int,
) -> str:
    """Construct the Lua snippet that grants one received item.

    Calls ``RandomizerPowerup.OnPickedUp(nil, <resources>)`` against the
    deployed patcher Lua — that handles the full ``HandlePickupResources``
    + post-grant flow (energy/ammo/artifact recompute, weapon update,
    inventory index bump, RL.UpdateRDVClient tick that triggers our
    telemetry heartbeat). Passing ``nil`` for the actor skips the
    ``MarkLocationCollected`` branch — we did NOT visit a pickup pedestal,
    so we shouldn't fake a location-collected blackboard prop.

    Why not ``RL.ReceivePickup``? Older Randovania bootstrap Lua exposed
    that function, but current upstream ``open-dread-rando-exlaunch``
    doesn't define it — only Send* push primitives plus
    ``RandomizerPowerup`` itself. CLAUDE.md's wire-protocol notes
    documented the old API; this is the corrected path.

    ``progression`` is a list of stages; each stage is a list of resource
    dicts ``{"item_id": "ITEM_X", "quantity": N}``. For a non-progressive
    item, ``[[{"item_id": "...", "quantity": 1}]]``.

    ``message``, ``parent_ref``, ``num_pickups``, ``inventory_index`` are
    no-ops in the current implementation — kept in the signature so the
    context.py call site doesn't need to change every time we revise the
    Lua contract. The ``message`` is surfaced via ``Game.LogWarn`` (which
    routes back to PC as a PACKET_LOG_MESSAGE push for client visibility);
    the inventory index gets bumped by RandomizerPowerup itself.

    NOT IDEMPOTENT. Because ``inventory_index`` is ignored, calling this twice
    for the same item grants it twice — and for additive items (Missile /
    Energy / Power Bomb tanks) that means inflated capacity, not a no-op. Do
    NOT build a reconnect/post-cutscene replay on top of this as-is. A safe
    replay first needs index-gated delivery: have the caller consult the
    Switch's real ``Blackboard.ReceivedPickups`` count (the
    ``PACKET_RECEIVED_PICKUPS`` push, currently ignored in context.py) and skip
    any item the game has already applied — matching Randovania's
    ``dread_remote_connector``. See CLAUDE.md risk #1 and client/state.py.
    """
    import json
    progression_lua = _to_lua_table(progression)
    # Lua block: log the "received from" message (PC will see it as a log
    # push), then run the full pickup flow. Wrap in a do-end so the
    # statement can be sent as a single Lua-exec call.
    return (
        f"do "
        f"Game.LogWarn(0, {json.dumps(message)}); "
        f"RandomizerPowerup.OnPickedUp(nil, {progression_lua}); "
        f"end"
    )


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
