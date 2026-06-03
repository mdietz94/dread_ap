"""Unit tests for scripts/extract_dread_data.py — specifically the
``_resource_to_ap_name`` resolver, which maps a pickup's vanilla resource
list to an AP item name.

The resolver used to silently fall back to "Missile Tank" for any resource it
couldn't map, which is how a granted-but-unmodeled resource (Super Missile)
disappeared without a trace. These tests pin the hardened contract: real
unmapped resources raise; only explicit sentinels get the neutral label.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from extract_dread_data import _resource_to_ap_name  # noqa: E402


def _res(*item_ids: str, quantity: int = 1) -> list[list[dict]]:
    """Build a resource list from one item id per progressive stage."""
    return [[{"item_id": pid, "quantity": quantity}] for pid in item_ids]


def test_flat_pickup_resolves_to_its_item():
    assert _resource_to_ap_name(_res("ITEM_WEAPON_SUPER_MISSILE")) == "Super Missile"


def test_progressive_pickup_labels_by_first_stage():
    # [[Super], [Ice]] — a fresh pickup grants Super, so that's the label.
    assert _resource_to_ap_name(
        _res("ITEM_WEAPON_SUPER_MISSILE", "ITEM_WEAPON_ICE_MISSILE")
    ) == "Super Missile"


def test_quantity_disambiguates_shared_patcher_id():
    # Missile Tank (qty 2) and Missile+ Tank (qty 10) share ITEM_WEAPON_MISSILE_MAX.
    assert _resource_to_ap_name(
        _res("ITEM_WEAPON_MISSILE_MAX", quantity=2)) == "Missile Tank"
    assert _resource_to_ap_name(
        _res("ITEM_WEAPON_MISSILE_MAX", quantity=10)) == "Missile+ Tank"


def test_dna_artifact_resolves_by_index():
    assert _resource_to_ap_name(_res("ITEM_RANDO_ARTIFACT_7")) == "Metroid DNA 7"


def test_sentinel_and_empty_get_neutral_label():
    assert _resource_to_ap_name(_res("ITEM_NONE")) == "Missile Tank"
    assert _resource_to_ap_name([]) == "Missile Tank"
    assert _resource_to_ap_name([[]]) == "Missile Tank"


def test_unmapped_real_resource_raises_loudly():
    """The whole point of the hardening: a granted resource with no pool
    entry must fail, not silently degrade to a filler label."""
    with pytest.raises(ValueError, match="ITEM_WEAPON_MADE_UP"):
        _resource_to_ap_name(_res("ITEM_WEAPON_MADE_UP"))
