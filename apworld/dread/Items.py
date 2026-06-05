"""Item table loader + DreadItem class.

Loads ``data/items.json`` (produced by scripts/extract_dread_data.py) and
exposes the canonical lookup tables Archipelago expects.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from BaseClasses import Item, ItemClassification

from ._data_loader import load_json


@dataclass(frozen=True)
class DreadItemData:
    name: str
    ap_id: int
    patcher_item_id: str
    quantity: int  # capacity-per-pickup granted by the patcher
    pool_count: int  # default copies in the AP pool (overridable via Options)
    classification: str  # "progression" | "progression_skip_balancing" | "useful" | "filler"
    # open-dread-rando model name for the in-world pickup sphere. Authoritative
    # source: Randovania's dread pickup-database ``model_name``. Names absent
    # from the pinned patcher's ``ALL_MODEL_DATA`` (Slide, DNA) degrade to
    # ``itemsphere`` via ``model_data.get_data``'s fallback. Defaulted so older
    # data files (and tests) without the field still load.
    model_name: str = ""
    # For Randovania-style PROGRESSIVE items (Progressive Beam, Progressive
    # Suit, ...): the ordered component item names whose tiers this item grants
    # on successive collections (e.g. ("Wide Beam", "Plasma Beam", "Wave
    # Beam")). Empty for ordinary items. ``patcher_item_id`` / ``model_name``
    # are blank on a progressive entry — its patcher resources, in-world models,
    # and AP-logic atoms all derive from the referenced tier entries. See
    # World.create_items (pool swap), World.collect/remove (logic translation),
    # and World._build_placements_payload (multi-stage patcher output).
    progression_tiers: tuple[str, ...] = ()


class DreadItem(Item):
    game = "Metroid Dread"


CLASSIFICATION_MAP = {
    "progression": ItemClassification.progression,
    "progression_skip_balancing": ItemClassification.progression_skip_balancing,
    "useful": ItemClassification.useful,
    "filler": ItemClassification.filler,
    "trap": ItemClassification.trap,
}
_CLASSIFICATION_MAP = CLASSIFICATION_MAP  # legacy alias


def _load() -> list[DreadItemData]:
    out: list[DreadItemData] = []
    for entry in load_json("items.json"):
        e = dict(entry)
        # JSON arrays load as lists; coerce so the frozen dataclass stays
        # hashable and tier lookups compare as tuples.
        if "progression_tiers" in e:
            e["progression_tiers"] = tuple(e["progression_tiers"])
        out.append(DreadItemData(**e))
    return out


item_table: list[DreadItemData] = _load()

item_id_to_name: dict[int, str] = {it.ap_id: it.name for it in item_table}
item_name_to_id: dict[str, int] = {it.name: it.ap_id for it in item_table}
item_name_to_item: dict[str, DreadItemData] = {it.name: it for it in item_table}


# ---------------------------------------------------------------------------
# Progressive item groups (mirror Randovania's Dread progressives). Each group
# is one AP item carrying `pool_count` copies; collecting the k-th copy grants
# the k-th tier. These tables are the single source of truth shared by Options
# (one toggle per group), World (pool swap + collect/remove logic translation +
# patcher payload), and the client (multi-stage delivery).
# ---------------------------------------------------------------------------

# DreadOptions field name → progressive item name.
PROGRESSIVE_GROUPS: dict[str, str] = {
    "progressive_suit": "Progressive Suit",
    "progressive_spin": "Progressive Spin",
    "progressive_charge_beam": "Progressive Charge Beam",
    "progressive_beam": "Progressive Beam",
    "progressive_missile": "Progressive Missile",
    "progressive_bomb": "Progressive Bomb",
}

# Progressive item name → ordered component (tier) item names. Derived from the
# items.json `progression_tiers` so the data lives in one place.
PROGRESSIVE_TIERS: dict[str, tuple[str, ...]] = {
    it.name: tuple(it.progression_tiers)
    for it in item_table if it.progression_tiers
}

# Progressive item name → the map-screen icon id baked in the starter preset
# (note "Progressive Charge Beam" → PROGRESSIVE_CHARGE, not _CHARGE_BEAM).
PROGRESSIVE_MAP_ICON: dict[str, str] = {
    "Progressive Suit": "PROGRESSIVE_SUIT",
    "Progressive Spin": "PROGRESSIVE_SPIN",
    "Progressive Charge Beam": "PROGRESSIVE_CHARGE",
    "Progressive Beam": "PROGRESSIVE_BEAM",
    "Progressive Missile": "PROGRESSIVE_MISSILE",
    "Progressive Bomb": "PROGRESSIVE_BOMB",
}


def get_item_classification(item_name: str) -> ItemClassification:
    item = item_name_to_item.get(item_name)
    if item is None:
        return ItemClassification.filler
    return _CLASSIFICATION_MAP.get(item.classification, ItemClassification.filler)
