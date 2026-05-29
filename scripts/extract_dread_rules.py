"""Compile Randovania Dread logic JSON → AP-shaped compiled_rules.json.

Reads ``.dread-cache/randovania-logic/{header,Elun,...}.json`` and emits
two artifacts the apworld consumes:

  apworld/dread_archipelago/data/compiled_rules.json   per-location rule AST
  apworld/dread_archipelago/data/events.json           event-item pool entries

The serialized AST is intentionally simple — no Python objects, no
lambdas — so the apworld can re-compile against the live ``player``
index at world-creation time without importing this script.

AST shape:

    {"type": "and",  "items": [...]}     all children must hold
    {"type": "or",   "items": [...]}     at least one child must hold
    {"type": "item", "name": "...", "amount": 1}
    {"type": "event", "name": "..."}
    {"type": "trick", "name": "...", "level": 1}   v0.1: always False
    {"type": "damage", "kind": "Heat" | "Cold" | "OOB" | "Damage" | "Lava"}
    {"type": "trivial"}                  always True
    {"type": "impossible"}               always False

Milestone 1 coverage (per docs/randovania-logic-port.md):
    - Elun only (compile_area runs for any area, but only Elun's rules
      are written by default; --all compiles every area as a stress test)
    - Tricks default to OFF (every TrickReq → Impossible)
    - Damage requirements collapse to suit ownership (Lava/Heat → Varia
      or Gravity; Cold → Gravity; raw Damage → trivial in v0.1)
    - Entry point for non-Artaria areas is the cross-region dock(s)
      because the cross-region access graph itself is Milestone 2.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / ".dread-cache" / "randovania-logic"
DATA_DIR = REPO_ROOT / "apworld" / "dread_archipelago" / "data"

ALL_AREAS = [
    "Artaria", "Burenia", "Cataris", "Dairon", "Elun",
    "Ferenia", "Ghavoran", "Hanubia", "Itorash",
]
MILESTONE_1_AREAS = ["Elun"]


# ---------------------------------------------------------------------------
# Item name mapping: Randovania short_name → our items.json name (or None
# for items that don't exist in our pool, e.g. starting equipment).
# ---------------------------------------------------------------------------

RDV_ITEM_TO_AP: dict[str, str | None] = {
    # Starting equipment / no AP item
    "Nothing": None,
    "Power": None,                 # Samus always has Power Beam
    "PowerSuit": None,             # Vanilla starting suit
    "MissileLauncher": "Missile Tank",   # First Missile Tank unlocks firing
    "MainPB": "Power Bomb",        # Bomb capability == having the Power Bomb item
    "Supers": "Missile+ Tank",     # Super Missile gating == having a Missile+ Tank
    "Hyper": None,                 # Hyper Beam is the final-form-only weapon
    "HyperSuit": None,             # Metroid Suit is plot-granted in vanilla
    "Metroidnization": None,       # Plot event (no item-pool entry in v0.1)

    # Direct beam weapons
    "Wide": "Wide Beam",
    "Plasma": "Plasma Beam",
    "Wave": "Wave Beam",
    "Charge": "Charge Beam",
    "Diffusion": "Diffusion Beam",
    "Grapple": "Grapple Beam",

    # Missiles
    "Ice": "Ice Missile",
    "Storm": "Storm Missile",

    # Suits + utility
    "Varia": "Varia Suit",
    "Gravity": "Gravity Suit",
    "Cloak": "Phantom Cloak",
    "Flash": "Flash Shift",
    "Pulse": "Pulse Radar",
    "Morph": "Morph Ball",
    "Bomb": "Bomb",
    "Cross": "Cross Bomb",
    "Magnet": "Spider Magnet",
    "Speed": "Speed Booster",
    "Spin": "Spin Boost",
    "Space": "Space Jump",
    "Screw": "Screw Attack",
    "Slide": "Slide",

    # Ammo — in our pool the corresponding pickup grants ammo + unlock both.
    # MissileAmmo: any Missile Tank in inventory satisfies the requirement.
    # PBAmmo: the Power Bomb pickup grants the first ammo too.
    "MissileAmmo": "Missile Tank",
    "PBAmmo": "Power Bomb",

    # Tanks / fragments
    "ETank": "Energy Tank",
    "EFragment": "Energy Part",
    "FlashUpgrade": "Flash Shift Upgrade",
    "SpeedBoostUpgrade": "Speed Booster Upgrade",

    # DNA / Artifacts — these are 12 items in upstream but our v0.1 pool
    # doesn't include them. Treat as Impossible until Milestone 2 adds them.
    **{f"Artifact{i}": None for i in range(1, 13)},
}


# Ammo / tank items where Randovania's "amount" represents a raw count
# (75 missiles, 2 power bombs, 5 e-tanks) but our pool doesn't carry that
# granularity. Collapse to amount=1 for v0.1.
_AMMO_OR_TANK_ITEMS = {
    "MissileAmmo", "PBAmmo", "EFragment", "ETank", "Supers",
    "MissileLauncher", "MainPB",
}


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

TRIVIAL: dict = {"type": "trivial"}
IMPOSSIBLE: dict = {"type": "impossible"}


def _key(ast: dict) -> str:
    """Canonical key for deduping AST nodes."""
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
    # dedupe by canonical key, preserving order
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


def absorb_or(or_ast: dict) -> dict:
    """Drop redundant disjuncts: if A is an AND that is a superset of
    another AND B, A is implied by B and can be removed (because B is
    weaker / easier to satisfy)."""
    if or_ast.get("type") != "or":
        return or_ast
    items = or_ast["items"]
    sets = []
    for it in items:
        if it["type"] == "and":
            sets.append((_key(it), frozenset(_key(c) for c in it["items"]), it))
        else:
            sets.append((_key(it), frozenset([_key(it)]), it))
    keep: list[dict] = []
    for i, (ki, si, asti) in enumerate(sets):
        dominated = False
        for j, (kj, sj, _) in enumerate(sets):
            if i == j:
                continue
            # sj implies si if sj ⊆ si and sj is strictly smaller
            # (so si requires more items than sj → drop si)
            if sj < si:
                dominated = True
                break
        if not dominated:
            keep.append(asti)
    return mk_or(keep)


# ---------------------------------------------------------------------------
# Requirement translation (Randovania req tree → our AST)
# ---------------------------------------------------------------------------

class CompileError(RuntimeError):
    pass


@dataclass
class Header:
    items_by_short: dict[str, dict]
    events_by_name: dict[str, dict]
    tricks_by_short: dict[str, dict]
    damages_by_short: dict[str, dict]
    templates: dict[str, dict]          # template_name -> requirement tree
    dock_weakness: dict[str, dict]      # (dock_type, weakness_name) -> {open, lock}
    starting_location: dict

    @classmethod
    def from_json(cls, hdr: dict) -> "Header":
        rdb = hdr["resource_database"]
        items_by_short = rdb["items"]
        events_by_name = rdb["events"]
        tricks_by_short = rdb["tricks"]
        damages_by_short = rdb["damage"]
        templates = rdb.get("requirement_template", {})

        dock_weakness = {}
        dw_root = hdr["dock_weakness_database"]["types"]
        for dock_type, td in dw_root.items():
            for wname, w in td.get("items", {}).items():
                dock_weakness[(dock_type, wname)] = w
        return cls(
            items_by_short=items_by_short,
            events_by_name=events_by_name,
            tricks_by_short=tricks_by_short,
            damages_by_short=damages_by_short,
            templates=templates,
            dock_weakness=dock_weakness,
            starting_location=hdr["starting_location"],
        )


def translate_requirement(
    req: dict | None,
    header: Header,
    *,
    template_stack: tuple[str, ...] = (),
) -> dict:
    """Translate a Randovania requirement tree node into our AST.

    `req` is None for missing connections (treated as Impossible).
    """
    if req is None:
        return IMPOSSIBLE
    t = req["type"]
    data = req.get("data", {})

    if t == "and":
        children = [translate_requirement(c, header, template_stack=template_stack)
                    for c in data.get("items", [])]
        return mk_and(children)

    if t == "or":
        children = [translate_requirement(c, header, template_stack=template_stack)
                    for c in data.get("items", [])]
        return mk_or(children)

    if t == "node":
        # "Have you visited this node before?" Randovania uses this for
        # back-paths after a pickup is collected (see Artaria Melee
        # Tutorial Room comment in the upstream JSON). v0.1 doesn't
        # model visit-history; treat as Impossible so a path like
        # `Morph OR node(...)` correctly collapses to `Morph`. Real
        # back-path handling is M2.
        return IMPOSSIBLE

    if t == "template":
        name = data if isinstance(data, str) else data.get("template")
        if name in template_stack:
            # Cyclic template — treat as trivial to avoid infinite recursion
            # (cycles in Randovania templates are unusual; surface as a
            # warning rather than crash).
            print(f"  WARN: cyclic template {name!r}, returning trivial", file=sys.stderr)
            return TRIVIAL
        if name not in header.templates:
            raise CompileError(f"unknown template {name!r}")
        # Templates are wrapped as {"display_name": ..., "requirement": <tree>}.
        tmpl = header.templates[name]
        body = tmpl["requirement"] if isinstance(tmpl, dict) and "requirement" in tmpl else tmpl
        return translate_requirement(
            body, header,
            template_stack=template_stack + (name,),
        )

    if t == "resource":
        rtype = data["type"]
        rname = data["name"]
        amount = int(data.get("amount", 1))
        negate = bool(data.get("negate", False))

        if negate:
            # Rare; means "must NOT have this resource". For v0.1 we don't
            # model negation in state.has predicates. Conservative choice:
            # treat as impossible (the path requires not having something,
            # so we don't claim it's reachable).
            return IMPOSSIBLE

        if rtype == "items":
            if rname not in header.items_by_short:
                raise CompileError(f"unknown item short_name {rname!r}")
            ap_name = RDV_ITEM_TO_AP.get(rname, "<unmapped>")
            if ap_name == "<unmapped>":
                raise CompileError(
                    f"item {rname!r} not in RDV_ITEM_TO_AP — add an entry"
                )
            if ap_name is None:
                # Starting equipment / not an AP item — trivially satisfied
                return TRIVIAL
            # v0.1 collapses ammo/tank counts to 1: our pool can't satisfy
            # state.has("Missile Tank", 75) since the requirement is for raw
            # Randovania-units of MissileAmmo. We approximate "you have any
            # Missile Tank" → can fire missiles. Real counting is M2.
            if rname in _AMMO_OR_TANK_ITEMS:
                amount = 1
            return {"type": "item", "name": ap_name, "amount": amount}

        if rtype == "events":
            if rname not in header.events_by_name:
                raise CompileError(f"unknown event {rname!r}")
            return {"type": "event", "name": rname}

        if rtype == "tricks":
            # v0.1: enable level-1 ("Beginner") tricks; treat anything
            # higher as impossible. Most vanilla pickups marked as
            # trick-1 ARE expected of a normal Dread playthrough
            # (basic shinesparks, bomb jumps from clear ledges, etc.).
            # Real trick-level UI option lands in Milestone 2.
            if amount <= 1:
                return TRIVIAL
            return IMPOSSIBLE

        if rtype == "damage":
            return translate_damage(rname, amount)

        if rtype == "misc":
            # 'Combat', 'Final Boss', etc. — generally story flags.
            # For v0.1 treat as trivial except a known-impossible set.
            return TRIVIAL

        if rtype == "versions":
            # Game version requirement; we target one version, so trivial.
            return TRIVIAL

        raise CompileError(f"unknown resource type {rtype!r}")

    raise CompileError(f"unknown requirement type {t!r}")


def translate_damage(kind: str, amount: int) -> dict:
    """Map a damage requirement to suit ownership (v0.1)."""
    if kind in ("Heat", "Lava"):
        # Vanilla: Varia required for hot areas (Cataris). Gravity also
        # protects against heat.
        return mk_or([
            {"type": "item", "name": "Varia Suit", "amount": 1},
            {"type": "item", "name": "Gravity Suit", "amount": 1},
        ])
    if kind == "Cold":
        # Cold rooms in Ferenia — gravity / varia both fine in vanilla.
        return mk_or([
            {"type": "item", "name": "Gravity Suit", "amount": 1},
            {"type": "item", "name": "Varia Suit", "amount": 1},
        ])
    if kind == "OOB":
        # Out-of-bounds damage — implies unintended route; v0.1 treats as
        # impossible to avoid encoding tricks via the back door.
        return IMPOSSIBLE
    if kind == "Damage":
        # Generic damage — for v0.1 assume the player has enough HP. We
        # don't model E-Tank counting yet; revisit in Milestone 2.
        return TRIVIAL
    raise CompileError(f"unknown damage kind {kind!r}")


# ---------------------------------------------------------------------------
# Graph + reachability
# ---------------------------------------------------------------------------

NodeKey = tuple[str, str]   # (sub_area, node_name)


@dataclass
class CompiledArea:
    name: str
    rules: dict[str, dict]                # actor_name -> AST
    pickup_indices: dict[str, int]        # actor_name -> Randovania pickup_index
    events_used: set[str] = field(default_factory=set)
    cross_region_exits: list[dict] = field(default_factory=list)
    # event_name -> reach AST for the event node(s) in this area.
    # If an event has multiple nodes in the same area, the rules are
    # OR'd together (the player triggers the event from any reachable
    # node).
    event_rules: dict[str, dict] = field(default_factory=dict)


def build_graph(
    area_data: dict,
    header: Header,
) -> tuple[dict[NodeKey, list[tuple[NodeKey, dict]]], dict[NodeKey, dict]]:
    """Return (edges, nodes). edges[u] = [(v, requirement_ast), ...].

    Cross-region docks are represented by ``("__EXIT__", "<region>::<area>::<node>")``
    target keys so we can recognize and ignore them in reachability while
    still surfacing them as region exits.
    """
    nodes: dict[NodeKey, dict] = {}
    edges: dict[NodeKey, list[tuple[NodeKey, dict]]] = {}
    region_name = area_data["name"]

    for sub_name, sub in area_data["areas"].items():
        for n_name, n in sub["nodes"].items():
            key = (sub_name, n_name)
            nodes[key] = n
            for tgt_name, req in n.get("connections", {}).items():
                tgt = (sub_name, tgt_name)
                edges.setdefault(key, []).append(
                    (tgt, translate_requirement(req, header)),
                )

    # Add inter-area edges for docks (within the same region) plus
    # cross-region exits surfaced via a sentinel target.
    for (sub_name, n_name), n in list(nodes.items()):
        if n.get("node_type") != "dock":
            continue
        dc = n.get("default_connection")
        if not dc:
            continue
        dock_type = n.get("dock_type")
        weakness = n.get("default_dock_weakness")
        dock_req = dock_open_requirement(header, dock_type, weakness)
        # Honor any explicit per-node overrides.
        override_open = n.get("override_default_open_requirement")
        if override_open is not None:
            dock_req = translate_requirement(override_open, header)
        if dc["region"] == area_data["name"]:
            tgt = (dc["area"], dc["node"])
            edges.setdefault((sub_name, n_name), []).append((tgt, dock_req))
        else:
            tgt = ("__EXIT__", f"{dc['region']}::{dc['area']}::{dc['node']}")
            edges.setdefault((sub_name, n_name), []).append((tgt, dock_req))

    return edges, nodes


def dock_open_requirement(header: Header, dock_type: str, weakness: str) -> dict:
    """Look up a dock's open requirement (combine 'requirement' + lock)."""
    if not dock_type or not weakness:
        return TRIVIAL
    w = header.dock_weakness.get((dock_type, weakness))
    if w is None:
        # Unknown weakness — generous default. (Shouldn't happen for the
        # standard door types; surface as a warning so we can fill the gap.)
        print(f"  WARN: unknown dock_weakness ({dock_type}, {weakness})",
              file=sys.stderr)
        return TRIVIAL
    base = translate_requirement(w.get("requirement"), header)
    lock = w.get("lock")
    if lock is None:
        return base
    lock_req = translate_requirement(lock.get("requirement"), header)
    # The lock must additionally be openable. For "front-blast-back-free-
    # unlock"-style locks this is approximate but conservative-enough for v0.1.
    return mk_and([base, lock_req])


