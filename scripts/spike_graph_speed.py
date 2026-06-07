"""SPIKE: measure real AP generation speed with a NATIVE region graph.

This is the make-or-break probe for the door-lock/start-area plan. It swaps the
apworld's star topology for a faithful Randovania-style region graph (one region
per trivial-SCC of nodes, one entrance per cross-region edge with a per-edge
access rule, events as locked event-items), then runs the REAL AP generation
pipeline + fill and times it.

Run:  python scripts/spike_graph_speed.py
Needs the vendored Archipelago runtime (smwonder_archipelago/vendor/Archipelago)
and the .dread-cache logic database (resolved by extract_dread_rules.CACHE_DIR).

It does NOT modify any production file — it monkeypatches DreadWorld in-process.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Vendored AP runtime (same one the AP-gated tests use). Resolve robustly: the
# repo may be checked out in a deep git worktree, so search upward for the
# sibling smwonder_archipelago checkout.
def _find_ap_runtime() -> Path:
    explicit = Path(r"C:\Users\maxwe\Documents\smwonder_archipelago\vendor\Archipelago")
    if explicit.exists():
        return explicit
    for anc in REPO.parents:
        cand = anc / "smwonder_archipelago" / "vendor" / "Archipelago"
        if cand.exists():
            return cand
    raise SystemExit("could not locate vendored Archipelago runtime")


AP_RUNTIME = _find_ap_runtime()
for p in (str(AP_RUNTIME), str(REPO / "apworld"), str(REPO / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import extract_dread_rules as E  # noqa: E402


# ---------------------------------------------------------------------------
# Build the symbolic graph + SCC collapse (in memory, vanilla dock weaknesses)
# ---------------------------------------------------------------------------

def build_graph():
    header = E.Header.from_json(json.loads((E.CACHE_DIR / "header.json").read_text()))
    areas = {a: json.loads((E.CACHE_DIR / f"{a}.json").read_text()) for a in E.ALL_AREAS}

    nodes = {}          # key -> node dict
    conn = []           # (u, v, ast)
    dock = []           # (u, v, ast)
    for region, ad in areas.items():
        for sub_name, sub in ad["areas"].items():
            for nname, n in sub["nodes"].items():
                key = (region, sub_name, nname)
                nodes[key] = n
                for tgt, req in n.get("connections", {}).items():
                    conn.append((key, (region, sub_name, tgt),
                                 E.translate_requirement(req, header)))
    for key, n in list(nodes.items()):
        if n.get("node_type") != "dock":
            continue
        dc = n.get("default_connection")
        if not dc:
            continue
        req = E.dock_open_requirement(header, n.get("dock_type"),
                                      n.get("default_dock_weakness"))
        ov = n.get("override_default_open_requirement")
        if ov is not None:
            req = E.translate_requirement(ov, header)
        dock.append((key, (dc["region"], dc["area"], dc["node"]), req))

    # SCC over trivial connection edges only (docks never collapse).
    adj = {k: [] for k in nodes}
    radj = {k: [] for k in nodes}
    for u, v, ast in conn:
        if v in nodes and ast.get("type") == "trivial":
            adj[u].append(v)
            radj[v].append(u)
    visited = set()
    order = []
    for s in nodes:
        if s in visited:
            continue
        stack = [(s, 0)]
        while stack:
            node, i = stack.pop()
            if i == 0:
                if node in visited:
                    continue
                visited.add(node)
            if i < len(adj[node]):
                stack.append((node, i + 1))
                w = adj[node][i]
                if w not in visited:
                    stack.append((w, 0))
            else:
                order.append(node)
    comp = {}
    lab = 0
    seen = set()
    for k in reversed(order):
        if k in seen:
            continue
        stk = [k]
        seen.add(k)
        while stk:
            x = stk.pop()
            comp[x] = lab
            for w in radj[x]:
                if w not in seen:
                    seen.add(w)
                    stk.append(w)
        lab += 1

    # AP-location lookup for pickups.
    locs = json.loads((E.DATA_DIR / "locations.json").read_text())
    by_actor = {(l["region"], l["actor"]): l["name"]
                for l in locs if l.get("pickup_type") == "actor" and l.get("actor")}
    by_pidx = {l["pickup_index"]: l["name"]
               for l in locs if l.get("pickup_type") not in ("actor", "event")
               and l.get("pickup_index") is not None}

    # Cross-region entrances (dedup identical (csrc,cdst,ast) by OR-merge would
    # save count, but keep separate here to measure the worst case).
    entrances = []
    for u, v, ast in conn:
        if v in nodes and comp[u] != comp[v]:
            entrances.append((comp[u], comp[v], ast))
    for u, v, ast in dock:
        if v in nodes and comp[u] != comp[v]:
            entrances.append((comp[u], comp[v], ast))

    pickups = []        # (comp_id, ap_location_name)
    events = []         # (comp_id, event_name, unique_node_id)
    for key, n in nodes.items():
        nt = n.get("node_type")
        if nt == "pickup":
            actor = n.get("extra", {}).get("actor_name")
            pidx = n.get("pickup_index")
            name = by_actor.get((key[0], actor)) or by_pidx.get(pidx)
            if name:
                pickups.append((comp[key], name))
        elif nt == "event":
            # One locked event-LOCATION per node (unique name), all granting the
            # same event-ITEM. Multiple locked copies => has>=1 gives the correct
            # "triggerable from ANY of its nodes" OR-reachability.
            uid = "::".join(key)
            events.append((comp[key], n["event_name"], uid))

    start = header.starting_location
    start_key = (start["region"], start["area"], start["node"])
    start_comp = comp[start_key]

    return {
        "n_regions": lab,
        "n_entrances": len(entrances),
        "regions": lab,
        "entrances": entrances,
        "pickups": pickups,
        "events": events,
        "start_comp": start_comp,
    }


GRAPH = None  # cached across worlds


# ---------------------------------------------------------------------------
# Event model (the design that works): events are EVENT-ITEMS collected at one
# locked EVENT-LOCATION per event NAME, hosted in an "Event:<name>" region that
# is reachable from ANY of the event's node-components (trivial in-edges => OR
# reachability). Edge rules consult `state.has("Event: <name>")` via the real
# Rules.compile_to_lambda. This is monotonic (fill-friendly) AND keeps the event
# locations reachable under accessibility:full — verified: with a full loadout
# every event is reachable at every trick level, so the full/items access check
# passes. Door rando recomputes event reachability for free (graph is live).
# ---------------------------------------------------------------------------

def patched_create_regions(self):
    from BaseClasses import Region, Item, ItemClassification, Location
    from collections import defaultdict
    from dread.Locations import DreadLocation, location_name_to_id
    from dread.Rules import compile_to_lambda
    from dread.Tricks import effective_trick_levels

    g = GRAPH
    mw = self.multiworld
    player = self.player
    tl = effective_trick_levels(self.options)

    regions = {}
    menu = Region("Menu", player, mw)
    mw.regions.append(menu)
    for rid in range(g["regions"]):
        r = Region(f"R{rid}", player, mw)
        regions[rid] = r
        mw.regions.append(r)

    menu.connect(regions[g["start_comp"]], "Menu->start")

    for i, (csrc, cdst, ast) in enumerate(g["entrances"]):
        if ast.get("type") == "impossible":
            continue
        if ast.get("type") == "trivial":
            regions[csrc].connect(regions[cdst], f"e{i}")
        else:
            regions[csrc].connect(regions[cdst], f"e{i}",
                                  rule=compile_to_lambda(ast, player, tl))

    for rid, ap_name in g["pickups"]:
        loc = DreadLocation(player, ap_name, location_name_to_id[ap_name], regions[rid])
        regions[rid].locations.append(loc)

    # One event-region + one locked event-item location per unique event name.
    ev_comps = defaultdict(set)
    for rid, ename, uid in g["events"]:
        ev_comps[ename].add(rid)
    for ename, comps in ev_comps.items():
        er = Region(f"Event:{ename}", player, mw)
        mw.regions.append(er)
        for c in comps:
            regions[c].connect(er, f"ev:{ename}<-{c}")
        loc = Location(player, f"EventLoc: {ename}", None, er)
        loc.place_locked_item(
            Item(f"Event: {ename}", ItemClassification.progression, None, player))
        er.locations.append(loc)


def patched_set_rules(self):
    player = self.player
    n_dna = int(self.options.required_artifacts.value)

    def victory(state, p=player):
        return state.has("Event: Ship", p)

    if n_dna > 0:
        dna = tuple(f"Metroid DNA {k}" for k in range(1, n_dna + 1))
        self.multiworld.completion_condition[player] = (
            lambda state, v=victory, ns=dna, p=player:
            v(state) and all(state.has(n, p) for n in ns)
        )
    else:
        self.multiworld.completion_condition[player] = victory

    # DNA prefer_bosses locking (mirror real set_rules).
    if n_dna > 0 and self.options.artifact_placement.current_key == "prefer_bosses":
        from dread.Locations import location_table
        boss = [l.name for l in location_table
                if l.pickup_type in ("corpius", "emmi", "cutscene", "corex")]
        chosen = self.random.sample(boss, min(n_dna, len(boss)))
        for k, lname in enumerate(chosen, start=1):
            iname = f"Metroid DNA {k}"
            try:
                loc = self.multiworld.get_location(lname, player)
            except KeyError:
                continue
            item = next((i for i in self.multiworld.itempool
                         if i.player == player and i.name == iname), None)
            if item is not None:
                self.multiworld.itempool.remove(item)
                loc.place_locked_item(item)


def _sphere1(mw, player):
    """Reachable non-event locations from the START state (precollected only).
    Proves the graph actually GATES — should be a small sphere, not all 149."""
    from BaseClasses import CollectionState
    st = CollectionState(mw)
    for it in mw.precollected_items[player]:
        st.collect(it, prevent_sweep=True)
    st.update_reachable_regions(player)
    return sum(1 for loc in mw.get_locations(player)
               if loc.address is not None and loc.can_reach(st))


def run_once(explicit_indirect, n_slots=1, options=None, seed=0):
    from test.general import setup_multiworld, gen_steps
    from Fill import distribute_items_restrictive
    from dread.World import DreadWorld

    DreadWorld.create_regions = patched_create_regions
    DreadWorld.set_rules = patched_set_rules
    DreadWorld.explicit_indirect_conditions = explicit_indirect

    worlds = [DreadWorld] * n_slots
    opts = options or {}

    t0 = time.perf_counter()
    mw = setup_multiworld(worlds, gen_steps, seed=seed,
                          options=[opts] * n_slots)
    t_setup = time.perf_counter() - t0

    t1 = time.perf_counter()
    distribute_items_restrictive(mw)
    t_fill = time.perf_counter() - t1

    ok = True
    state = mw.get_all_state(False)
    for p in mw.player_ids:
        ok = ok and mw.has_beaten_game(state, p)
    # AP's real accessibility verdict (full/items/minimal per option).
    try:
        access = mw.fulfills_accessibility()
    except Exception as exc:  # FillError in __debug__
        access = False
        if "_VERBOSE" in globals() and _VERBOSE:
            print("    accessibility FillError:", str(exc)[:300])
    return t_setup + t_fill, ok and access, None


def _bench(label, explicit, n_slots=1, options=None, n_seeds=20):
    times = []
    fails = 0
    for seed in range(n_seeds):
        dt, ok, _ = run_once(explicit, n_slots=n_slots, options=options, seed=seed)
        times.append(dt)
        if not ok:
            fails += 1
    times.sort()
    med = times[len(times) // 2]
    flag = "" if fails == 0 else "  <<< FAIL"
    print(f"  {label:40s} median={med:5.2f}s min={times[0]:5.2f}s "
          f"max={times[-1]:5.2f}s  unsolved={fails}/{n_seeds}{flag}")
    return med, fails


_VERBOSE = False


def main():
    global GRAPH, _VERBOSE
    if "-v" in sys.argv:
        _VERBOSE = True
    t = time.perf_counter()
    GRAPH = build_graph()
    print(f"graph built in {time.perf_counter()-t:.2f}s: "
          f"{GRAPH['regions']} regions, {GRAPH['n_entrances']} entrances, "
          f"{len(GRAPH['pickups'])} pickups, {len(GRAPH['events'])} event nodes\n")
    print("(unsolved = not beatable OR fails fulfills_accessibility)\n")

    for explicit in (True, False):
        tag = "explicit" if explicit else "auto"
        print(f"=== indirect={tag} ===")
        _bench("solo accessibility=full (default)", explicit)
        _bench("solo accessibility=items", explicit,
               options={"accessibility": "items"})
        _bench("solo accessibility=minimal", explicit,
               options={"accessibility": "minimal"})
        _bench("solo full + trick_level=mastery", explicit,
               options={"trick_level": 5})
        _bench("2-slot full", explicit, n_slots=2, n_seeds=10)
        print()


if __name__ == "__main__":
    main()
