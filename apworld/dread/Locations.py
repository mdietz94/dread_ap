"""Location table loader.

Loads ``data/locations.json`` and exposes the canonical lookup tables.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from BaseClasses import Location

from ._data_loader import load_json


@dataclass(frozen=True)
class DreadLocationData:
    name: str
    ap_id: int
    region: str
    scenario: str
    actor: str
    pickup_type: str
    vanilla_item: str
    # Position in the Randovania starter-preset patcher template's `pickups`
    # array. None for synthetic event locations (they have no Switch-side
    # counterpart). The runtime wire uses this to translate Switch-reported
    # collected-indices bitfields into AP location_ids.
    pickup_index: Optional[int] = None


class DreadLocation(Location):
    game = "Metroid Dread"


def _load() -> list[DreadLocationData]:
    return [DreadLocationData(**entry) for entry in load_json("locations.json")]


location_table: list[DreadLocationData] = _load()

location_id_to_name: dict[int, str] = {l.ap_id: l.name for l in location_table}
location_name_to_id: dict[str, int] = {l.name: l.ap_id for l in location_table}
location_name_to_location: dict[str, DreadLocationData] = {l.name: l for l in location_table}


def locations_in_region(region_name: str) -> list[DreadLocationData]:
    return [l for l in location_table if l.region == region_name]