def find_entries(area_data: dict, nodes: dict[NodeKey, dict],
                 starting_location: dict) -> list[NodeKey]:
    """Entry nodes for area-local BFS.

    - If the global starting_location is in this region, include its node.
    - Otherwise, any cross-region dock pointing INTO this area is an entry
      (we use the dock node itself; its outbound edges represent "having
      arrived from elsewhere").
    - Any node with valid_starting_location=true is also an entry (covers
      Randovania's "random starting location" mode).
    """
    entries: list[NodeKey] = []

    # Cross-region docks: their default_connection.region != ours means
    # the dock leads OUT, but the dock node itself is reachable only by
    # arriving from outside, which we approximate by treating it as a
    # start node.
    region_name = area_data["name"]
    for key, n in nodes.items():
        if n.get("node_type") == "dock":
            dc = n.get("default_connection") or {}
            if dc.get("region") and dc["region"] != region_name:
                entries.append(key)
        if n.get("valid_starting_location"):
            entries.append(key)

    if starting_location.get("region") == region_name:
        entries.append((starting_location["area"], starting_location["node"]))

    # Dedupe preserving order
    seen: set[NodeKey] = set()
    out: list[NodeKey] = []
    for k in entries:
        if k not in seen and k in nodes:
            seen.add(k)
            out.append(k)
    return out


