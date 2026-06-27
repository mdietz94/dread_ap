"""Cross-check compiled_rules.json against authoritative graph reachability.

For random item loadouts + trick configs, compare:

  * GROUND TRUTH — reachability over logic_graph.json (the model AP itself
    solves): fixpoint BFS where an edge is passable iff its resolved rule holds,
    with ``event`` atoms = "that event's host component is currently reachable".
  * COMPILED — evaluate the flat per-pickup rule from compiled_rules.json
    (events already inlined, dock/misc already resolved).

Soundness requirement (HARD): the compiled rule must NEVER report a pickup
reachable when the graph does not (a false-positive would let the tracker show a
locked check as in-logic). The reverse — compiled over-restricts due to the
disjunct cap — is allowed but should be ~0 at a full loadout.

Run:  python scripts/tests/validate_graph_compiled.py [--trials N] [--seed S]
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from graph_to_compiled_rules import _resolve, _transport_edges  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "apworld" / "dread" / "data"

# Every item the rules can reference (mirrors export's ITEM_CODE keys + DNA).
POOL_ITEMS = [
    "Morph Ball", "Bomb", "Cross Bomb", "Power Bomb", "Power Bomb Tank",
    "Charge Beam", "Wide Beam", "Plasma Beam", "Wave Beam", "Diffusion Beam",
    "Grapple Beam", "Super Missile", "Ice Missile", "Storm Missile",
    "Missile Tank", "Missile+ Tank", "Varia Suit", "Gravity Suit",
    "Phantom Cloak", "Flash Shift", "Flash Shift Upgrade", "Speed Booster",
    "Speed Booster Upgrade", "Spider Magnet", "Spin Boost", "Space Jump",
    "Screw Attack", "Slide", "Energy Tank", "Energy Part",
]
# Counts a "full" loadout grants (enough to satisfy sum/damage gates).
FULL_COUNTS = {"Missile Tank": 75, "Missile+ Tank": 12, "Power Bomb Tank": 16,
               "Energy Tank": 16, "Energy Part": 16, "Flash Shift Upgrade": 3,
               "Speed Booster Upgrade": 3}

BASE_HP, EPT, EPP = 99, 100, 25   # vanilla budget the exporter renders


def make_ctx(items: dict[str, int], trick_levels: dict[str, int]):
    def has(name, amt=1):
        return items.get(name, 0) >= amt

    def trick(name, level):
        return level <= trick_levels.get(name, 0)

    def summ(terms, base, thr):
        total = base
        for nm, per in terms:
            total += items.get(nm, 0) * per
        return total >= thr

    def dmg(suits, hp):
        if any(items.get(s, 0) > 0 for s in suits):
            return True
        budget = BASE_HP + EPT * items.get("Energy Tank", 0) + EPP * items.get("Energy Part", 0)
        return budget >= hp

    return has, trick, summ, dmg


def eval_ast(ast, ctx, event_reach=None) -> bool:
    has, trick, summ, dmg = ctx
    t = ast.get("type")
    if t == "trivial":
        return True
    if t == "impossible":
        return False
    if t == "item":
        return has(ast["name"], int(ast.get("amount", 1)))
    if t == "trick":
        return trick(ast["name"], int(ast.get("level", 1)))
    if t == "sum":
        return summ([(x["name"], int(x["per_unit"])) for x in ast["terms"]],
                    int(ast["base"]), int(ast["threshold"]))
    if t == "damage_threshold":
        return dmg(tuple(ast.get("suit_options", [])), int(ast["hp_needed"]))
    if t == "event":
        if event_reach is None:
            raise ValueError("event atom in compiled rule (should be inlined)")
        return ast["name"] in event_reach
    if t == "and":
        return all(eval_ast(c, ctx, event_reach) for c in ast["items"])
    if t == "or":
        return any(eval_ast(c, ctx, event_reach) for c in ast["items"])
    raise ValueError(f"unknown type {t!r}")


def graph_reachable(graph, resolved_edges, ev_comps_by_name, ctx) -> set[int]:
    """Fixpoint BFS: reachable component set for this loadout, with events
    firing when any host component is reachable (monotonic)."""
    start = graph["start_comp"]
    reach = {start}
    ev_reach: set[str] = set()
    changed = True
    while changed:
        changed = False
        # refresh fired events
        for nm, comps in ev_comps_by_name.items():
            if nm not in ev_reach and any(c in reach for c in comps):
                ev_reach.add(nm)
                changed = True
        for u, adj in resolved_edges.items():
            if u not in reach:
                continue
            for v, ast in adj:
                if v in reach:
                    continue
                if eval_ast(ast, ctx, ev_reach):
                    reach.add(v)
                    changed = True
    return reach


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=200)
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args(argv)

    graph = json.loads((DATA_DIR / "logic_graph.json").read_text())
    compiled = json.loads((DATA_DIR / "compiled_rules.json").read_text())
    rules = compiled["rules"]
    dock_sides, wreq = graph["dock_sides"], graph["weakness_requirements"]

    resolved_edges: dict[int, list] = {}
    for csrc, cdst, ast in graph["entrances"]:
        resolved_edges.setdefault(csrc, []).append((cdst, _resolve(ast, dock_sides, wreq)))
    for src, dst in _transport_edges(graph):
        resolved_edges.setdefault(src, []).append((dst, {"type": "trivial"}))

    ev_comps_by_name: dict[str, list[int]] = {}
    for comp, ename in graph["events"]:
        ev_comps_by_name.setdefault(ename, []).append(comp)
    pickup_comp = {name: comp for comp, name in graph["pickups"]}

    rng = random.Random(args.seed)
    # gather trick names from graph
    trick_names = set()

    def collect_tricks(ast):
        t = ast.get("type")
        if t == "trick":
            trick_names.add(ast["name"])
        elif t in ("and", "or"):
            for c in ast["items"]:
                collect_tricks(c)
    for _, _, ast in graph["entrances"]:
        collect_tricks(ast)
    trick_names = sorted(trick_names)

    false_pos = 0   # compiled reachable, graph NOT — UNSOUND
    false_neg = 0   # graph reachable, compiled NOT — over-restrictive (allowed)
    fp_examples, fn_examples = [], []
    full_fn = 0

    for trial in range(args.trials):
        if trial == 0:
            items = dict(FULL_COUNTS)
            for it in POOL_ITEMS:
                items.setdefault(it, 9)
            tl = {n: 5 for n in trick_names}   # full loadout, all tricks maxed
            is_full = True
        else:
            items = {}
            for it in POOL_ITEMS:
                if rng.random() < 0.55:
                    items[it] = rng.choice([1, 1, 1, FULL_COUNTS.get(it, 1)])
            tl = {n: rng.choice([0, 0, 1, 2, 3, 5]) for n in trick_names}
            is_full = False

        ctx = make_ctx(items, tl)
        reach = graph_reachable(graph, resolved_edges, ev_comps_by_name, ctx)

        for name, comp in pickup_comp.items():
            g_ok = comp in reach
            rule = rules.get(name)
            c_ok = eval_ast(rule, ctx) if rule is not None else False
            if c_ok and not g_ok:
                false_pos += 1
                if len(fp_examples) < 5:
                    fp_examples.append((name, sorted(items), tl))
            elif g_ok and not c_ok:
                false_neg += 1
                if is_full:
                    full_fn += 1
                if len(fn_examples) < 5:
                    fn_examples.append(name)

    print(f"trials={args.trials} pickups={len(pickup_comp)} tricks={len(trick_names)}")
    print(f"FALSE POSITIVES (unsound: compiled reachable, graph not): {false_pos}")
    print(f"false negatives (over-restrictive, allowed): {false_neg}")
    print(f"full-loadout false negatives (should be 0): {full_fn}")
    if fp_examples:
        print("FP examples:")
        for name, its, tl in fp_examples:
            print("  ", name, "items=", its)
    if full_fn and fn_examples:
        print("full-loadout FN examples:", fn_examples)

    ok = (false_pos == 0 and full_fn == 0)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
