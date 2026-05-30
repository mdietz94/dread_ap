"""Bridge-side mirror of game state.

Lifted-pattern from smo_archipelago/state.py but dramatically simpler:
Dread doesn't have kingdoms, captures, talkatoo, or multi-Switch routing.

State model:

  * ``received_items`` — diagnostics-only log of confirmed deliveries (appended
    when the game's ``ReceivedPickups`` count advances). NOT a delivery cursor:
    the game's ``ReceivedPickups`` (``game_received_pickups`` below) is the
    authoritative cursor. Delivery goes through ``RL.ReceivePickup``, which is
    idempotent + cutscene-safe by construction (index match + single pending +
    cutscene deferral on the Switch side). See [[dread-delivery-protocol]].

  * ``collected_locations`` — set of AP location_ids the Switch has told
    us were collected. Dedup'd; reconnect snapshot replay re-emits checks
    but they get suppressed here.

  * ``inventory`` — last known per-item amount from a NEW_INVENTORY push.
    Diagnostic only; the authoritative ``ReceivedPickups`` counter is on
    the Switch side.

  * ``game_state`` — last known scenario / mode / goal flag from the
    GAME_STATE push. Used for goal detection.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .protocol import ReceivedItemEvent, CollectedLocationEvent

log = logging.getLogger(__name__)


@dataclass
class DreadGameState:
    scenario_id: str = ""
    game_mode_id: str = ""
    beaten_since_reboot: bool = False
    layout_uuid: str = ""


class BridgeState:
    """Thread-safe snapshot. Read by potential web tracker; mutated by AP +
    Switch loops on the asyncio thread."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

        self.ap_conn: str = "disconnected"
        self.switch_conn: str = "disconnected"
        self.seed: str = ""
        self.slot: str = ""

        self.received_items: list[ReceivedItemEvent] = []
        self.collected_locations: list[CollectedLocationEvent] = []
        self._collected_keys: set[int] = set()

        # The game's two authoritative counters, mirrored from pushes. These
        # ARE the delivery protocol: RL.ReceivePickup only grants a pickup when
        # the (receivedPickupIndex, inventoryIndex) we send match the game's
        # live values, then bumps ReceivedPickups on confirm. So we deliver the
        # AP item at position == game_received_pickups, tagged with the game's
        # current inventory_index, and let the counter advancing (next push)
        # clock the next delivery. Idempotent + cutscene-safe by construction —
        # a client restart reads the real counts and never re-grants.
        #
        # The mirrors track the game's live value, not a high-water mark.
        # Both counters live in Blackboard, which is scenario-scoped: a
        # restart-without-save reverts them to the last save snapshot. We
        # accept the regression as truth and re-deliver from the new lower
        # cursor; the Lua-side index match keeps items the player still has
        # from being re-granted (they index-mismatch and are dropped).
        # See [[dread-delivery-protocol]].
        #   game_received_pickups: Blackboard.ReceivedPickups (PACKET_RECEIVED_PICKUPS)
        #   game_inventory_index : Blackboard.InventoryIndex   (NEW_INVENTORY "index")
        self._game_received_pickups: int = 0
        self._game_inventory_index: int = 0

        self.inventory: dict[str, int] = {}
        self.game_state: DreadGameState = DreadGameState()

        # Short, human-readable patcher-Python status for the GUI panel
        # ("ready (python.exe)" / "not installed — see Archipelago tab").
        self.patcher_python: str = ""

        self.last_messages: list[str] = []  # cap 200, for log surface

    # ---- AP connection state ----

    def set_ap_conn(self, conn: str) -> None:
        with self._lock:
            self.ap_conn = conn

    def set_switch_conn(self, conn: str) -> None:
        with self._lock:
            self.switch_conn = conn

    # ---- Received items (PC → Switch via RL.ReceivePickup) ----

    def append_received(self, evt: ReceivedItemEvent) -> None:
        with self._lock:
            self.received_items.append(evt)

    def received_count(self) -> int:
        with self._lock:
            return len(self.received_items)

    def all_received(self) -> list[ReceivedItemEvent]:
        with self._lock:
            return list(self.received_items)

    def set_game_received_pickups(self, count: int) -> None:
        """Record the game's ReceivedPickups count.

        Tracks the game's live value, not a high-water mark: on a
        restart-without-save the in-game Blackboard reverts to the last save
        snapshot and we need to re-deliver the items that got lost. Lua-side
        index match in RL.ReceivePickup prevents re-granting items the player
        still has."""
        with self._lock:
            if count < self._game_received_pickups:
                log.info(
                    "game ReceivedPickups regressed %d -> %d "
                    "(likely restart without save); re-delivering from %d",
                    self._game_received_pickups, count, count)
            self._game_received_pickups = count

    def game_received_pickups(self) -> int:
        with self._lock:
            return self._game_received_pickups

    def set_game_inventory_index(self, index: int) -> None:
        """Record the game's InventoryIndex.

        Tracks the game's live value, not a high-water mark — see
        ``set_game_received_pickups``."""
        with self._lock:
            if index < self._game_inventory_index:
                log.debug("game InventoryIndex regressed %d -> %d",
                          self._game_inventory_index, index)
            self._game_inventory_index = index

    def game_inventory_index(self) -> int:
        with self._lock:
            return self._game_inventory_index

    def clear_received(self) -> None:
        """Reset the per-slot received-item state.

        Called on slot-change reconnect — same process, different AP slot —
        so a stale mirror from the previous slot doesn't suppress new items
        at duplicated positions (see the SMOContext.clear_received docstring
        for the underlying invariant; same logic applies here).
        """
        with self._lock:
            self.received_items = []
            self.inventory = {}
            self.collected_locations = []
            self._collected_keys = set()
            self._game_received_pickups = 0
            self._game_inventory_index = 0

    # ---- Collected locations (Switch → PC via RL.GetCollectedIndicesAndSend) ----

    def mark_collected(self, evt: CollectedLocationEvent) -> bool:
        """Record a collected location. Returns True if newly seen."""
        with self._lock:
            if evt.location_id in self._collected_keys:
                return False
            self._collected_keys.add(evt.location_id)
            self.collected_locations.append(evt)
            return True

    def all_collected_ids(self) -> set[int]:
        with self._lock:
            return set(self._collected_keys)

    # ---- Inventory snapshot (from PACKET_NEW_INVENTORY) ----

    def set_inventory(self, items: dict[str, int]) -> None:
        with self._lock:
            self.inventory = dict(items)

    def get_inventory(self) -> dict[str, int]:
        with self._lock:
            return dict(self.inventory)

    # ---- Game state (from PACKET_GAME_STATE / API probe) ----

    def update_game_state(
        self,
        *,
        scenario_id: Optional[str] = None,
        game_mode_id: Optional[str] = None,
        beaten_since_reboot: Optional[bool] = None,
        layout_uuid: Optional[str] = None,
    ) -> None:
        with self._lock:
            if scenario_id is not None:
                self.game_state.scenario_id = scenario_id
            if game_mode_id is not None:
                self.game_state.game_mode_id = game_mode_id
            if beaten_since_reboot is not None:
                self.game_state.beaten_since_reboot = beaten_since_reboot
            if layout_uuid is not None:
                self.game_state.layout_uuid = layout_uuid

    def is_beaten(self) -> bool:
        with self._lock:
            return self.game_state.beaten_since_reboot

    # ---- Patcher Python status (for the GUI panel) ----

    def set_patcher_python(self, status: str) -> None:
        with self._lock:
            self.patcher_python = status

    # ---- Log surface ----

    def add_log(self, text: str) -> None:
        with self._lock:
            self.last_messages.append(text)
            if len(self.last_messages) > 200:
                self.last_messages = self.last_messages[-200:]

    # ---- Snapshot for web tracker ----

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "ap_conn": self.ap_conn,
                "switch_conn": self.switch_conn,
                "seed": self.seed,
                "slot": self.slot,
                "received_count": len(self.received_items),
                "game_received_pickups": self._game_received_pickups,
                "game_inventory_index": self._game_inventory_index,
                "collected_count": len(self.collected_locations),
                "inventory": dict(self.inventory),
                "scenario": self.game_state.scenario_id,
                "game_mode": self.game_state.game_mode_id,
                "beaten": self.game_state.beaten_since_reboot,
                "layout_uuid": self.game_state.layout_uuid,
                "patcher_python": self.patcher_python,
                "recent_messages": list(self.last_messages[-50:]),
                "recent_items": [
                    {
                        "patcher_item_id": e.item.patcher_item_id,
                        "ap_item_name": e.item.ap_item_name,
                        "quantity": e.item.quantity,
                        "from": e.sender,
                        "at_ms": e.received_at_ms,
                    }
                    for e in self.received_items[-50:]
                ],
            }
