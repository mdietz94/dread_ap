"""Per-seed door-lock weakness assignment (door rando) for the native graph.

Randovania-style: each rando-eligible door (its vanilla weakness is in the logic
DB's ``door_rando.change_from``) is reassigned a weakness from ``change_to``.
Doors are TWO-SIDED — one engine actor with a shield on each side — so the paired
sides always receive the SAME weakness (a "two-way" door). The assignment feeds
two consumers:

  * logic — ``graph_logic.build_regions`` resolves each dock atom against the
    assignment, so AP's live region sweep re-derives reachability for THIS seed
    (the whole reason for the native graph). Fill therefore only ever places
    items in spots reachable through the randomized doors — solvability is by
    construction.
  * patcher — ``assignments_to_door_patches`` emits ``door_patches``
    ({scenario, actor, door_type}) for open-dread-rando, one per physical door.

v1 excludes the ``locked`` weakness (Access Permanently Closed) from the pool so
every door stays passable with the right weapon and the door graph stays
connected (the goal can't be walled off). Per-door ``incompatible_weaknesses``
(from the logic DB) are honored.
"""
from __future__ import annotations

from typing import Any


def _physical_doors(dock_sides: dict) -> list[list[str]]:
    """Group eligible dock sides into physical doors (unordered {side, pair}).
    Returns a list of side-id groups; each group's sides share one engine actor
    and must get the same weakness."""
    seen: set[str] = set()
    groups: list[list[str]] = []
    for sid, meta in dock_sides.items():
        if sid in seen:
            continue
        group = [sid]
        seen.add(sid)
        pair = meta.get("paired_side_id")
        if pair and pair in dock_sides and pair not in seen:
            group.append(pair)
            seen.add(pair)
        groups.append(group)
    return groups


def roll_assignments(graph: dict, rng, mode: str = "randomized") -> dict[str, str]:
    """Return ``{side_id: weakness}`` for door rando. ``mode`` other than a
    randomizing mode (or an empty pool) yields ``{}`` = vanilla doors."""
    if mode in ("off", "vanilla", None):
        return {}

    dr = graph["door_rando"]
    dock_sides = graph["dock_sides"]
    locked = dr.get("locked_weakness")
    # Assignable pool: change_to minus the locking weakness (v1 keeps every door
    # passable). Only weaknesses we can render to a patcher door_type qualify.
    door_type = dr["weakness_door_type"]
    pool = [w for w in dr["change_to"]
            if w != locked and w in door_type]

    assign: dict[str, str] = {}
    for group in _physical_doors(dock_sides):
        incompat: set[str] = set()
        for sid in group:
            incompat.update(dock_sides[sid].get("incompatible_weaknesses", []))
        choices = [w for w in pool if w not in incompat]
        if not choices:
            continue
        weakness = rng.choice(choices)
        for sid in group:
            assign[sid] = weakness
    return assign


def assignments_to_door_patches(
    assign: dict[str, str], graph: dict
) -> list[dict[str, Any]]:
    """Build open-dread-rando ``door_patches`` from a side->weakness assignment,
    one entry per physical door (deduped by engine actor)."""
    dock_sides = graph["dock_sides"]
    door_type = graph["door_rando"]["weakness_door_type"]
    patches: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for sid, weakness in assign.items():
        meta = dock_sides[sid]
        p = meta["patcher"]
        key = (p["scenario"], p["actor"])
        if key in seen:
            continue
        seen.add(key)
        dt = door_type.get(weakness)
        if dt is None:
            continue
        patches.append({
            "actor": {"scenario": p["scenario"], "actor": p["actor"]},
            "door_type": dt,
        })
    return patches
