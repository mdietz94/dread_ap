"""AP id ↔ name + AP item ↔ Dread patcher item_id mapping.

This is Dread's analogue of smo_archipelago's datapackage.py + maps.py.
Smaller because Dread doesn't have the SMO complexity (per-kingdom moon
buckets, capture-name translation, talkatoo pool).

The mapping is small enough to ship as a literal Python dict for now;
Phase 4 will populate it from the apworld's items.json + locations.json.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .protocol import DreadItem, DreadPickupLocation

try:
    # Available when the apworld is importable (i.e. installed under
    # worlds/dread/ or as a .apworld zip in custom_worlds/).
    from .._data_loader import load_json as _load_json
except ImportError:  # pragma: no cover — defensive
    _load_json = None

log = logging.getLogger(__name__)


# AP item name → (patcher_item_id, default_quantity). Populated by
# Phase 4 from data/items.json. The starter list here is a stub.
DEFAULT_AP_TO_PATCHER: dict[str, tuple[str, int]] = {
    "Missile Tank": ("ITEM_WEAPON_MISSILE_MAX", 2),
    "Missile+ Tank": ("ITEM_WEAPON_MISSILE_MAX", 10),
    "Power Bomb Tank": ("ITEM_WEAPON_POWER_BOMB_MAX", 2),
    "Energy Tank": ("ITEM_ENERGY_TANKS", 1),
    "Energy Part": ("ITEM_LIFE_SHARDS", 1),
    "Morph Ball": ("ITEM_MORPH_BALL", 1),
    "Bomb": ("ITEM_WEAPON_BOMB", 1),
    "Cross Bomb": ("ITEM_WEAPON_LINE_BOMB", 1),
    "Power Bomb": ("ITEM_WEAPON_POWER_BOMB", 1),
    "Wide Beam": ("ITEM_WEAPON_WIDE_BEAM", 1),
    "Plasma Beam": ("ITEM_WEAPON_PLASMA_BEAM", 1),
    "Wave Beam": ("ITEM_WEAPON_WAVE_BEAM", 1),
    "Charge Beam": ("ITEM_WEAPON_CHARGE_BEAM", 1),
    "Diffusion Beam": ("ITEM_WEAPON_DIFFUSION_BEAM", 1),
    "Grapple Beam": ("ITEM_WEAPON_GRAPPLE_BEAM", 1),
    "Storm Missile": ("ITEM_MULTILOCKON", 1),
    "Ice Missile": ("ITEM_WEAPON_ICE_MISSILE", 1),
    "Varia Suit": ("ITEM_VARIA_SUIT", 1),
    "Gravity Suit": ("ITEM_GRAVITY_SUIT", 1),
    "Phantom Cloak": ("ITEM_OPTIC_CAMOUFLAGE", 1),
    "Flash Shift": ("ITEM_GHOST_AURA", 1),
    "Pulse Radar": ("ITEM_SONAR", 1),
    "Speed Booster": ("ITEM_SPEED_BOOSTER", 1),
    "Spider Magnet": ("ITEM_MAGNET_GLOVE", 1),
    "Spin Boost": ("ITEM_DOUBLE_JUMP", 1),
    "Space Jump": ("ITEM_SPACE_JUMP", 1),
    "Screw Attack": ("ITEM_SCREW_ATTACK", 1),
    "Speed Booster Upgrade": ("ITEM_UPGRADE_SPEED_BOOST_CHARGE", 1),
    "Flash Shift Upgrade": ("ITEM_UPGRADE_FLASH_SHIFT_CHAIN", 1),
}


class DataPackage:
    """Lookup tables for ``ap_id ↔ ap_name ↔ DreadItem``.

    Loaded from ``apworld_data_dir/items.json`` and ``locations.json``
    when available, falling back to ``DEFAULT_AP_TO_PATCHER`` otherwise.
    Phase 4 will populate those JSON files with the full apworld dataset.
    """

    def __init__(
        self,
        apworld_data_dir: Optional[Path] = None,
    ) -> None:
        """Construct the DataPackage.

        ``apworld_data_dir`` is kept for back-compat with tests / callers
        that want to point at a specific on-disk data directory. When None
        (the default), data is loaded via ``importlib.resources`` from
        the installed apworld — works for both folder and .apworld-zip
        installs.
        """
        self._ap_id_to_name: dict[int, str] = {}
        self._ap_name_to_id: dict[str, int] = {}
        self._ap_name_to_dread: dict[str, DreadItem] = {}
        self._location_id_to_pickup: dict[int, DreadPickupLocation] = {}
        self._location_name_to_id: dict[str, int] = {}
        # pickup_index ↔ AP location_id, only for non-event locations
        # (events are AP-synthetic and never collected via the Switch's
        # COLLECTED_INDICES push).
        self._pickup_index_to_location_id: dict[int, int] = {}

        if apworld_data_dir is not None:
            if apworld_data_dir.exists():
                self._load_from_dir(apworld_data_dir)
            else:
                log.warning("DataPackage: apworld_data_dir not found, using stub mapping")
                self._load_stub()
            return

        # Default path: read via importlib.resources from the installed package.
        if _load_json is None:
            log.warning("DataPackage: _data_loader unavailable, using stub mapping")
            self._load_stub()
            return
        try:
            items_data = _load_json("items.json")
        except FileNotFoundError:
            log.warning("DataPackage: items.json missing in apworld, using stub")
            self._load_stub()
            return
        self._ingest_items(items_data)
        try:
            locations_data = _load_json("locations.json")
        except FileNotFoundError:
            log.warning("DataPackage: locations.json missing in apworld")
            return
        self._ingest_locations(locations_data)

    def _load_stub(self) -> None:
        for ap_name, (patcher_id, qty) in DEFAULT_AP_TO_PATCHER.items():
            self._ap_name_to_dread[ap_name] = DreadItem(
                patcher_item_id=patcher_id,
                quantity=qty,
                ap_item_name=ap_name,
            )

    def _ingest_items(self, data: list[dict]) -> None:
        for entry in data:
            name = entry["name"]
            ap_id = entry.get("ap_id")
            patcher_id = entry.get("patcher_item_id")
            qty = entry.get("quantity", 1)
            if ap_id is not None:
                self._ap_id_to_name[int(ap_id)] = name
                self._ap_name_to_id[name] = int(ap_id)
            if patcher_id:
                self._ap_name_to_dread[name] = DreadItem(
                    patcher_item_id=patcher_id,
                    quantity=int(qty),
                    ap_item_name=name,
                )

    def _ingest_locations(self, data: list[dict]) -> None:
        for entry in data:
            name = entry["name"]
            ap_id = entry.get("ap_id")
            scenario = entry.get("scenario", "")
            actor = entry.get("actor", "")
            if ap_id is not None and scenario and actor:
                pickup = DreadPickupLocation(
                    scenario=scenario, actor=actor, location_name=name)
                self._location_id_to_pickup[int(ap_id)] = pickup
                self._location_name_to_id[name] = int(ap_id)
            pickup_index = entry.get("pickup_index")
            if pickup_index is not None and ap_id is not None:
                self._pickup_index_to_location_id[int(pickup_index)] = int(ap_id)

    def _load_from_dir(self, data_dir: Path) -> None:
        import json
        items_path = data_dir / "items.json"
        if items_path.exists():
            self._ingest_items(json.loads(items_path.read_text()))
        else:
            log.warning("DataPackage: items.json missing at %s, using stub", items_path)
            self._load_stub()
        locations_path = data_dir / "locations.json"
        if locations_path.exists():
            self._ingest_locations(json.loads(locations_path.read_text()))

    # ---- Lookups ----

    def ap_name_to_dread(self, ap_name: str) -> Optional[DreadItem]:
        return self._ap_name_to_dread.get(ap_name)

    def ap_id_to_name(self, ap_id: int) -> Optional[str]:
        return self._ap_id_to_name.get(int(ap_id))

    def ap_id_to_dread(self, ap_id: int) -> Optional[DreadItem]:
        name = self._ap_id_to_name.get(int(ap_id))
        return self._ap_name_to_dread.get(name) if name else None

    def location_id_to_pickup(self, loc_id: int) -> Optional[DreadPickupLocation]:
        return self._location_id_to_pickup.get(int(loc_id))

    def pickup_index_to_location_id(self, pickup_index: int) -> Optional[int]:
        """Return the AP location_id at the given Switch pickup_index, or
        None if the index doesn't correspond to a known pickup. Used by the
        runtime wire to translate PACKET_COLLECTED_INDICES bitfields into
        ``LocationChecks`` messages."""
        return self._pickup_index_to_location_id.get(int(pickup_index))

    def all_location_ids(self) -> list[int]:
        return list(self._location_id_to_pickup.keys())