# DNF representation: a set of disjuncts, where each disjunct is a
# frozenset of "atomic" requirement keys. An atomic requirement is a
# tuple identifying one needed resource (or a trick-as-trivial / damage
# expansion). Empty outer set = unreachable; set containing the empty
# frozenset = trivially reachable.
#
# Frozensets are O(1) equality + hashable, so DNF operations are MUCH
# faster than re-canonicalizing nested-dict ASTs. ASTs are only
# reconstructed once at output time (see `dnf_to_ast`).

Atom = tuple                    # e.g. ("item", "Morph Ball", 1)
Disjunct = frozenset            # frozenset[Atom]
DNF = frozenset                 # frozenset[Disjunct]

EMPTY_DNF: DNF = frozenset()
TRIVIAL_DNF: DNF = frozenset({frozenset()})


def ast_to_dnf(ast: dict, max_disjuncts: int = 32) -> DNF:
    """Convert an AST to DNF, with a hard cap on disjunct count.

    Edge requirements are typically small (≤ 4 ORs of singletons) so
    DNF blowup is rare. Templates like 'Lay Any Bomb' = OR(Bomb, Cross,
    PB) chain across path edges multiplicatively, hence the cap. Past
    the cap, drop the largest disjuncts (most restrictive paths) —
    those are the ones the player is least likely to take.
    """
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
        # Translator should have collapsed these, but be defensive.
        if int(ast.get("level", 1)) <= 1:
            return TRIVIAL_DNF
        return EMPTY_DNF
    if t == "damage":
        # Translator collapses damage to OR of suits etc.; reaching here
        # means a raw damage node slipped through — be permissive.
        return TRIVIAL_DNF
    if t == "and":
        # AND = product of child DNFs
        result: DNF = TRIVIAL_DNF
        for child in ast["items"]:
            child_dnf = ast_to_dnf(child, max_disjuncts)
            if not child_dnf:
                return EMPTY_DNF
            result = frozenset({a | b for a in result for b in child_dnf})
            result = _absorb_dnf(result)
            if len(result) > max_disjuncts:
                result = frozenset(sorted(result, key=len)[:max_disjuncts])
        return result
    if t == "or":
        result: DNF = EMPTY_DNF
        for child in ast["items"]:
            result = result | ast_to_dnf(child, max_disjuncts)
            result = _absorb_dnf(result)
            if len(result) > max_disjuncts:
                result = frozenset(sorted(result, key=len)[:max_disjuncts])
        return result
    raise ValueError(f"unknown AST type in ast_to_dnf: {t!r}")


