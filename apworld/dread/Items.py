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
    quantity: int
    classification: str  # "progression" | "useful" | "filler"


class DreadItem(Item):
    game = "Metroid Dread"


_CLASSIFICATION_MAP = {
    "progression": ItemClassification.progression,
    "progression_skip_balancing": ItemClassification.progression_skip_balancing,
    "useful": ItemClassification.useful,
    "filler": ItemClassification.filler,
    "trap": ItemClassification.trap,
}


def _load() -> list[DreadItemData]:
    return [DreadItemData(**entry) for entry in load_json("items.json")]


item_table: list[DreadItemData] = _load()

item_id_to_name: dict[int, str] = {it.ap_id: it.name for it in item_table}
item_name_to_id: dict[str, int] = {it.name: it.ap_id for it in item_table}
item_name_to_item: dict[str, DreadItemData] = {it.name: it for it in item_table}


def get_item_classification(item_name: str) -> ItemClassification:
    item = item_name_to_item.get(item_name)
    if item is None:
        return ItemClassification.filler
    return _CLASSIFICATION_MAP.get(item.classification, ItemClassification.filler)
