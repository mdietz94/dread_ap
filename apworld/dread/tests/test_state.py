"""Tests for the Dread BridgeState."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client.state import BridgeState  # noqa: E402
from dread.client.protocol import (  # noqa: E402
    DreadItem, ReceivedItemEvent, CollectedLocationEvent,
)


def _item(name="Missile Tank", pid="ITEM_WEAPON_MISSILE_MAX", qty=2):
    return DreadItem(patcher_item_id=pid, quantity=qty, ap_item_name=name)


def test_initial_state_is_empty():
    s = BridgeState()
    assert s.received_count() == 0
    assert s.all_collected_ids() == set()
    assert s.get_inventory() == {}
    assert s.is_beaten() is False


def test_append_received_increments_count():
    s = BridgeState()
    s.append_received(ReceivedItemEvent(item=_item(), inventory_index=0))
    s.append_received(ReceivedItemEvent(item=_item(qty=10), inventory_index=1))
    assert s.received_count() == 2


def test_mark_collected_dedup():
    s = BridgeState()
    assert s.mark_collected(CollectedLocationEvent(location_id=42)) is True
    assert s.mark_collected(CollectedLocationEvent(location_id=42)) is False
    assert s.mark_collected(CollectedLocationEvent(location_id=43)) is True
    assert s.all_collected_ids() == {42, 43}


def test_clear_received_wipes_per_slot_state():
    s = BridgeState()
    s.append_received(ReceivedItemEvent(item=_item(), inventory_index=0))
    s.mark_collected(CollectedLocationEvent(location_id=1))
    s.set_inventory({"ITEM_WEAPON_MISSILE_MAX": 5})
    s.clear_received()
    assert s.received_count() == 0
    assert s.all_collected_ids() == set()
    assert s.get_inventory() == {}


def test_update_game_state_partial_fields():
    s = BridgeState()
    s.update_game_state(scenario_id="s010_cave")
    s.update_game_state(beaten_since_reboot=True)
    assert s.game_state.scenario_id == "s010_cave"
    assert s.game_state.beaten_since_reboot is True
    assert s.game_state.layout_uuid == ""  # not set, stays default


def test_is_beaten_flips():
    s = BridgeState()
    assert s.is_beaten() is False
    s.update_game_state(beaten_since_reboot=True)
    assert s.is_beaten() is True


def test_log_capped_at_200():
    s = BridgeState()
    for i in range(300):
        s.add_log(f"line {i}")
    assert len(s.last_messages) == 200
    assert s.last_messages[0] == "line 100"


def test_snapshot_shape():
    s = BridgeState()
    s.append_received(ReceivedItemEvent(item=_item(), sender="Other", inventory_index=0))
    s.mark_collected(CollectedLocationEvent(location_id=5))
    s.update_game_state(scenario_id="s010_cave", beaten_since_reboot=True,
                        layout_uuid="abcd")
    snap = s.snapshot()
    assert snap["received_count"] == 1
    assert snap["collected_count"] == 1
    assert snap["scenario"] == "s010_cave"
    assert snap["beaten"] is True
    assert snap["layout_uuid"] == "abcd"
    assert snap["recent_items"][0]["ap_item_name"] == "Missile Tank"
    assert snap["recent_items"][0]["from"] == "Other"