def _absorb_dnf(dnf: DNF) -> DNF:
    """Drop dominated disjuncts: if disjunct B ⊂ A then A is redundant
    (anyone meeting B's items also meets A's)."""
    if len(dnf) <= 1:
        return dnf
    items = list(dnf)
    keep: list[Disjunct] = []
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


def dnf_to_ast(dnf: DNF) -> dict:
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
            else:
                raise ValueError(f"unknown atom kind in dnf_to_ast: {a}")
        disjuncts.append(mk_and(and_items))
    return mk_or(disjuncts)


def enumerate_paths(
    entries: list[NodeKey],
    edges: dict[NodeKey, list[tuple[NodeKey, dict]]],
    targets: set[NodeKey],
    *,
    max_iterations: int = 200,
    max_disjuncts_per_node: int = 12,
    max_disjuncts_per_edge: int = 16,
) -> dict[NodeKey, dict]:
    """Symbolic reachability via a Bellman-Ford-style fixed-point over a
    DNF representation.

    For each node, reach[node] is a DNF whose disjuncts are the minimal
    item-sets that suffice to reach that node from any entry. We
    repeatedly propagate along edges (AND with the edge's DNF, then OR
    into the target node's reach) until nothing changes or
    max_iterations is reached.

    Caps keep the DNF bounded — large templates like 'Lay Any Bomb' fan
    out multiplicatively, so absorption + size cap prevent runaway.
    Past the cap we drop the longest disjuncts (the hardest paths)
    rather than the shortest, biasing toward over-permissive logic
    rather than under-permissive impossibility flags.
    """
    # Build full node set
    all_nodes: set[NodeKey] = set(edges.keys())
    for adj in edges.values():
        for tgt, _ in adj:
            all_nodes.add(tgt)
    for e in entries:
        all_nodes.add(e)

    # Pre-translate edge requirements to DNF once
    edge_dnf: dict[NodeKey, list[tuple[NodeKey, DNF]]] = {}
    for u, adj in edges.items():
        edge_dnf[u] = []
        for v, req in adj:
            d = ast_to_dnf(req, max_disjuncts=max_disjuncts_per_edge)
            if not d:
                continue
            edge_dnf[u].append((v, d))

    reach: dict[NodeKey, DNF] = {n: EMPTY_DNF for n in all_nodes}
    for e in entries:
        reach[e] = TRIVIAL_DNF

    from collections import deque
    queue = deque(entries)
    in_queue: set[NodeKey] = set(entries)

    iterations = 0
    while queue and iterations < max_iterations * len(all_nodes):
        u = queue.popleft()
        in_queue.discard(u)
        iterations += 1
        if not reach[u]:
            continue
        for v, d in edge_dnf.get(u, ()):
            # new = AND(reach[u], edge_dnf) — product of disjuncts
            new_paths = frozenset({pu | pe for pu in reach[u] for pe in d})
            combined = reach[v] | new_paths
            combined = _absorb_dnf(combined)
            if len(combined) > max_disjuncts_per_node:
                combined = frozenset(sorted(combined, key=len)[:max_disjuncts_per_node])
            if combined != reach[v]:
                reach[v] = combined
                if v not in in_queue:
                    queue.append(v)
                    in_queue.add(v)

    if queue:
        print(f"  WARN: enumerate_paths hit iteration cap (queue still had {len(queue)} entries)",
              file=sys.stderr)

    out: dict[NodeKey, dict] = {}
    for tgt in targets:
        out[tgt] = dnf_to_ast(reach.get(tgt, EMPTY_DNF))
    return out


