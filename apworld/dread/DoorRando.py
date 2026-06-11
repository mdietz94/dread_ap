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

TWO SAFETY BOUNDS on the pool (both learned from a hardware crash — a guest-side
null deref in the game when *traversing* a randomized door):

  * **Type restriction** — only the "basic" door types open-dread-rando renders
    robustly (``BASIC_DOOR_TYPES``, the set its own everything-patched fixture
    exercises) are eligible. The exotic shields it offers but never stress-tests
    (bomb / cross-bomb / power-bomb, ice / storm missile, diffusion) are dropped:
    they pull multiple shield-asset folders into a scenario, and a missing asset
    loads a null shield actor that the game dereferences on the door transition.
  * **Per-scenario shield budget** — open-dread-rando allots
    ``SHIELD_IDS_PER_SCENARIO`` shield-actor ids per scenario, *shared* with the
    vanilla shields it renames into that same pool up front. Each shielded door
    costs 2 ids; converting too many unshielded doors to shielded types exhausts
    the pool (``IndexError`` at patch time). The roll seeds each scenario with
    its baked vanilla baseline (``door_rando.vanilla_shield_ids``) and refuses
    shielded targets once a scenario nears the cap.

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

# open-dread-rando door_type strings (door_locks/door_patcher.py ``DoorType.type``).
# BASIC_DOOR_TYPES is the set its everything-patched fixture exercises and the
# game renders without the exotic-shield asset hazards; the rando pool is
# restricted to these. SHIELDED_DOOR_TYPES are the types that spawn a shield
# actor (``need_shield=True`` upstream) and therefore draw from the per-scenario
# shield-id pool. See module docstring.
BASIC_DOOR_TYPES = frozenset({
    "power_beam", "charge_beam", "grapple_beam",
    "wide_beam", "wave_beam", "plasma_beam", "missile", "super_missile",
})
SHIELDED_DOOR_TYPES = frozenset({
    "wide_beam", "plasma_beam", "wave_beam", "missile", "super_missile",
    "ice_missile", "diffusion_beam", "storm_missile", "bomb", "cross_bomb",
    "power_bomb", "closed",
})
SHIELD_IDS_PER_SCENARIO = 100  # open-dread-rando's per-scenario shield-id pool
SHIELD_BUDGET_MARGIN = 10      # headroom kept below the hard cap


def _eval(ast: dict, items: dict, events: set, tl: dict,
          energy_per_tank: int = 100,
          ammo_amounts: dict | None = None) -> bool:
    """Evaluate a (dock-resolved) rule AST against an item multiset, a set of
    triggered events, and trick levels. Mirrors compile_to_lambda semantics.

    ``energy_per_tank`` scales the damage_threshold HP budget and ``ammo_amounts``
    scales the ``sum`` (missile / power-bomb) capacity gates to match
    Rules.compile_to_lambda (faithful v0.3 model). The start-door guard only
    ever calls this with the starting kit (no Energy/Missile/PB Tanks), so the
    per-tank scaling is effectively inert here, but kept consistent with the
    compiler (a low ``starting_missiles``/``starting_power_bombs`` still lowers
    the base capacity correctly)."""
    from .Rules import AMMO_BASE_KEYS, AMMO_PER_UNIT_KEYS
    amounts = ammo_amounts or {}
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
        base = ast["base"]
        total = 0
        for x in ast["terms"]:
            nm = x["name"]
            bkey = AMMO_BASE_KEYS.get(nm)
            if bkey is not None and bkey in amounts:
                base = int(amounts[bkey])
            pkey = AMMO_PER_UNIT_KEYS.get(nm)
            per = (int(amounts[pkey]) if pkey is not None and pkey in amounts
                   else x["per_unit"])
            total += items.get(nm, 0) * per
        return base + total >= ast["threshold"]
    if t == "damage_threshold":
        return (any(items.get(s, 0) > 0 for s in ast["suit_options"])
                or 99 + energy_per_tank * items.get("Energy Tank", 0)
                + (energy_per_tank / 4) * items.get("Energy Part", 0)
                >= ast["hp_needed"])
    if t == "and":
        return all(_eval(c, items, events, tl, energy_per_tank, ammo_amounts)
                   for c in ast["items"])
    if t == "or":
        return any(_eval(c, items, events, tl, energy_per_tank, ammo_amounts)
                   for c in ast["items"])
    return False


