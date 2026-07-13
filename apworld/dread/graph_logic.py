"""Native AP region-graph logic.

Consumes ``data/logic_graph.json`` (emitted by
``scripts/extract_dread_rules.py``) and builds an Archipelago region
graph: one Region per trivial-SCC of Randovania nodes, one Entrance per
cross-region edge with the symbolic edge rule compiled live, events as pure-logic
regions (no AP locations/items), and a victory condition that can reference event
regions via ``state.can_reach_region``.

The graph re-derives reachability per seed via AP's own O(edges) sweep (~0.1s
solo), making door/transport/start randomization expressible as per-seed Entrance
access rules. Verified: 0 unsolved across full/items/minimal accessibility, all
trick levels, DNA, progressive, solo + 2-slot.

Door rando (DoorRando.py) supplies ``dock_assignments`` {side_id -> weakness};
when absent, every dock resolves to its vanilla ``default_weakness``. Each dock
atom is substituted with that weakness's open requirement from
``weakness_requirements`` BEFORE compilation, so ``compile_to_lambda`` never
sees a ``dock`` node.
"""
from __future__ import annotations

from typing import Any

from ._data_loader import load_json
from .Rules import compile_to_lambda
from .Tricks import TRICK_LEVEL_NAMES, effective_trick_levels

EXPECTED_GRAPH_SCHEMA = 8


def load_graph() -> dict:
    g = load_json("logic_graph.json")
    v = g.get("graph_schema_version")
    if v != EXPECTED_GRAPH_SCHEMA:
        raise RuntimeError(
            f"logic_graph.json graph_schema_version={v!r} but loader expects "
            f"{EXPECTED_GRAPH_SCHEMA}. Regenerate with "
            f"`python scripts/extract_dread_rules.py --all --graph`."
        )
    return g


def ammo_amounts_from_options(options) -> dict[str, int]:
    """Build the ``ammo_amounts`` override dict ``compile_to_lambda`` consumes
    from a slot's options, so missile / power-bomb ``sum`` capacity gates reflect
    the user's ammo settings (faithful v0.3 model). Maps:

    * ``starting_missiles``      -> missile ``base``
    * ``missile_tank_ammo``      -> per Missile Tank
    * ``missile_plus_tank_ammo`` -> per Missile+ Tank
    * power-bomb ``base`` stays 0; ``starting_power_bombs`` rides the
      ``Power Bomb`` (launcher) term's per_unit, so the launcher item still gates
      PB ammo (a player with tanks but no launcher reads 0 PB capacity)
    * ``power_bomb_tank_ammo``   -> per Power Bomb Tank

    The Power Bomb base stays 0 (you cannot fire without the launcher); the
    launcher's ``starting_power_bombs`` capacity is credited via the
    ``Power Bomb`` term's per_unit so a player with tanks but no launcher still
    reads 0 PB capacity (the launcher is AND'd in by the extractor too)."""
    return {
        "missile_base": int(options.starting_missiles.value),
        "missile_tank_per_unit": int(options.missile_tank_ammo.value),
        "missile_plus_tank_per_unit": int(options.missile_plus_tank_ammo.value),
        "pb_base": 0,
        "power_bomb_per_unit": int(options.starting_power_bombs.value),
        "power_bomb_tank_per_unit": int(options.power_bomb_tank_ammo.value),
    }


def _resolve_docks(ast: dict, assign: dict, dock_sides: dict, wreq: dict) -> dict:
    """Substitute every ``dock`` atom with the open requirement of its assigned
    weakness (default weakness when unassigned). Returns a dock-free AST.

    An ASSIGNED weakness is always a door weakness (the roll pool), so it
    resolves under the ``door::`` key even on a companion side whose own
    ``dock_type`` is ``other`` (the Open Passage back of a one-way sensor
    door). An UNASSIGNED companion side keeps its extraction-baked
    ``vanilla_ast`` (Open Passage / Access Locked / Access Closed), which may
    not exist as a ``weakness_requirements`` entry."""
    t = ast.get("type")
    if t == "dock":
        sid = ast["side_id"]
        side = dock_sides[sid]
        weakness = assign.get(sid)
        if weakness is None:
            vanilla = side.get("vanilla_ast")
            if vanilla is not None:
                return vanilla
            key = f"{side['dock_type']}::{side['default_weakness']}"
        else:
            key = f"door::{weakness}"
        return wreq.get(key, {"type": "impossible"})
    if t in ("and", "or"):
        return {"type": t,
                "items": [_resolve_docks(c, assign, dock_sides, wreq)
                          for c in ast["items"]]}
    return ast


def transport_pairs(graph: dict, matching: dict[str, str] | None) -> list:
    """Resolve the transport endpoint matching into directed (src_comp, dst_comp)
    ride edges. ``matching`` maps side_id -> destination side_id; None uses the
    vanilla ``default_dest`` pairing. Taking the transport at S lands you at the
    node of its destination D, so edge S.comp -> D.comp."""
    tr = graph.get("transports", {})
    edges = []
    for sid, meta in tr.items():
        dest = (matching or {}).get(sid) or meta["default_dest"]
        d = tr.get(dest)
        if d is not None:
            edges.append((meta["comp"], d["comp"]))
    return edges


