"""Derive the sole-binding ammo capacity caps from logic_graph.json.

The faithful v0.3 ammo model (World._ammo_progression_counts +
MAX_BINDING_MISSILE_CAP / MAX_BINDING_PB_CAP) rests on an empirical fact: every
missile / power-bomb ``sum`` threshold ABOVE the printed caps is OR'd with a
non-sum alternative the advancement sweep can reach, so only a small irreducible
capacity is ever the SOLE gate to 100% reachability.

This script re-derives those caps directly from the graph by walking
reachability with a hard capacity CAP on every ammo ``sum`` (a sum passes iff
its threshold <= cap) and an otherwise-maximal loadout, then finding the minimal
cap that still reaches every pickup + the Ship event, at every trick level.

Run:  python scripts/analyze_ammo_binding.py
Used by scripts/tests/test_extract_dread_rules.py::test_max_binding_ammo_cap to
pin the constants to the graph.
"""
from __future__ import annotations

import collections
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
GRAPH = os.path.join(HERE, os.pardir, "apworld", "dread", "data",
                     "logic_graph.json")


def _load():
    with open(GRAPH, encoding="utf-8") as fh:
        return json.load(fh)


def binding_caps(graph: dict) -> tuple[int, int]:
    """Return (max_binding_missile_cap, max_binding_pb_cap): the minimal missile
    / PB capacity such that 100% of pickups + the Ship event are reachable at
    every trick level, with all items owned and all ammo sums capacity-capped."""
    dock_sides = graph["dock_sides"]
    wreq = graph["weakness_requirements"]

    def resolve(ast):
        t = ast.get("type")
        if t == "dock":
            sid = ast["side_id"]
            side = dock_sides[sid]
            key = f"{side['dock_type']}::{side['default_weakness']}"
            return wreq.get(key, {"type": "impossible"})
        if t in ("and", "or"):
            return {"type": t, "items": [resolve(c) for c in ast["items"]]}
        return ast

    entrances = [(s, d, resolve(r)) for s, d, r in graph["entrances"]]
    trick_names: set = set()

    def trk(node):
        if node.get("type") == "trick":
            trick_names.add(node["name"])
        for c in node.get("items", []):
            trk(c)

    for _s, _d, r in entrances:
        trk(r)

    adj = collections.defaultdict(list)
    for s, d, r in entrances:
        adj[s].append((d, r))
    tr = graph.get("transports", {})
    for sid, meta in tr.items():
        dd = tr.get(meta["default_dest"])
        if dd:
            adj[meta["comp"]].append((dd["comp"], {"type": "trivial"}))
    event_at = collections.defaultdict(list)
    for comp, name in graph["events"]:
        event_at[comp].append(name)
    start = graph["start_comp"]
    total = len(graph["pickups"])

    def reach(mcap, pbcap, lvl):
        tl = {n: lvl for n in trick_names}

        def ev(node, events):
            t = node["type"]
            if t == "trivial":
                return True
            if t == "impossible":
                return False
            if t == "item":
                return True  # own everything incl >=1 missile tank
            if t == "event":
                return node["name"] in events
            if t == "trick":
                return node.get("level", 1) <= tl.get(node["name"], 0)
            if t == "sum":
                names = {x["name"] for x in node["terms"]}
                cap = mcap if "Missile Tank" in names else pbcap
                return node["threshold"] <= cap
            if t == "damage_threshold":
                return True
            if t == "and":
                return all(ev(c, events) for c in node["items"])
            if t == "or":
                return any(ev(c, events) for c in node["items"])
            return False

        events: set = set()
        R = {start}
        changed = True
        while changed:
            changed = False
            frontier = list(R)
            while frontier:
                u = frontier.pop()
                for v, r in adj.get(u, []):
                    if v not in R and ev(r, events):
                        R.add(v)
                        frontier.append(v)
                        changed = True
            for comp in list(R):
                for name in event_at[comp]:
                    if name not in events:
                        events.add(name)
                        changed = True
        return sum(1 for c, _ in graph["pickups"] if c in R), ("Ship" in events)

    levels = (1, 3, 5)
    mcap = 0
    while True:
        if all(reach(mcap, 10**9, lvl) == (total, True) for lvl in levels):
            break
        mcap += 1
        if mcap > 1000:
            raise RuntimeError("missile cap search did not converge")
    pbcap = 0
    while True:
        if all(reach(10**9, pbcap, lvl) == (total, True) for lvl in levels):
            break
        pbcap += 1
        if pbcap > 100:
            raise RuntimeError("pb cap search did not converge")
    return mcap, pbcap


def main():
    g = _load()
    mcap, pbcap = binding_caps(g)
    print(f"MAX_BINDING_MISSILE_CAP = {mcap}")
    print(f"MAX_BINDING_PB_CAP = {pbcap}")


if __name__ == "__main__":
    main()