def collect_events_used(ast: dict, out: set[str]) -> None:
    t = ast["type"]
    if t == "event":
        out.add(ast["name"])
    elif t in ("and", "or"):
        for c in ast["items"]:
            collect_events_used(c, out)


def compile_area(area_data: dict, header: Header) -> CompiledArea:
    edges, nodes = build_graph(area_data, header)
    entries = find_entries(area_data, nodes, header.starting_location)
    if not entries:
        print(f"  WARN: no entries for {area_data['name']}", file=sys.stderr)
    # Targets: every pickup + every cross-region exit dock + every event node.
    # Event nodes are added so we can compute per-event reach rules — required
    # for M2 event-as-item plumbing.
    targets: set[NodeKey] = set()
    for key, n in nodes.items():
        if n.get("node_type") == "pickup":
            targets.add(key)
        if n.get("node_type") == "event":
            targets.add(key)
        if n.get("node_type") == "dock":
            dc = n.get("default_connection") or {}
            if dc.get("region") and dc["region"] != area_data["name"]:
                targets.add(key)
    reach = enumerate_paths(entries, edges, targets)

    rules: dict[str, dict] = {}
    pickup_indices: dict[str, int] = {}
    events_used: set[str] = set()
    exits: list[dict] = []
    event_rules_per_name: dict[str, list[dict]] = {}

    for key, n in nodes.items():
        if n.get("node_type") == "pickup":
            actor_name = n["extra"].get("actor_name")
            if not actor_name:
                # Boss / cutscene pickup — no actor; skip for v0.1
                continue
            ast = reach.get(key, IMPOSSIBLE)
            rules[actor_name] = ast
            if n.get("pickup_index") is not None:
                pickup_indices[actor_name] = n["pickup_index"]
            collect_events_used(ast, events_used)
        if n.get("node_type") == "event":
            ename = n.get("event_name")
            if not ename:
                continue
            event_rules_per_name.setdefault(ename, []).append(
                reach.get(key, IMPOSSIBLE)
            )
        if n.get("node_type") == "dock":
            dc = n.get("default_connection") or {}
            if dc.get("region") and dc["region"] != area_data["name"]:
                exits.append({
                    "from": {"area": key[0], "node": key[1]},
                    "to": dc,
                    "requirement": reach.get(key, IMPOSSIBLE),
                })

    # Collapse multiple-node-per-event-name into a single OR.
    event_rules: dict[str, dict] = {
        name: mk_or(asts) for name, asts in event_rules_per_name.items()
    }

    return CompiledArea(
        name=area_data["name"],
        rules=rules,
        pickup_indices=pickup_indices,
        events_used=events_used,
        cross_region_exits=exits,
        event_rules=event_rules,
    )


