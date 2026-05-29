"""Assemble + chunk the Randovania ``RL.*`` bootstrap from our data tables.

The exlaunch sysmodule opens the socket and exposes the ``RL.Send*`` push
primitives in C++, but it does NOT implement the query/delivery functions the
client calls (``RL.GetCollectedIndicesAndSend``, ``RL.GetReceivedPickupsAndSend``,
``RL.ReceivePickup``, ``RL.UpdateRDVClient``, …). Randovania sends those to the
Switch as Lua source at *every* connect (``dread_executor.bootstrap()``); the
patcher only bakes no-op stubs into the ROM. So our client must send the same
bootstrap — otherwise the API probe itself fails (``RL.Version`` is nil) and
every poll/delivery call hits an undefined function.

This module is a faithful port of randovania's ``get_bootstrapper_for`` +
``bootstrap()`` chunking (``game_connection/executor/dread_executor.py``), but
driven by THIS apworld's ``items.json`` / ``locations.json`` instead of
Randovania's in-memory game database. The vendored Lua lives in ``lua/`` (see
``lua/NOTICE.md``); we only fill its ``TEMPLATE("...")`` holes.

``replace_lua_template`` runs ``lua_convert(value, wrap_strings=False)`` on each
replacement, and for a ``str`` that is just ``str(value)`` — verbatim passthrough.
So we pre-render every replacement to a Lua source string (mirroring upstream's
``repr(...)`` / ``"{...}"`` formatting) and do plain text substitution.
"""
from __future__ import annotations

import re
from importlib.resources import files
from typing import Optional

# Order matters: part_0 defines RL.Pickups + GetCollectedIndicesAndSend and
# DoFile's the ROM-baked RandomizerPowerup; the locations blocks then fill
# RL.Pickups; the trailing assignment flips the bootstrapped flag.
_BOOTSTRAP_PARTS = (
    "bootstrap_part_0",
    "bootstrap_part_1",
    "bootstrap_part_2",
    "bootstrap_part_3",
)
_LOCATIONS_TEMPLATE = "bootstrap_locations"
_BOOTSTRAP_DONE = "RL.Bootstrap=true"

_TEMPLATE_LEFTOVER = re.compile(r'TEMPLATE\("([^"]+)"\)|T__(\w+)__T')
# Lua bareword table keys must be identifiers; the locations template uses each
# pickup's actor/callback name as a bareword key (`{ItemSphere_ChargeBeam=1}`).
_LUA_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*$")


def _read_lua(name: str) -> str:
    return files(__package__).joinpath("lua").joinpath(name + ".lua").read_text(
        encoding="utf-8"
    )


def _substitute(code: str, replacements: dict[str, str]) -> str:
    """Mirror randovania's ``replace_lua_template`` (both placeholder styles,
    plus the unfulfilled-template guard). Values are already Lua source."""
    for key, value in replacements.items():
        code = code.replace(f'TEMPLATE("{key}")', value)
        code = code.replace(f"T__{key}__T", value)
    leftover = [a or b for a, b in _TEMPLATE_LEFTOVER.findall(code)]
    if leftover:
        raise ValueError(f"unfulfilled bootstrap templates: {sorted(set(leftover))}")
    return code


def build_bootstrap_code(items_rows: list[dict], locations_rows: list[dict]) -> list[str]:
    """Return the ordered Lua code blocks to send, ending with ``RL.Bootstrap=true``.

    Mirrors ``get_bootstrapper_for``: ``num_pickup_nodes`` + ``inventory`` fill
    parts 0-3; one ``bootstrap_locations`` block per scenario fills
    ``RL.Pickups`` with ``Location_Collected_<scenario>_<key>`` props keyed by
    pickup index (the bitfield position the Switch reports in COLLECTED_INDICES).
    """
    pickups = [loc for loc in locations_rows if loc.get("pickup_index") is not None]
    if not pickups:
        raise ValueError("locations data has no pickup_index entries to bootstrap")

    indices = [int(loc["pickup_index"]) for loc in pickups]
    if len(set(indices)) != len(indices):
        raise ValueError("duplicate pickup_index values in locations data")
    num_pickup_nodes = max(indices) + 1

    # Distinct patcher_item_ids, stable order. Drives RL.InventoryItems, which
    # only feeds the (diagnostic) NEW_INVENTORY array; inventory_index comes
    # from the JSON "index" field regardless, so the exact set isn't load-bearing.
    inventory: list[str] = []
    seen: set[str] = set()
    for item in items_rows:
        pid = item.get("patcher_item_id")
        if pid and pid not in seen:
            seen.add(pid)
            inventory.append(pid)
    inventory_lua = "{" + ",".join(repr(p) for p in inventory) + "}"

    base = {"num_pickup_nodes": str(num_pickup_nodes), "inventory": inventory_lua}
    blocks = [_substitute(_read_lua(part), base) for part in _BOOTSTRAP_PARTS]

    # Group by scenario in first-appearance order (block order is cosmetic —
    # each block independently sets disjoint RL.Pickups slots).
    by_scenario: dict[str, list[dict]] = {}
    for loc in pickups:
        by_scenario.setdefault(loc["scenario"], []).append(loc)

    loc_template = _read_lua(_LOCATIONS_TEMPLATE)
    for scenario, locs in by_scenario.items():
        for loc in locs:
            key = str(loc["actor"])
            if not _LUA_IDENT.match(key):
                raise ValueError(
                    f"pickup key {key!r} (index {loc['pickup_index']}) is not a valid "
                    "Lua identifier; bootstrap_locations would emit malformed Lua"
                )
        pairs = ",".join(f"{loc['actor']}={int(loc['pickup_index']) + 1}" for loc in locs)
        blocks.append(_substitute(loc_template, {**base, "pairs": pairs,
                                                 "location": repr(scenario + "_")}))

    blocks.append(_BOOTSTRAP_DONE)
    return blocks


def chunk_lua_blocks(blocks: list[str], buffer_size: int) -> list[str]:
    """Pack code blocks into ``;``-joined chunks that each fit ``buffer_size``.

    Verbatim port of ``dread_executor.bootstrap()``'s packing loop (note the
    ``+ 1`` for the joining semicolon)."""
    chunks: list[str] = []
    current = ""
    for code in blocks:
        if len(current) + len(code) + 1 > buffer_size:
            if not current:
                raise ValueError(
                    f"single bootstrap block has length {len(code)} but buffer is {buffer_size}"
                )
            chunks.append(current)
            current = ""
        if current:
            current += ";"
        current += code
    if current:
        chunks.append(current)
    return chunks


def load_bootstrap_code(
    items_rows: Optional[list[dict]] = None,
    locations_rows: Optional[list[dict]] = None,
) -> list[str]:
    """Convenience: build the bootstrap from the apworld's bundled data tables
    (loaded zip-safely via ``_data_loader``) unless rows are supplied."""
    if items_rows is None or locations_rows is None:
        from .._data_loader import load_json
        if items_rows is None:
            items_rows = load_json("items.json")
        if locations_rows is None:
            locations_rows = load_json("locations.json")
    return build_bootstrap_code(items_rows, locations_rows)
