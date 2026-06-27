"""Compile the native region graph into item-only per-pickup rules.

``extract_dread_rules.py`` emits ``data/logic_graph.json`` (graph schema 5):
a component graph whose entrance ASTs carry the live, fixed Dread logic
(events as atoms, ``dock`` atoms for randomizable doors, ``misc`` for the
vanilla-only DoorLocks shortcuts, plus symbolic ``trick`` / ``sum`` /
``damage_threshold`` atoms). AP generation solves reachability over that graph
per seed.

External consumers that can only evaluate a flat per-location boolean of held
items — notably the PopTracker pack (``export_poptracker_logic.py``) — need the
OLD ``compiled_rules.json`` shape instead: one ITEM-ONLY rule AST per pickup,
with events already inlined. That artifact used to be produced by a forward
resolver living in ``extract_dread_rules.py``; commit ``ad2962d`` removed it
once the native graph became the sole AP backend.

This script rebuilds ``compiled_rules.json`` *from the current graph*, so the
flat rules inherit every fix that has since landed in the graph (lava→Gravity,
faithful v0.3 HP thresholds, door-locks-symbolic, ...). It is the same forward
resolver, re-pointed from the old per-area node graph at the component graph:

  * ``dock`` atoms resolve to their DEFAULT weakness's open requirement (the
    tracker models a no-door-rando seed), mirroring ``graph_logic._resolve_docks``
    with an empty assignment.
  * ``misc`` (always ``NOT DoorLocks``) resolves to TRIVIAL — those shortcut
    doors are never randomized, so the vanilla path is always open (matches
    ``Rules.compile_to_lambda``'s ``misc`` branch exactly).
  * ``event`` atoms are inlined in dependency-sphere order to each event's
    item-only reach cost (the crux that removes the item↔event cycle).
  * ``trick`` / ``sum`` / ``damage_threshold`` / ``item`` atoms stay SYMBOLIC;
    the PopTracker Translator renders them (trick dial level, vanilla ammo
    ``base``/``per_unit``, ``99 + 100·ETank + 25·EPart`` HP budget).

Unlike the historical resolver this does NOT strip no-suit damage thresholds or
derive per-region E-Tank floors — that predated the faithful damage model. HP
gates stay symbolic per-pickup and ``region_access`` is left empty (a star).

Output ``compiled_rules.json`` is ``schema_version: 3`` so the unchanged
``export_poptracker_logic.py`` consumes it.

Run:  python scripts/graph_to_compiled_rules.py
      python scripts/graph_to_compiled_rules.py --out <path>
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "apworld" / "dread" / "data"

SCHEMA_VERSION = 3            # compiled_rules.json shape the exporter expects
EXPECTED_GRAPH_SCHEMA = 5

# Disjunct caps — match the historical forward resolver so the produced rules
# are the same shape the merged PopTracker pack shipped.
NODE_CAP = 32
EDGE_CAP = 32
MAX_ROUNDS = 60              # event dependency spheres (converges well under this)


# ---------------------------------------------------------------------------
# AST helpers (ported verbatim from the pre-ad2962d extract_dread_rules.py)
# ---------------------------------------------------------------------------

TRIVIAL: dict = {"type": "trivial"}
IMPOSSIBLE: dict = {"type": "impossible"}


def _key(ast: dict) -> str:
    return json.dumps(ast, sort_keys=True)


def mk_and(items: list[dict]) -> dict:
    flat: list[dict] = []
    for it in items:
        if it == IMPOSSIBLE:
            return IMPOSSIBLE
        if it == TRIVIAL:
            continue
        if it["type"] == "and":
            flat.extend(it["items"])
        else:
            flat.append(it)
    if not flat:
        return TRIVIAL
    seen: set[str] = set()
    uniq: list[dict] = []
    for it in flat:
        k = _key(it)
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    if len(uniq) == 1:
        return uniq[0]
    return {"type": "and", "items": uniq}


def mk_or(items: list[dict]) -> dict:
    flat: list[dict] = []
    for it in items:
        if it == TRIVIAL:
            return TRIVIAL
        if it == IMPOSSIBLE:
            continue
        if it["type"] == "or":
            flat.extend(it["items"])
        else:
            flat.append(it)
    if not flat:
        return IMPOSSIBLE
    seen: set[str] = set()
    uniq: list[dict] = []
    for it in flat:
        k = _key(it)
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    if len(uniq) == 1:
        return uniq[0]
    return {"type": "or", "items": uniq}


# DNF: frozenset[frozenset[Atom]]; Atom is a hashable tuple.
EMPTY_DNF = frozenset()
TRIVIAL_DNF = frozenset({frozenset()})


def _disjunct_sort_key(disjunct) -> tuple:
    """Stable order for the cap: item-only paths beat trick-gated ones (so
    truncation can only over-restrict, never falsely-reach), then shortest
    first, then a deterministic tie-break."""
    n_tricks = sum(1 for a in disjunct if a[0] == "trick")
    return (n_tricks, len(disjunct), tuple(sorted(disjunct)))


def _absorb_dnf(dnf):
    if len(dnf) <= 1:
        return dnf
    items = list(dnf)
    keep = []
    for i, di in enumerate(items):
        dominated = False
        for j, dj in enumerate(items):
            if i == j:
                continue
            if dj < di:
                dominated = True
                break
        if not dominated:
            keep.append(di)
    return frozenset(keep)


def ast_to_dnf(ast: dict, max_disjuncts: int = 32):
    t = ast.get("type")
    if t == "trivial":
        return TRIVIAL_DNF
    if t == "impossible":
        return EMPTY_DNF
    if t == "item":
        return frozenset({frozenset({("item", ast["name"], int(ast.get("amount", 1)))})})
    if t == "event":
        return frozenset({frozenset({("event", ast["name"])})})
    if t == "trick":
        return frozenset({frozenset({("trick", ast["name"], int(ast.get("level", 1)))})})
    if t == "sum":
        terms = tuple((x["name"], int(x["per_unit"])) for x in ast["terms"])
        atom = ("sum", terms, int(ast["base"]), int(ast["threshold"]))
        return frozenset({frozenset({atom})})
    if t == "damage_threshold":
        suits = tuple(ast.get("suit_options", []))
        atom = ("damage_threshold", suits, int(ast["hp_needed"]))
        return frozenset({frozenset({atom})})
    if t == "and":
        result = TRIVIAL_DNF
        for child in ast["items"]:
            child_dnf = ast_to_dnf(child, max_disjuncts)
            if not child_dnf:
                return EMPTY_DNF
            result = frozenset({a | b for a in result for b in child_dnf})
            result = _absorb_dnf(result)
            if len(result) > max_disjuncts:
                result = frozenset(sorted(result, key=_disjunct_sort_key)[:max_disjuncts])
        return result
    if t == "or":
        result = EMPTY_DNF
        for child in ast["items"]:
            result = result | ast_to_dnf(child, max_disjuncts)
            result = _absorb_dnf(result)
            if len(result) > max_disjuncts:
                result = frozenset(sorted(result, key=_disjunct_sort_key)[:max_disjuncts])
        return result
    raise ValueError(f"unknown AST type in ast_to_dnf: {t!r}")


def dnf_to_ast(dnf) -> dict:
    if not dnf:
        return IMPOSSIBLE
    disjuncts: list[dict] = []
    for d in sorted(dnf, key=lambda x: (len(x), tuple(sorted(x)))):
        atoms = sorted(d)
        if not atoms:
            return TRIVIAL
        and_items: list[dict] = []
        for a in atoms:
            if a[0] == "item":
                and_items.append({"type": "item", "name": a[1], "amount": a[2]})
            elif a[0] == "event":
                and_items.append({"type": "event", "name": a[1]})
            elif a[0] == "trick":
                and_items.append({"type": "trick", "name": a[1], "level": a[2]})
            elif a[0] == "sum":
                terms = [{"name": n, "per_unit": p} for n, p in a[1]]
                and_items.append({"type": "sum", "terms": terms,
                                  "base": a[2], "threshold": a[3]})
            elif a[0] == "damage_threshold":
                and_items.append({"type": "damage_threshold",
                                  "suit_options": list(a[1]),
                                  "hp_needed": a[2]})
            else:
                raise ValueError(f"unknown atom kind in dnf_to_ast: {a}")
        disjuncts.append(mk_and(and_items))
    return mk_or(disjuncts)


def enumerate_paths(entry, edges, targets, *, max_iterations=200,
                    node_cap=NODE_CAP, edge_cap=EDGE_CAP) -> dict:
    """Bellman-Ford-style DNF fixed-point: reach[node] is the minimal item-sets
    that suffice to reach it from ``entry``. Ported from the historical
    resolver, single-entry."""
    all_nodes = set(edges.keys())
    for adj in edges.values():
        for tgt, _ in adj:
            all_nodes.add(tgt)
    all_nodes.add(entry)

    edge_dnf = {}
    for u, adj in edges.items():
        lst = []
        for v, req in adj:
            d = ast_to_dnf(req, max_disjuncts=edge_cap)
            if not d:
                continue
            lst.append((v, d))
        edge_dnf[u] = lst

    reach = {n: EMPTY_DNF for n in all_nodes}
    reach[entry] = TRIVIAL_DNF
    queue = deque([entry])
    in_queue = {entry}

    iterations = 0
    cap = max_iterations * max(1, len(all_nodes))
    while queue and iterations < cap:
        u = queue.popleft()
        in_queue.discard(u)
        iterations += 1
        if not reach[u]:
            continue
        for v, d in edge_dnf.get(u, ()):
            new_paths = frozenset({pu | pe for pu in reach[u] for pe in d})
            combined = reach[v] | new_paths
            combined = _absorb_dnf(combined)
            if len(combined) > node_cap:
                combined = frozenset(sorted(combined, key=_disjunct_sort_key)[:node_cap])
            if combined != reach[v]:
                reach[v] = combined
                if v not in in_queue:
                    queue.append(v)
                    in_queue.add(v)

    if queue:
        print(f"  WARN: enumerate_paths hit iteration cap "
              f"(queue still had {len(queue)} entries)", file=sys.stderr)

    return {tgt: dnf_to_ast(reach.get(tgt, EMPTY_DNF)) for tgt in targets}


def _substitute_events(ast: dict, event_cost: dict) -> dict:
    """Inline each event's item-only reach cost; uncollected events → Impossible
    (their edge is blocked this round)."""
    t = ast.get("type")
    if t == "event":
        return event_cost.get(ast["name"], IMPOSSIBLE)
    if t == "and":
        return mk_and([_substitute_events(c, event_cost) for c in ast["items"]])
    if t == "or":
        return mk_or([_substitute_events(c, event_cost) for c in ast["items"]])
    return ast


def _strip_tricks_above(ast: dict, floor: int) -> dict:
    """Replace every trick atom with level > floor by IMPOSSIBLE; keep tricks at
    level <= floor symbolic."""
    t = ast.get("type")
    if t == "trick":
        return IMPOSSIBLE if int(ast.get("level", 1)) > floor else ast
    if t == "and":
        return mk_and([_strip_tricks_above(c, floor) for c in ast["items"]])
    if t == "or":
        return mk_or([_strip_tricks_above(c, floor) for c in ast["items"]])
    return ast


# ---------------------------------------------------------------------------
# Graph atom resolution (dock / misc → item-only or trivial/impossible)
# ---------------------------------------------------------------------------

def _resolve(ast: dict, dock_sides: dict, wreq: dict) -> dict:
    """Resolve ``dock`` and ``misc`` atoms for a no-door-rando tracker seed.

    ``dock`` → the open requirement of its DEFAULT weakness (vanilla door).
    ``misc`` (always ``NOT DoorLocks``) → TRIVIAL: those shortcut doors are never
    randomized so the vanilla path is always open (matches
    ``Rules.compile_to_lambda``'s ``misc`` branch). Everything else passes
    through; ``and``/``or`` recurse."""
    t = ast.get("type")
    if t == "dock":
        side = dock_sides[ast["side_id"]]
        weakness = side["default_weakness"]
        key = f"{side['dock_type']}::{weakness}"
        return _resolve(wreq.get(key, IMPOSSIBLE), dock_sides, wreq)
    if t == "misc":
        # DoorLocks is effectively always False for us (these connections' doors
        # are never randomized), so `NOT DoorLocks` is always passable.
        negate = bool(ast.get("negate", False))
        door_locks_active = False
        holds = (not door_locks_active) if negate else door_locks_active
        return dict(TRIVIAL) if holds else dict(IMPOSSIBLE)
    if t in ("and", "or"):
        return {"type": t, "items": [_resolve(c, dock_sides, wreq) for c in ast["items"]]}
    return ast


def _transport_edges(graph: dict) -> list[tuple[int, int]]:
    """Vanilla transport ride edges (src_comp -> dst_comp), no rando."""
    tr = graph.get("transports", {})
    edges = []
    for sid, meta in tr.items():
        dest = meta["default_dest"]
        d = tr.get(dest)
        if d is not None:
            edges.append((meta["comp"], d["comp"]))
    return edges


# ---------------------------------------------------------------------------
# Forward resolver over the component graph
# ---------------------------------------------------------------------------

def compile_forward(graph: dict, *, strip_tricks_above: int | None = None
                    ) -> tuple[dict, dict]:
    """Item-only forward resolver over the component graph.

    Returns (rules_by_pickup_name, event_cost). Events are collected in
    dependency spheres: each round, every event atom in an edge is replaced by
    the item-only reach cost of events collected in earlier rounds, reachability
    is recomputed, and newly-reachable events record their own cost for the next
    round — so the result is acyclic and item-only."""
    dock_sides = graph["dock_sides"]
    wreq = graph["weakness_requirements"]
    entry = graph["start_comp"]
    n_regions = graph["n_regions"]

    # Resolve dock/misc once; the result still carries event atoms (inlined in
    # the round loop) and symbolic trick/sum/damage atoms.
    base_edges: dict[int, list[tuple[int, dict]]] = {}
    for csrc, cdst, ast in graph["entrances"]:
        base_edges.setdefault(csrc, []).append((cdst, _resolve(ast, dock_sides, wreq)))
    for src, dst in _transport_edges(graph):
        base_edges.setdefault(src, []).append((dst, dict(TRIVIAL)))

    ev_comps_by_name: dict[str, list[int]] = {}
    for comp, ename in graph["events"]:
        ev_comps_by_name.setdefault(ename, []).append(comp)

    targets = set(range(n_regions))
    event_cost: dict[str, dict] = {}
    node_reach: dict[int, dict] = {}
    for rnd in range(MAX_ROUNDS):
        round_edges = {
            u: [(v, _substitute_events(
                    _strip_tricks_above(req, strip_tricks_above)
                    if strip_tricks_above is not None else req, event_cost))
                for v, req in adj]
            for u, adj in base_edges.items()
        }
        node_reach = enumerate_paths(entry, round_edges, targets)
        newly = {nm for nm, comps in ev_comps_by_name.items()
                 if nm not in event_cost
                 and any(node_reach.get(c, IMPOSSIBLE).get("type") != "impossible"
                         for c in comps)}
        if not newly:
            break
        for nm in newly:
            event_cost[nm] = mk_or([node_reach.get(c, IMPOSSIBLE)
                                    for c in ev_comps_by_name[nm]])
    else:
        print(f"  WARN: compile_forward hit MAX_ROUNDS={MAX_ROUNDS} "
              f"(uncollected events remain)", file=sys.stderr)

    rules_by_loc = {name: node_reach.get(comp, IMPOSSIBLE)
                    for comp, name in graph["pickups"]}
    return rules_by_loc, event_cost


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def build_compiled_rules(graph: dict) -> dict:
    if graph.get("graph_schema_version") != EXPECTED_GRAPH_SCHEMA:
        raise SystemExit(
            f"logic_graph.json graph_schema_version="
            f"{graph.get('graph_schema_version')!r} (need {EXPECTED_GRAPH_SCHEMA}). "
            f"Regenerate: python scripts/extract_dread_rules.py --all")

    print("forward resolver: full symbolic pass ...")
    full_rules, full_events = compile_forward(graph)
    print("forward resolver: recovery pass (level>=2 tricks stripped) ...")
    item_rules, item_events = compile_forward(graph, strip_tricks_above=1)

    # Union the two passes per pickup — the recovery pass restores long
    # item-only / Beginner detours the symbolic pass truncated at the cap.
    rules = {
        name: mk_or([item_rules.get(name, IMPOSSIBLE), ast])
        for name, ast in full_rules.items()
    }

    victory = graph["victory_condition"]   # item-only event atom ("Ship")
    victory_ast = mk_or([
        _substitute_events(_strip_tricks_above(victory, 1), item_events),
        _substitute_events(victory, full_events),
    ])

    return {
        "schema_version": SCHEMA_VERSION,
        "pinned_commit": graph.get("pinned_commit", ""),
        "source": "graph_to_compiled_rules.py (from logic_graph.json)",
        # region_access is a star: per-pickup rules carry the full item-only
        # reach (events inlined), and damage thresholds stay symbolic per-pickup
        # under the faithful v0.3 model, so no coarse region gate is needed.
        "region_access": {},
        "victory_condition": victory_ast,
        "rules": rules,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--graph", type=Path, default=DATA_DIR / "logic_graph.json",
                    help="input logic_graph.json")
    ap.add_argument("--out", type=Path, default=DATA_DIR / "compiled_rules.json",
                    help="output compiled_rules.json")
    args = ap.parse_args(argv)

    if not args.graph.exists():
        print(f"missing {args.graph}; regenerate with "
              f"`python scripts/extract_dread_rules.py --all`", file=sys.stderr)
        return 2
    graph = json.loads(args.graph.read_text())
    out = build_compiled_rules(graph)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2))
    n_impossible = sum(1 for r in out["rules"].values()
                       if r.get("type") == "impossible")
    print(f"wrote {args.out} — {len(out['rules'])} rules "
          f"({n_impossible} impossible), victory "
          f"{out['victory_condition'].get('type')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