# ---------------------------------------------------------------------------
# Actor → AP location-name lookup (uses our locations.json)
# ---------------------------------------------------------------------------

def load_ap_locations() -> tuple[dict[tuple[str, str], dict], dict[str, str]]:
    """Returns ((region, actor_name) -> location, region -> scenario_id).

    Matching is case-sensitive and includes underscores. Randovania's
    ``extra.actor_name`` matches our ``locations.json`` ``actor`` field
    exactly across all 9 areas (verified at scripted extract time), so
    no normalization is needed — and the two ``Item_MissileTank001`` vs
    ``item_missiletank_001``-style pairs in Artaria/Cataris would
    collide under naive lowercase+strip-underscore normalization.
    """
    raw = json.loads((DATA_DIR / "locations.json").read_text())
    by_key: dict[tuple[str, str], dict] = {}
    for l in raw:
        if l.get("pickup_type") != "actor":
            continue
        by_key[(l["region"], l["actor"])] = l
    return by_key, {l["region"]: l["scenario"] for l in raw}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--areas",
        nargs="+",
        default=None,
        help="area names to compile (default: Milestone 1 list — just Elun)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="compile every area (stress-test the pipeline)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat any unmapped actor or compile warning as a hard error",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DATA_DIR / "compiled_rules.json",
        help="output path for compiled_rules.json",
    )
    args = parser.parse_args(argv)

    if args.all:
        areas = ALL_AREAS
    elif args.areas:
        areas = args.areas
    else:
        areas = MILESTONE_1_AREAS

    pinned = (CACHE_DIR / "PINNED_COMMIT.txt").read_text().strip()
    header_data = json.loads((CACHE_DIR / "header.json").read_text())
    header = Header.from_json(header_data)

    ap_loc_by_key, ap_region_scenario = load_ap_locations()
    ap_items = json.loads((DATA_DIR / "items.json").read_text())
    ap_locations = json.loads((DATA_DIR / "locations.json").read_text())
    existing_item_max = max(it["ap_id"] for it in ap_items)
    existing_loc_max = max(l["ap_id"] for l in ap_locations)

    out_rules: dict[str, dict] = {}
    all_events: set[str] = set()
    unmapped: list[str] = []
    cross_exits: list[dict] = []
    # event_name -> list of (region, reach_ast) — one entry per area that
    # contains an event node with this name. Aggregated below.
    event_rules_by_name: dict[str, list[tuple[str, dict]]] = {}

    for area_name in areas:
        path = CACHE_DIR / f"{area_name}.json"
        if not path.exists():
            print(f"missing cache file: {path}", file=sys.stderr)
            return 2
        area = json.loads(path.read_text())
        print(f"compiling {area_name} ...")
        compiled = compile_area(area, header)
        all_events |= compiled.events_used
        cross_exits.extend(compiled.cross_region_exits)

        for ename, ast in compiled.event_rules.items():
            event_rules_by_name.setdefault(ename, []).append((compiled.name, ast))

        for actor_name, ast in compiled.rules.items():
            key = (compiled.name, actor_name)
            loc = ap_loc_by_key.get(key)
            if loc is None:
                msg = f"  no AP location for actor {actor_name!r} in {compiled.name}"
                if args.strict:
                    unmapped.append(msg)
                else:
                    print(msg, file=sys.stderr)
                continue
            out_rules[loc["name"]] = ast

    if unmapped and args.strict:
        for m in unmapped:
            print(m, file=sys.stderr)
        return 3

    # Translate victory_condition + collect its events too
    victory_ast = translate_requirement(header_data["victory_condition"], header)
    collect_events_used(victory_ast, all_events)

    # ---- Build the events list -----------------------------------------------
    # Union the set of event NAMES referenced from any rule (all_events)
    # plus the names we observed as event NODES while compiling. Some
    # events show up only as node defs (no rule references them); some
    # show up only as references (the node lives in an uncompiled area).
    # An event with no node we ever saw gets IMPOSSIBLE — the only safe
    # default since we can't prove reachability.
    event_names = sorted(set(all_events) | set(event_rules_by_name.keys()))
    event_item_base = existing_item_max + 1
    event_loc_base = existing_loc_max + 1

    events_out: list[dict] = []
    for i, ename in enumerate(event_names):
        per_area = event_rules_by_name.get(ename, [])
        if not per_area:
            # Referenced from rules but no node found — defensive default.
            print(f"  WARN: event {ename!r} referenced but no event node "
                  f"found in any compiled area; defaulting to IMPOSSIBLE",
                  file=sys.stderr)
            rule = IMPOSSIBLE
            region = ""
        else:
            # OR across areas — player triggers the event from any
            # reachable node. Region tag = first contributing area
            # (alphabetical for determinism).
            per_area_sorted = sorted(per_area, key=lambda x: x[0])
            rule = mk_or([ast for _, ast in per_area_sorted])
            region = per_area_sorted[0][0]
        events_out.append({
            "name": ename,
            "region": region,
            "rule": rule,
            "item_ap_id": event_item_base + i,
            "location_ap_id": event_loc_base + i,
        })

    output = {
        "pinned_commit": pinned,
        "areas_compiled": areas,
        "victory_condition": victory_ast,
        "events": events_out,
        "rules": out_rules,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2))
    print(f"wrote {args.out} — {len(out_rules)} rules, {len(events_out)} events")

    # events.json kept as a back-compat view: bare name list only.
    events_path = DATA_DIR / "events.json"
    events_path.write_text(json.dumps({
        "pinned_commit": pinned,
        "events": [e["name"] for e in events_out],
    }, indent=2))
    print(f"wrote {events_path} — {len(events_out)} events")

    return 0


if __name__ == "__main__":
    sys.exit(main())