def early_reachable(graph: dict, items: dict, tl: dict,
                    start_comp: int | None = None,
                    use_events: bool = True,
                    transport_matching: dict | None = None,
                    energy_per_tank: int = 100,
                    ammo_amounts: dict | None = None) -> tuple[set, dict]:
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
                if v not in reach and _eval(ast, items, triggered, tl,
                                            energy_per_tank, ammo_amounts):
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
    graph: dict, rng, mode: str = "individual",
    starting_items: dict | None = None, trick_levels: dict | None = None,
    start_comp: int | None = None, transport_matching: dict | None = None,
    energy_per_tank: int = 100, ammo_amounts: dict | None = None,
) -> dict[str, str]:
    """Return ``{side_id: weakness}`` for door rando. ``mode`` names mirror
    Randovania's ``DockRandoMode``:

      * ``"vanilla"`` / ``"off"`` / ``None`` → ``{}`` (no randomization).
      * ``"individual"`` (alias ``"randomized"``) — Randovania **Individual Doors**
        (``DockRandoMode.DOCKS``): each physical door rolls its target weakness
        independently.
      * ``"types"`` (aliases ``"door_types"`` / ``"weaknesses"``) — Randovania
        **Door Types** (``DockRandoMode.WEAKNESSES``): roll ONE target per source
        weakness up front, then every door of that vanilla type becomes the same
        target world-wide (a consistent global remap).

    ``starting_items``/``trick_levels`` drive the start-door guard: doors
    reachable from spawn with the starting kit stay VANILLA so fill can
    bootstrap (see module docstring). Without them the guard is skipped (every
    eligible door randomized) — only safe for analysis, not real generation.

    Two safety bounds apply in BOTH modes (see module docstring): the BASIC-type
    target pool and the per-scenario shield budget. In ``"types"`` mode the
    global remap is best-effort per those bounds — a door whose type-target is
    locally incompatible, or whose scenario can't afford the extra shield, stays
    VANILLA rather than breaking the bound (so a dense scenario can leave a few
    doors of a remapped type unchanged)."""
    if mode in ("off", "vanilla", None):
        return {}
    by_type = mode in ("types", "door_types", "weaknesses")

    dr = graph["door_rando"]
    dock_sides = graph["dock_sides"]
    locked = dr.get("locked_weakness")
    # Assignable pool: change_to minus the locking weakness (v1 keeps every door
    # passable), restricted to BASIC_DOOR_TYPES (the exotic shields are dropped —
    # see module docstring) and to weaknesses we can render to a patcher
    # door_type at all.
    door_type = dr["weakness_door_type"]
    pool = [w for w in dr["change_to"]
            if w != locked and door_type.get(w) in BASIC_DOOR_TYPES]

    # Start-door guard: protect doors on the early (start-reachable) frontier.
    protected: set[str] = set()
    if starting_items is not None:
        reach, side_comp = early_reachable(
            graph, starting_items, trick_levels or {}, start_comp,
            transport_matching=transport_matching,
            energy_per_tank=energy_per_tank, ammo_amounts=ammo_amounts)
        protected = {sid for sid, (c0, c1) in side_comp.items()
                     if c0 in reach or c1 in reach}

    # Per-scenario shield-id budget (see module docstring). Seed each scenario
    # with its baked vanilla baseline (2 ids per vanilla shielded door) so the
    # roll can't push a scenario past the cap and exhaust the pool at patch time.
    shield_used: dict[str, int] = {
        s: 2 * n for s, n in dr.get("vanilla_shield_ids", {}).items()
    }
    cap = SHIELD_IDS_PER_SCENARIO - SHIELD_BUDGET_MARGIN

    def _shielded(weakness: str) -> bool:
        return door_type.get(weakness) in SHIELDED_DOOR_TYPES

    # "Door Types" mode: one global source-type -> target mapping, rolled once
    # up front from the shared BASIC pool. Every door of a given vanilla weakness
    # then receives the same target (subject to the per-door incompat / budget
    # bounds applied below). ``change_from`` is already sorted in the graph, so
    # the roll order — and thus the assignment — is deterministic for a seed.
    type_map: dict[str, str] = {}
    if by_type and pool:
        for src in dr.get("change_from", []):
            type_map[src] = rng.choice(pool)

    assign: dict[str, str] = {}
    for group in _physical_doors(dock_sides):
        if any(sid in protected for sid in group):
            continue
        incompat: set[str] = set()
        for sid in group:
            incompat.update(dock_sides[sid].get("incompatible_weaknesses", []))
        scenario = dock_sides[group[0]]["patcher"]["scenario"]
        src_weakness = dock_sides[group[0]]["default_weakness"]
        src_shielded = _shielded(src_weakness)
        if by_type:
            # Apply the global type remap; skip (leave vanilla) if this type's
            # target is locally incompatible or the scenario can't afford its
            # shield — never break a safety bound to honor the remap.
            weakness = type_map.get(src_weakness)
            if weakness is None or weakness in incompat:
                continue
            if (_shielded(weakness) and not src_shielded
                    and shield_used.get(scenario, 0) + 2 > cap):
                continue
        else:
            choices = [w for w in pool if w not in incompat]
            # A target that ADDS a shield (unshielded door -> shielded type) costs
            # 2 ids. If this scenario can't afford it, drop shielded targets so the
            # door stays cheap (or vanilla, if nothing's left).
            if not src_shielded and shield_used.get(scenario, 0) + 2 > cap:
                choices = [w for w in choices if not _shielded(w)]
            if not choices:
                continue
            weakness = rng.choice(choices)
        if _shielded(weakness) and not src_shielded:
            shield_used[scenario] = shield_used.get(scenario, 0) + 2
        elif src_shielded and not _shielded(weakness):
            shield_used[scenario] = max(0, shield_used.get(scenario, 0) - 2)
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
