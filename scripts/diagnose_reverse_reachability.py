"""Diagnostic: would adding escape-cost AND-ed into each pickup rule
strictly tighten the logic, and by how much?

Computes ``escape[N]`` — items needed to reach a "safe" node (the starting
location, any Save Station, or any cross-region Teleport) from N via the
REVERSED graph — then compares the existing per-pickup rule against
``reach[N] AND escape[N]``. Strict tightening means the current rule lets
a player into a pickup's room without enough items to leave, i.e. a
potential softlock.

This script does NOT modify ``compiled_rules.json``. Read-only analysis
designed to validate option (a) of the softlock-prevention design.

Usage:
    python scripts/diagnose_reverse_reachability.py
    python scripts/diagnose_reverse_reachability.py --cache-dir ...
    python scripts/diagnose_reverse_reachability.py --top 50 --trick-level 2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_dread_rules as edr


# ---------------------------------------------------------------------------
# Safe-terminal identification
# ---------------------------------------------------------------------------

# Sub-area name prefixes that mark "safe" rooms. A node in a Save Station
# sub-area lets the player save and reload (escaping any trap); a Teleport
# sub-area lets them leave the region entirely. Recharge / Map stations
# refill resources but don't let you bail, so they don't qualify.
SAFE_SUBAREA_PREFIXES = ("Save Station", "Teleport to")


def find_safe_terminals(nodes: dict) -> list:
    safe = []
    for key in nodes:
        _region, sub, _node = key
        if sub.startswith(SAFE_SUBAREA_PREFIXES):
            safe.append(key)
    return safe


# ---------------------------------------------------------------------------
# Reverse graph
# ---------------------------------------------------------------------------

def reverse_edges(edges: dict) -> dict:
    rev: dict = {}
    for u, adj in edges.items():
        for v, req in adj:
            rev.setdefault(v, []).append((u, req))
    return rev


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-dir", type=Path, default=None,
        help="Override the .dread-cache/randovania-logic path "
             "(default: auto-detect worktree, then parent dread_ap repo)",
    )
    parser.add_argument(
        "--top", type=int, default=30,
        help="How many tightened locations to print in detail (default: 30)",
    )
    parser.add_argument(
        "--trick-level", type=int, default=1, choices=(1, 2, 3),
        help="Trick tier to use for both forward and reverse pass",
    )
    args = parser.parse_args(argv)

    cache_dir = args.cache_dir
    if cache_dir is None:
        for candidate in (
            edr.CACHE_DIR,
            Path("C:/Users/maxwe/Documents/dread_ap/.dread-cache/randovania-logic"),
        ):
            if candidate.exists():
                cache_dir = candidate
                break
    if cache_dir is None or not cache_dir.exists():
        print("cache dir not found; pass --cache-dir", file=sys.stderr)
        return 2
    print(f"cache: {cache_dir}")

    header = edr.Header.from_json(json.loads((cache_dir / "header.json").read_text()))
    header.trick_level = args.trick_level

    all_area_data: dict[str, dict] = {}
    for area_name in edr.ALL_AREAS:
        all_area_data[area_name] = json.loads((cache_dir / f"{area_name}.json").read_text())

    ap_locations = json.loads((edr.DATA_DIR / "locations.json").read_text())
    ap_loc_by_actor = {
        (l["region"], l["actor"]): l["name"]
        for l in ap_locations
        if l.get("pickup_type") == "actor" and l.get("actor")
    }
    ap_loc_by_pickup_index = {
        l["pickup_index"]: l["name"]
        for l in ap_locations
        if l.get("pickup_type") not in ("actor", "event")
        and l.get("pickup_index") is not None
    }

    print(f"running forward resolver (trick L{args.trick_level}) ...")
    _rules_by_loc, event_cost, _ = edr.compile_forward(
        all_area_data, header, ap_loc_by_actor, ap_loc_by_pickup_index,
    )
    print(f"  events inlined: {len(event_cost)}")

    edges, nodes = edr.build_global_graph(all_area_data, header)
    print(f"  graph: {len(nodes)} nodes, {sum(len(v) for v in edges.values())} edges")

    inlined_edges = {
        u: [(v, edr._substitute_events(req, event_cost)) for v, req in adj]
        for u, adj in edges.items()
    }

    start = header.starting_location
    entry = (start["region"], start["area"], start["node"])
    print(f"  forward reach from {entry} ...")
    forward_reach_ast = edr.enumerate_paths(
        [entry], inlined_edges, set(nodes.keys()),
        max_disjuncts_per_node=32, max_disjuncts_per_edge=32,
    )

    safe = find_safe_terminals(nodes)
    if entry not in safe:
        safe.append(entry)
    print(f"  safe terminals: {len(safe)} (Save Stations + Teleports + start)")

    rev_edges = reverse_edges(inlined_edges)
    print(f"  reverse reach (escape) ...")
    escape_ast = edr.enumerate_paths(
        safe, rev_edges, set(nodes.keys()),
        max_disjuncts_per_node=32, max_disjuncts_per_edge=32,
    )

    # Per-pickup tightening — actor pickups only. Boss/EMMI/cutscene/corex
    # pickups have escape rules that recursively reference the in-room event
    # (e.g. EMMI death) whose own cost transitively pulls in the pickup's own
    # item, producing a circular IMPOSSIBLE that isn't a real softlock.
    tightenings: list[dict] = []
    pickups_seen = 0
    already_impossible = 0
    no_escape_path = 0
    safe_already = 0
    non_actor_skipped = 0

    for key, n in nodes.items():
        if n.get("node_type") != "pickup":
            continue
        region = key[0]
        actor_name = (n.get("extra") or {}).get("actor_name")
        loc_name = None
        is_actor = False
        if actor_name and (region, actor_name) in ap_loc_by_actor:
            loc_name = ap_loc_by_actor[(region, actor_name)]
            is_actor = True
        elif n.get("pickup_index") is not None and n["pickup_index"] in ap_loc_by_pickup_index:
            loc_name = ap_loc_by_pickup_index[n["pickup_index"]]
        if loc_name is None:
            continue
        if not is_actor:
            non_actor_skipped += 1
            continue
        pickups_seen += 1

        fwd = forward_reach_ast.get(key, edr.IMPOSSIBLE)
        esc = escape_ast.get(key, edr.IMPOSSIBLE)

        # Strip no-suit damage thresholds on both sides. The forward compiler
        # strips them post-hoc and replaces with per-region E-Tank floors; the
        # reverse pass doesn't, so escape rules retain HP atoms that the
        # forward rules have already removed. Apples-to-apples requires
        # stripping both before set comparison.
        fwd = edr._strip_no_suit_damage_thresholds(fwd)
        esc = edr._strip_no_suit_damage_thresholds(esc)

        if fwd == edr.IMPOSSIBLE:
            already_impossible += 1
            continue

        fwd_dnf = edr.ast_to_dnf(fwd, max_disjuncts=64)
        esc_dnf = edr.ast_to_dnf(esc, max_disjuncts=64)

        if esc_dnf == edr.TRIVIAL_DNF:
            safe_already += 1
            continue

        if not esc_dnf:
            no_escape_path += 1
            combined_dnf = edr.EMPTY_DNF
        else:
            combined_dnf = frozenset({pf | pe for pf in fwd_dnf for pe in esc_dnf})
            combined_dnf = edr._absorb_dnf(combined_dnf)

        if combined_dnf == fwd_dnf:
            continue

        # The REAL signal: did the cheapest reach path get strictly longer?
        # If shortest_combined == shortest_fwd, the tightening only added
        # worse alternatives — there's still an unchanged cheapest path, no
        # softlock. If shortest_combined > shortest_fwd, EVERY forward path
        # of the old minimum length now needs extra items to be escape-safe.
        shortest_fwd = min(len(d) for d in fwd_dnf) if fwd_dnf else 0
        if combined_dnf:
            shortest_combined = min(len(d) for d in combined_dnf)
        else:
            shortest_combined = float("inf")

        if shortest_combined <= shortest_fwd and combined_dnf:
            # Tightening only added longer alternatives, not a real constraint
            continue

        # Find the (fwd, esc) pair that produced the cheapest combined
        # disjunct, then report the extra items the escape adds on top of
        # that specific forward path.
        best_pair: tuple = (None, None)
        best_len: int = 10**9
        for fd in fwd_dnf:
            for ed in esc_dnf:
                merged = fd | ed
                if len(merged) < best_len:
                    best_len = len(merged)
                    best_pair = (fd, ed)
        extra: set = set()
        if best_pair[0] is not None and best_pair[1] is not None:
            extra = best_pair[1] - best_pair[0]

        tightenings.append({
            "loc": loc_name,
            "key": key,
            "fwd_size": len(fwd_dnf),
            "esc_size": len(esc_dnf),
            "combined_size": len(combined_dnf),
            "now_impossible": not combined_dnf,
            "extra_items": sorted(_atom_str(a) for a in extra),
            "shortest_fwd_len": shortest_fwd,
            "shortest_combined_len": shortest_combined,
        })

    print()
    print("=== SUMMARY ===")
    print(f"actor pickups examined:     {pickups_seen}")
    print(f"non-actor (boss/EMMI etc):  {non_actor_skipped}  (skipped — event-locked, circular)")
    print(f"already IMPOSSIBLE (fwd):   {already_impossible}")
    print(f"escape is trivial:          {safe_already}  (no tightening possible)")
    print(f"no escape path at all:      {no_escape_path}  (would become IMPOSSIBLE)")
    print(f"cheapest path tightened:    {len(tightenings)}  (real softlock candidates)")
    print()

    if tightenings:
        atom_freq: dict[str, int] = {}
        for t in tightenings:
            for a in t["extra_items"]:
                atom_freq[a] = atom_freq.get(a, 0) + 1
        if atom_freq:
            print("Extra items most often introduced by escape constraint:")
            for atom, n in sorted(atom_freq.items(), key=lambda x: -x[1])[:20]:
                print(f"  {n:4d}  {atom}")
            print()

        tightenings.sort(
            key=lambda t: (-int(t["now_impossible"]),
                           -(t["shortest_combined_len"] - t["shortest_fwd_len"]),
                           -len(t["extra_items"]),
                           t["loc"])
        )
        print(f"=== TOP {args.top} MOST AFFECTED LOCATIONS ===")
        for t in tightenings[:args.top]:
            flag = " [NOW IMPOSSIBLE]" if t["now_impossible"] else ""
            extra = ", ".join(t["extra_items"]) if t["extra_items"] else "(stricter combinations only)"
            delta = t["shortest_combined_len"] - t["shortest_fwd_len"]
            print(f"  {t['loc']}{flag}  [+{delta} items on cheapest path]")
            print(f"    node: {'/'.join(t['key'])}")
            print(f"    extra items needed to escape: {extra}")
            print(f"    cheapest path: {t['shortest_fwd_len']} -> {t['shortest_combined_len']} items"
                  f"  (fwd={t['fwd_size']}, esc={t['esc_size']}, combined={t['combined_size']} disjuncts)")

    return 0


def _atom_str(a: tuple) -> str:
    kind = a[0]
    if kind == "item":
        return f"{a[1]}x{a[2]}"
    if kind == "event":
        return f"event:{a[1]}"
    if kind == "sum":
        terms = "+".join(f"{n}x{p}" for n, p in a[1])
        return f"sum({terms}>={a[3]})"
    if kind == "damage_threshold":
        suits = "/".join(a[1]) or "no-suit"
        return f"hp{a[2]}({suits})"
    return repr(a)


if __name__ == "__main__":
    sys.exit(main())
