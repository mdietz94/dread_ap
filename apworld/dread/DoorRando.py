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

START-DOOR GUARD (the thing that makes fill succeed): AP's stock
``fill_restrictive`` needs a non-empty EARLY-reachable pickup set to bootstrap
progression. If the doors reachable from spawn with only the starting kit are
randomized to hard types, the early sphere is empty and fill dead-ends. So the
doors reachable from the start with the starting items (vanilla) are PROTECTED
(kept vanilla); only doors past that early frontier are randomized. Verified:
with the guard, 0/15 seeds fail; without it, ~all fail. The guard touches ~38 of
457 doors. See [[dread-native-graph-spike]].
"""
from __future__ import annotations

from typing import Any

from .graph_logic import _resolve_docks


def _eval(ast: dict, items: dict, events: set, tl: dict) -> bool:
    """Evaluate a (dock-resolved) rule AST against an item multiset, a set of
    triggered events, and trick levels. Mirrors compile_to_lambda semantics."""
    t = ast.get("type")
    if t == "trivial":
        return True
    if t == "impossible":
        return False
    if t == "item":
        return items.get(ast["name"], 0) >= ast.get("amount", 1)
    if t == "event":
        return ast["name"] in events
    if t == "trick":
        return ast["level"] <= tl.get(ast["name"], 0)
    if t == "sum":
        return ast["base"] + sum(items.get(x["name"], 0) * x["per_unit"]
                                 for x in ast["terms"]) >= ast["threshold"]
    if t == "damage_threshold":
        return (any(items.get(s, 0) > 0 for s in ast["suit_options"])
                or 99 + 100 * items.get("Energy Tank", 0)
                + 25 * items.get("Energy Part", 0) >= ast["hp_needed"])
    if t == "and":
        return all(_eval(c, items, events, tl) for c in ast["items"])
    if t == "or":
        return any(_eval(c, items, events, tl) for c in ast["items"])
    return False


def early_reachable(graph: dict, items: dict, tl: dict,
                    start_comp: int | None = None,
                    use_events: bool = True,
                    transport_matching: dict | None = None) -> tuple[set, dict]:
    """Regions reachable from the start with VANILLA doors + the given items +
    trick levels. Returns (reachable_comps, side_id->(src,dst)).

    ``use_events=True`` runs the event fixpoint (the true reach, used by the
    door guard). ``use_events=False`` evaluates event atoms as False — an
    ITEM-ONLY lower bound on reach, used by the starting-area foothold check so
    that hitting the target provably gives AP's fill a real (item-only) early
    sphere rather than one that depends on the live event cascade."""
    ds = graph["dock_sides"]
    wreq = graph["weakness_requirements"]
    adj: dict[int, list] = {}
    side_comp: dict[str, tuple] = {}
    for c0, c1, ast in graph["entrances"]:
        adj.setdefault(c0, []).append((c1, _resolve_docks(ast, {}, ds, wreq)))
        if ast.get("type") == "dock":
            side_comp[ast["side_id"]] = (c0, c1)
    # Transport ride edges (elevator/shuttle) per the matching — they're not in
    # ``entrances``, so add them here or reachability is understated.
    tr = graph.get("transports", {})
    for sid, meta in tr.items():
        dest = (transport_matching or {}).get(sid) or meta["default_dest"]
        d = tr.get(dest)
        if d is not None:
            adj.setdefault(meta["comp"], []).append((d["comp"], {"type": "trivial"}))
    ev: dict[str, set] = {}
    for comp, ename in graph["events"]:
        ev.setdefault(ename, set()).add(comp)

    start = graph["start_comp"] if start_comp is None else start_comp
    triggered: set = set()
    changed = True
    reach: set = set()
    while changed:
        changed = False
        reach = {start}
        frontier = [start]
        while frontier:
            u = frontier.pop()
            for v, ast in adj.get(u, []):
                if v not in reach and _eval(ast, items, triggered, tl):
                    reach.add(v)
                    frontier.append(v)
        if not use_events:
            break
        for ename, comps in ev.items():
            if ename not in triggered and any(c in reach for c in comps):
                triggered.add(ename)
                changed = True
    return reach, side_comp


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


def roll_assignments(
    graph: dict, rng, mode: str = "randomized",
    starting_items: dict | None = None, trick_levels: dict | None = None,
    start_comp: int | None = None, transport_matching: dict | None = None,
) -> dict[str, str]:
    """Return ``{side_id: weakness}`` for door rando. ``mode`` other than a
    randomizing mode yields ``{}`` = vanilla doors.

    ``starting_items``/``trick_levels`` drive the start-door guard: doors
    reachable from spawn with the starting kit stay VANILLA so fill can
    bootstrap (see module docstring). Without them the guard is skipped (every
    eligible door randomized) — only safe for analysis, not real generation."""
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

    # Start-door guard: protect doors on the early (start-reachable) frontier.
    protected: set[str] = set()
    if starting_items is not None:
        reach, side_comp = early_reachable(
            graph, starting_items, trick_levels or {}, start_comp,
            transport_matching=transport_matching)
        protected = {sid for sid, (c0, c1) in side_comp.items()
                     if c0 in reach or c1 in reach}

    assign: dict[str, str] = {}
    for group in _physical_doors(dock_sides):
        if any(sid in protected for sid in group):
            continue
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
        if not p.get("actor"):
            continue
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