def build_regions(world, dock_assignments: dict[str, str] | None = None,
                  transport_matching: dict[str, str] | None = None,
                  dropped: set | None = None) -> None:
    """Build the native region graph on ``world``'s multiworld.

    ``dock_assignments`` maps dock ``side_id`` -> weakness name (door rando);
    None / missing entries fall back to the vanilla ``default_weakness``.
    ``transport_matching`` maps a transport ``side_id`` -> its destination
    side_id (transport rando); None uses the vanilla pairing. Transport ride
    edges are added here (they're NOT in ``entrances``).
    ``dropped`` is the set of pickup AP-names to skip creating (unreachable-by-
    config locations under full/items — see World._compute_dropped_locations);
    their physical in-game spot is left holding the starter-preset template item.
    Stashes ``world._graph_indirect`` = [(event_region, entrance), ...] for
    explicit-indirect-condition registration in ``set_graph_rules``.
    Stashes ``world._graph_victory`` = the item-only victory AST.
    """
    from BaseClasses import Region
    from .Locations import DreadLocation, location_name_to_id

    g = load_graph()
    mw = world.multiworld
    player = world.player
    tl = effective_trick_levels(world.options)
    ept = int(world.options.energy_per_tank.value)
    amm = ammo_amounts_from_options(world.options)
    dlr = int(world.options.door_lock_rando.value) != 0
    assign = dock_assignments or {}
    dock_sides = g["dock_sides"]
    wreq = g["weakness_requirements"]

    regions: dict[int, Any] = {}
    menu = Region("Menu", player, mw)
    mw.regions.append(menu)
    for rid in range(g["n_regions"]):
        r = Region(f"R{rid}", player, mw)
        regions[rid] = r
        mw.regions.append(r)

    # Event regions (pure logic, no locations) reachable from any node-component.
    ev_region: dict[str, Any] = {}
    ev_comps: dict[str, set] = {}
    for comp, ename in g["events"]:
        ev_comps.setdefault(ename, set()).add(comp)
    for ename, comps in ev_comps.items():
        er = Region(f"Event:{ename}", player, mw)
        ev_region[ename] = er
        mw.regions.append(er)
        for c in comps:
            regions[c].connect(er, f"ev:{ename}<-{c}")

    start_comp = getattr(world, "_start_comp", None)
    if start_comp is None:
        start_comp = g["start_comp"]
    menu.connect(regions[start_comp], "Menu->start")

    # Per-entrance resolved AST + component labels, stashed for Universal
    # Tracker's /explain (see ut_explain.py + World.explain_path/explain_rule).
    # Only non-trivial ``e{i}`` carry a rule; trivial / transport / event hops
    # render as a plain label. Harmless in real generation (never read).
    explain_asts: dict[str, dict] = {}

    indirect: list = []
    for i, (csrc, cdst, ast) in enumerate(g["entrances"]):
        resolved = _resolve_docks(ast, assign, dock_sides, wreq)
        rt = resolved.get("type")
        if rt == "impossible":
            continue
        if rt == "trivial":
            regions[csrc].connect(regions[cdst], f"e{i}")
            continue
        ent = regions[csrc].connect(
            regions[cdst], f"e{i}",
            rule=compile_to_lambda(resolved, player, tl, graph_mode=True,
                                   energy_per_tank=ept, ammo_amounts=amm,
                                   door_lock_rando=dlr))
        explain_asts[f"e{i}"] = resolved
        for en in _event_atoms(resolved):
            if en in ev_region:
                indirect.append((ev_region[en], ent))

    # Transport ride edges (elevator/shuttle), per the matching (vanilla default).
    for ti, (src, dst) in enumerate(transport_pairs(g, transport_matching)):
        regions[src].connect(regions[dst], f"t{ti}")

    drop = dropped or set()
    for comp, ap_name in g["pickups"]:
        if ap_name in drop:
            continue
        loc = DreadLocation(player, ap_name, location_name_to_id[ap_name],
                            regions[comp])
        regions[comp].locations.append(loc)

    world._graph_indirect = indirect
    world._graph_victory = g["victory_condition"]
    world._explain_asts = explain_asts
    from .ut_explain import build_comp_labels
    world._explain_labels = build_comp_labels(g)


def _event_atoms(ast: dict, out: set | None = None) -> set:
    if out is None:
        out = set()
    t = ast.get("type")
    if t == "event":
        out.add(ast["name"])
    elif t in ("and", "or"):
        for c in ast["items"]:
            _event_atoms(c, out)
    return out


def _trick_names(ast: dict, out: set | None = None) -> set:
    if out is None:
        out = set()
    if ast.get("type") == "trick":
        out.add(ast["name"])
    for c in ast.get("items", []):
        _trick_names(c, out)
    return out


