"""Bridge-side mirror of game state.

Lifted-pattern from smo_archipelago/state.py but dramatically simpler:
Dread doesn't have kingdoms, captures, talkatoo, or multi-Switch routing.

State model:

  * ``received_items`` — append-only log of every AP item we've delivered
    to the Switch (via ``RandomizerPowerup.OnPickedUp``). Its length is the
    PC-side delivery cursor: ``_on_received_items`` only delivers items at
    positions >= this length.

    CAUTION: delivery is NOT idempotent today — ``build_receive_pickup_lua``
    calls ``OnPickedUp`` directly and the ``inventory_index`` arg is a no-op,
    so re-sending an item RE-GRANTS it (additive items like Missile/Energy/
    Power Bomb tanks would double). The cursor advances on SEND, not on the
    Switch confirming receipt, so an item dropped mid-cutscene is lost rather
    than retried. A cutscene-safe replay requires first making delivery
    idempotent — gate sends on the Switch's real ``Blackboard.ReceivedPickups``
    count (consume ``PACKET_RECEIVED_PICKUPS``, currently ignored) — and a
    hardware test. See the risk note in CLAUDE.md.

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

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .protocol import ReceivedItemEvent, CollectedLocationEvent


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

        self.inventory: dict[str, int] = {}
        self.game_state: DreadGameState = DreadGameState()

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
                "collected_count": len(self.collected_locations),
                "inventory": dict(self.inventory),
                "scenario": self.game_state.scenario_id,
                "game_mode": self.game_state.game_mode_id,
                "beaten": self.game_state.beaten_since_reboot,
                "layout_uuid": self.game_state.layout_uuid,
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