def unreachable_pickup_locations(
    graph: dict, trick_levels: dict, *, start_comp: int | None = None,
    transport_matching: dict | None = None, dock_assignments: dict | None = None,
    energy_per_tank: int = 100, ammo_amounts: dict | None = None,
) -> tuple[list[str], set[str]]:
    """Pickup AP-names that are unreachable even with a FULL loadout under this
    config, plus the trick short_names gating the stranded frontier.

    Reachability with every item is the fill-independent oracle for "no placement
    can ever make this location reachable": if a pickup is out of reach with all
    items, then NO fill can reach it, so it fails ``accessibility: full`` (and the
    ``items`` alias). Under ``minimal`` such a location simply holds filler — the
    Randovania-faithful behavior (Randovania's starter preset disables every
    trick and only guarantees beatability, not full reachability). Used by the
    World's accessibility guard to turn a cryptic late FillError into an
    actionable up-front OptionError."""
    from .DoorRando import early_reachable, _resolve_docks
    from .TransportRando import _ALL_ITEMS

    full = {nm: 99 for nm in _ALL_ITEMS}

    def _missing(tl):
        reach, _ = early_reachable(
            graph, full, tl, start_comp, use_events=True,
            transport_matching=transport_matching, energy_per_tank=energy_per_tank,
            ammo_amounts=ammo_amounts, dock_assignments=dock_assignments)
        return reach, [name for comp, name in graph["pickups"] if comp not in reach]

    reach, unreachable = _missing(trick_levels)
    if not unreachable:
        return [], set()

    # Candidate tricks: those on any reachable->unreachable frontier edge.
    candidates: set[str] = set()
    ds, wreq = graph["dock_sides"], graph["weakness_requirements"]
    da = dock_assignments or {}
    for c0, c1, ast in graph["entrances"]:
        if c0 in reach and c1 not in reach:
            _trick_names(_resolve_docks(ast, da, ds, wreq), candidates)

    # Narrow to tricks that ACTUALLY recover a stranded location when raised
    # alone (precise "enable this" guidance). Only runs on the error path, so the
    # extra per-candidate sweeps are free. Fall back to the raw candidate set if
    # nothing single-handedly helps (a location may need a trick combo).
    base = len(unreachable)
    recoverers = set()
    for name in candidates:
        bumped = dict(trick_levels)
        bumped[name] = max(TRICK_LEVEL_NAMES)  # raise this one to the top tier
        _, missing = _missing(bumped)
        if len(missing) < base:
            recoverers.add(name)
    return unreachable, (recoverers or candidates)


def set_graph_rules(world) -> None:
    """Register event indirect conditions, set the item-only victory + DNA goal.

    Mirrors Rules.set_rules' DNA logic; per-pickup gating lives on entrances now,
    so there are no per-location ``add_rule`` calls."""
    player = world.player
    mw = world.multiworld
    tl = effective_trick_levels(world.options)
    ept = int(world.options.energy_per_tank.value)
    amm = ammo_amounts_from_options(world.options)

    if getattr(mw.worlds[player], "explicit_indirect_conditions", True):
        for ev_region, ent in getattr(world, "_graph_indirect", []):
            mw.register_indirect_condition(ev_region, ent)

    victory = compile_to_lambda(world._graph_victory, player, tl, graph_mode=True,
                                energy_per_tank=ept, ammo_amounts=amm)
    n_dna = int(world.options.required_artifacts.value)
    if n_dna > 0:
        dna = tuple(f"Metroid DNA {k}" for k in range(1, n_dna + 1))
        mw.completion_condition[player] = (
            lambda state, v=victory, ns=dna, p=player:
            v(state) and all(state.has(n, p) for n in ns)
        )
    else:
        mw.completion_condition[player] = victory

    # Locked-DNA placement consumes world.random and pins items onto boss
    # locations. On a Universal Tracker regen the RNG stream differs, so this
    # would pick different bosses AND collide with UT overlaying the seed's real
    # placements from multidata. Skip it: DNA never gates region access (only the
    # completion condition references it), so the reachable-location set UT
    # computes is identical whether or not we pre-place the DNA here.
    fake_gen = getattr(mw, "generation_is_fake", False)
    if (not fake_gen and n_dna > 0
            and world.options.artifact_placement.current_key == "prefer_bosses"):
        from .Locations import location_table
        boss = [l.name for l in location_table
                if l.pickup_type in ("corpius", "emmi", "cutscene", "corex")]
        chosen = world.random.sample(boss, min(n_dna, len(boss)))
        for k, lname in enumerate(chosen, start=1):
            iname = f"Metroid DNA {k}"
            try:
                loc = mw.get_location(lname, player)
            except KeyError:
                continue
            item = next((i for i in mw.itempool
                         if i.player == player and i.name == iname), None)
            if item is not None:
                mw.itempool.remove(item)
                loc.place_locked_item(item)
