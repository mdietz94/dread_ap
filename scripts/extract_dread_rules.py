"""Extract Randovania Dread logic → native AP region graph (logic_graph.json).

Reads ``.dread-cache/randovania-logic/{header,Elun,...}.json`` and emits:

  apworld/dread/data/logic_graph.json   native AP region-graph artifact

AST vocabulary (symbolic; compiled to lambdas at AP-generation time):

    {"type": "and",  "items": [...]}
    {"type": "or",   "items": [...]}
    {"type": "item", "name": "...", "amount": 1}
    {"type": "event", "name": "..."}          graph_mode → can_reach_region
    {"type": "trick", "name": "...", "level": N}   resolved per-trick at gen time
    {"type": "sum", "terms": [{"name", "per_unit"}, ...],
                    "base": int, "threshold": int}
    {"type": "damage_threshold",
        "suit_options": [...], "hp_needed": int}
    {"type": "dock", "side_id": "..."}        substituted by the apworld per-seed
    {"type": "trivial"}
    {"type": "impossible"}
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
DATA_DIR = REPO_ROOT / "apworld" / "dread" / "data"

# Worktree fallback: git worktrees don't carry the cache dir (it's
# .gitignored), so resolve to the primary checkout's cache if the local
# one is missing. Avoids forcing every dev branch to re-pull the upstream
# logic snapshot.
if not CACHE_DIR.exists():
    for _candidate in (
        REPO_ROOT.parent.parent.parent / ".dread-cache" / "randovania-logic",
    ):
        if _candidate.exists():
            CACHE_DIR = _candidate
            break

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
    "Supers": "Super Missile",     # green-door (Super Missile) gating == the Super Missile item
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


# Items where Randovania's "amount" is an unlock-flag, not a count: any one
# pickup satisfies the requirement. (Supers is the single Super Missile item,
# MissileLauncher/MainPB by any tank/PB.) These stay collapsed to amount=1 —
# for Super Missile that's a no-op (the pool only has one copy), but it keeps
# the unlock-flag semantics explicit alongside the genuinely-counted tanks.
#
# Counted resources have moved out:
#   - ETank / EFragment: state.has(name, player, N) handles "have N tanks"
#     directly via translate_requirement's amount pass-through.
#   - MissileAmmo / PBAmmo: routed to _translate_ammo, which emits sum nodes
#     that respect per-tank yield (2 per Missile Tank, 10 per Missile+ Tank,
#     2 per Power Bomb Tank) and the starting capacities (15 missiles, 0 PBs).
_AMMO_OR_TANK_ITEMS = {
    "Supers", "MissileLauncher", "MainPB",
}

# Vanilla starting capacities — matches starter_preset_patcher.json. These are
# game-stable for our target (Dread 2.1.0); revisit if the patcher's
# starting_items grows additional ammo-capacity entries.
_MISSILE_BASE_CAPACITY = 15
_POWER_BOMB_BASE_CAPACITY = 0


# Randovania 'misc' resources are static per-seed CONFIG booleans (door-lock
# rando, transport rando, "highly dangerous logic", power-bomb limits, etc.) —
# NOT collectible state. We resolve them at compile time against our patcher
# config so a negated misc requirement is exact ("config flag is off → NOT-flag
# holds") instead of the old conservative "negation is impossible". Values mirror
# the bundled starter preset: no door/transport rando, no highly-dangerous
# logic, power bombs nerfed, beams/missiles as separate items.
MISC_RESOURCE_VALUES: dict[str, bool] = {
    "DoorLocks": False,
    "Teleporters": False,
    "HighDanger": False,
    "NerfPowerBombs": True,
    "SeparateBeams": True,
    "SeparateMissiles": True,
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
    # dock_type -> {change_from, change_to, ...}. Defaulted so older callers /
    # fixtures that don't supply it still construct.
    dock_rando: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def from_json(cls, hdr: dict) -> "Header":
        rdb = hdr["resource_database"]
        items_by_short = rdb["items"]
        events_by_name = rdb["events"]
        tricks_by_short = rdb["tricks"]
        damages_by_short = rdb["damage"]
        templates = rdb.get("requirement_template", {})

        dock_weakness = {}
        dock_rando = {}
        dw_root = hdr["dock_weakness_database"]["types"]
        for dock_type, td in dw_root.items():
            for wname, w in td.get("items", {}).items():
                dock_weakness[(dock_type, wname)] = w
            if "dock_rando" in td:
                dock_rando[dock_type] = td["dock_rando"]
        return cls(
            items_by_short=items_by_short,
            events_by_name=events_by_name,
            tricks_by_short=tricks_by_short,
            damages_by_short=damages_by_short,
            templates=templates,
            dock_weakness=dock_weakness,
            starting_location=hdr["starting_location"],
            dock_rando=dock_rando,
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

        # misc = static per-seed config flag. Resolve against our config
        # (negation is exact here — it's not collectible state).
        if rtype == "misc":
            val = MISC_RESOURCE_VALUES.get(rname)
            if val is None:
                print(f"  WARN: unknown misc resource {rname!r}; assuming present",
                      file=sys.stderr)
                val = True
            holds = (not val) if negate else bool(val)
            return TRIVIAL if holds else IMPOSSIBLE

        if negate:
            # Negated item/event = TEMPORAL ("don't have / haven't triggered it
            # yet"). Dread's major progression (Ship, bosses, X-release, chain
            # reaction) is gated SOLELY by negated state, so Impossible breaks
            # completion. Treat as Trivial (satisfiable in the early state); the
            # forward resolver inlines events into item-only rules so AP's
            # monotonic item sweep stays consistent.
            return TRIVIAL

        if rtype == "items":
            if rname not in header.items_by_short:
                raise CompileError(f"unknown item short_name {rname!r}")
            # MissileAmmo / PBAmmo are raw-count resources (e.g. "75 missiles"
            # for a shielded door). Route to _translate_ammo which emits a sum
            # node honoring per-tank yield and starting capacity.
            if rname in ("MissileAmmo", "PBAmmo"):
                return _translate_ammo(rname, amount)
            ap_name = RDV_ITEM_TO_AP.get(rname, "<unmapped>")
            if ap_name == "<unmapped>":
                raise CompileError(
                    f"item {rname!r} not in RDV_ITEM_TO_AP — add an entry"
                )
            if ap_name is None:
                # Starting equipment / not an AP item — trivially satisfied
                return TRIVIAL
            # Unlock-flag items collapse: any single pickup satisfies the
            # capability requirement. ETank / EFragment fall through and use
            # state.has(name, player, amount) directly.
            if rname in _AMMO_OR_TANK_ITEMS:
                amount = 1
            return {"type": "item", "name": ap_name, "amount": amount}

        if rtype == "events":
            if rname not in header.events_by_name:
                raise CompileError(f"unknown event {rname!r}")
            return {"type": "event", "name": rname}

        if rtype == "tricks":
            # Keep the trick SYMBOLIC — the level (amount) is carried through to
            # the compiled output and resolved at AP-generation time against the
            # seed's per-trick config (apworld/dread/Tricks.py). A requirement
            # ``trick N`` is assumed satisfiable iff the effective level for that
            # trick is >= N. (Pre-v3 this collapsed to Trivial/Impossible against
            # one global header.trick_level and baked three files.)
            if rname not in header.tricks_by_short:
                raise CompileError(f"unknown trick {rname!r}")
            return {"type": "trick", "name": rname, "level": amount}

        if rtype == "damage":
            return translate_damage(rname, amount)

        if rtype == "versions":
            # Game version requirement; we target one version, so trivial.
            return TRIVIAL

        raise CompileError(f"unknown resource type {rtype!r}")

    raise CompileError(f"unknown requirement type {t!r}")


def translate_damage(kind: str, amount: int) -> dict:
    """Map a damage requirement to a damage_threshold (or IMPOSSIBLE for OOB).

    Randovania pre-resolves Dread's suit-multiplier math into the published
    ``amount`` per requirement, so we don't model coefficients here — suits
    are a binary shortcut OR an HP-budget check. The lambda compiler resolves
    the threshold as ``99 + 100·count(Energy Tank) + 25·count(Energy Part)``.

    Generic ``Damage`` (no suit applies) is emitted as ``damage_threshold``
    with empty ``suit_options``, BUT downstream the compile_forward driver
    walks the compiled rules to derive per-region E-Tank floors and then
    *strips* these no-suit nodes from per-location rules (replacing with
    TRIVIAL). The reason for the round-trip: the amounts are needed to set
    region_access floors that approximate vanilla HP accumulation, but
    leaving them in per-location rules deadlocks AP's fill (every Hanubia
    location ends up needing 3+ E-Tanks reachable in sphere 1). The
    region-level gate is what AP's fill can satisfy; the per-location gate
    is too tight. See ``_strip_no_suit_damage_thresholds`` and
    ``compute_region_etank_floors`` for the post-processing.
    """
    if kind in ("Heat", "Lava"):
        # Vanilla: Varia required for hot areas (Cataris). Gravity also
        # protects against heat.
        return {"type": "damage_threshold",
                "suit_options": ["Varia Suit", "Gravity Suit"],
                "hp_needed": int(amount)}
    if kind == "Cold":
        # In Dread, ONLY Gravity Suit negates cold — Varia does not. Verified
        # against Randovania's logic DB (commit 3559136d): of the 47 OR-groups
        # containing a Cold-damage branch across all 9 regions, every suit pass
        # is Gravity (the rest are a no-suit "Suitless" trick + HP-budget route);
        # Varia never appears. Listing Varia here previously put Varia-only seeds
        # in logic for cold rooms that genuinely require Gravity. (Heat keeps
        # Varia per its own mapping above.)
        return {"type": "damage_threshold",
                "suit_options": ["Gravity Suit"],
                "hp_needed": int(amount)}
    if kind == "OOB":
        # Out-of-bounds damage — implies unintended route; treat as
        # impossible to avoid encoding tricks via the back door.
        return IMPOSSIBLE
    if kind == "Damage":
        # Generic damage — kept temporarily so compile_forward can derive
        # region floors. Stripped before output (per-location, not per-region).
        return {"type": "damage_threshold",
                "suit_options": [],
                "hp_needed": int(amount)}
    raise CompileError(f"unknown damage kind {kind!r}")


def _translate_ammo(rdv_name: str, amount: int) -> dict:
    """Turn MissileAmmo/PBAmmo requirements into sum nodes.

    Randovania's ``amount`` is the raw count of missiles or power bombs needed.
    For missiles, our pool contributes 2/Missile Tank and 10/Missile+ Tank on
    top of 15 starting capacity. For power bombs, our pool contributes 2/Power
    Bomb (the launcher item, which grants firing + first capacity) and
    2/Power Bomb Tank; the launcher is also AND'd in so PB capacity without
    the ability to fire never passes (AP can hand out tanks pre-launcher)."""
    if rdv_name == "MissileAmmo":
        return {
            "type": "sum",
            "terms": [
                {"name": "Missile Tank", "per_unit": 2},
                {"name": "Missile+ Tank", "per_unit": 10},
            ],
            "base": _MISSILE_BASE_CAPACITY,
            "threshold": int(amount),
        }
    if rdv_name == "PBAmmo":
        return mk_and([
            {"type": "item", "name": "Power Bomb", "amount": 1},
            {"type": "sum",
             "terms": [
                 {"name": "Power Bomb", "per_unit": 2},
                 {"name": "Power Bomb Tank", "per_unit": 2},
             ],
             "base": _POWER_BOMB_BASE_CAPACITY,
             "threshold": int(amount)},
        ])
    raise CompileError(f"_translate_ammo called with non-ammo resource {rdv_name!r}")


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


def _disjunct_sort_key(disjunct: Disjunct) -> tuple:
    """Stable total order for the disjunct cap. Primary key = number of trick
    atoms, so item-only paths are always preferred over trick-gated ones at the
    cap — a trick-disabled player keeps any item-only path, so truncation can
    only ever *over*-restrict (never falsely-reach). Secondary key = length, so
    truncation keeps the shortest (easiest) paths and drops the longest; the
    tertiary key (sorted atoms) replaces frozenset iteration order, which is
    hash-seed-dependent, so repeated bakes are byte-reproducible.

    Disjuncts with zero trick atoms keep their pre-v3 relative order
    (``(len, sorted)``), so non-trick rules stay byte-for-byte identical."""
    n_tricks = sum(1 for a in disjunct if a[0] == "trick")
    return (n_tricks, len(disjunct), tuple(sorted(disjunct)))


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
        # Symbolic (v3): carry name + level as an opaque atom; the lambda
        # compiler resolves it against the seed's per-trick config. Treated
        # like any other cost atom by the DNF engine.
        return frozenset({frozenset({("trick", ast["name"], int(ast.get("level", 1)))})})
    if t == "sum":
        # Opaque counted-resource atom — the lambda compiler resolves
        # ``base + Σ count·per_unit ≥ threshold`` at runtime. Two sum atoms
        # with different thresholds are treated as distinct (no dominance
        # inference); slightly over-conservative but the DNF cap handles it.
        terms = tuple((t_["name"], int(t_["per_unit"])) for t_ in ast["terms"])
        atom = ("sum", terms, int(ast["base"]), int(ast["threshold"]))
        return frozenset({frozenset({atom})})
    if t == "damage_threshold":
        # Opaque HP-budget atom — the lambda compiler resolves
        # ``any suit OR 99 + 100·ETank + 25·EPart ≥ hp_needed``. We keep it as
        # a single atom (not expanded into suit-disjuncts) so the DNF stays
        # bounded; the lambda evaluates the suit shortcut at solve time.
        suits = tuple(ast.get("suit_options", []))
        atom = ("damage_threshold", suits, int(ast["hp_needed"]))
        return frozenset({frozenset({atom})})
    if t == "damage":
        # v1-schema leftover. Translator no longer emits these; raise so
        # stale artifacts can't sneak past the schema check.
        raise ValueError("stale 'damage' AST node — regenerate compiled_rules.json")
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
                result = frozenset(sorted(result, key=_disjunct_sort_key)[:max_disjuncts])
        return result
    if t == "or":
        result: DNF = EMPTY_DNF
        for child in ast["items"]:
            result = result | ast_to_dnf(child, max_disjuncts)
            result = _absorb_dnf(result)
            if len(result) > max_disjuncts:
                result = frozenset(sorted(result, key=_disjunct_sort_key)[:max_disjuncts])
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
                combined = frozenset(sorted(combined, key=_disjunct_sort_key)[:max_disjuncts_per_node])
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
# Global cross-region reachability (Gate B)
# ---------------------------------------------------------------------------
#
# compile_area treats every cross-region dock as a FREE entry node, so an
# area-local pickup rule answers "what's needed to reach this pickup from the
# nearest area boundary". That deliberately omits the cost of reaching the
# boundary — fine for per-pickup gating, but it makes the area-local
# cross-region exits all Trivial, so they cannot gate region access.
#
# To gate region entry faithfully we run ONE reachability pass over the whole
# 9-area graph (region-qualified node keys) from the single global start.
# region_access[R] is the easiest global requirement to set foot in region R
# (the OR of the reach rules of R's inbound cross-region docks). The start
# region is Trivial. The AP side gates Menu -> R on this, leaving the
# per-pickup rules untouched. Itorash has no AP region (its events fold into
# Hanubia, carrying their own area-local reach rules), so it is not emitted.

GlobalKey = tuple                  # (region, sub_area, node)



# ---------------------------------------------------------------------------
# Native-graph emit (logic_graph.json)
#
# Emit the SYMBOLIC graph; AP's own region sweep solves reachability per seed
# (O(edges), ~0.1s). Nodes collapse to one Region per trivial-SCC; each
# cross-region edge becomes an Entrance whose access rule is the symbolic edge
# AST. Rando-eligible door docks emit a {"type":"dock","side_id":...} atom the
# apworld resolves per-seed against the door assignment. Events are pure-logic
# regions (no locations); the victory condition references them via event atoms
# compiled in graph_mode (can_reach_region). See [[dread-native-graph-spike]].
# ---------------------------------------------------------------------------

# v2: transports pulled into a shuffle pool (transport rando).
GRAPH_SCHEMA_VERSION = 3

# Dock types whose weakness can be randomized (door-lock rando). Transports
# (elevator/teleporter/shuttle) are handled by transport rando (separate); tunnel
# / other are structural. v1: doors only.
RANDOMIZABLE_DOCK_TYPES = {"door"}

# Transport dock types whose DESTINATION can be shuffled (transport rando). Their
# open requirement is trivial — rando changes which region the ride reaches, not
# a weapon gate. Teleporters are excluded: open-dread-rando's elevator patcher
# has a "TODO implement teleporter rando" (only TRANSPORT-type elevators/shuttles
# get the full minimap/icon treatment), so we keep teleporters vanilla for now.
TRANSPORT_DOCK_TYPES = {"elevator", "shuttle"}


def _trivial_scc(nodes: dict, conn_edges: list) -> dict:
    """Collapse nodes into regions = strongly-connected components of the
    TRIVIAL connection-edge digraph (free mutual reachability). Dock edges never
    participate (they stay region boundaries). Returns {node_key: region_id}."""
    adj = {k: [] for k in nodes}
    radj = {k: [] for k in nodes}
    for u, v, ast in conn_edges:
        if v in nodes and ast.get("type") == "trivial":
            adj[u].append(v)
            radj[v].append(u)
    visited: set = set()
    order: list = []
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
    comp: dict = {}
    lab = 0
    seen: set = set()
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
    return comp


def emit_graph(
    areas: dict[str, dict],
    header: Header,
    ap_loc_by_actor: dict,
    ap_loc_by_pickup_index: dict,
    victory_item_only: dict,
) -> dict:
    """Build the native logic graph artifact (see module header)."""
    # Per-(dock_type,weakness) open-requirement table the apworld resolves dock
    # atoms against. Also map each weakness to its patcher door_type (extra.type)
    # for door_patches emission.
    weakness_requirements: dict[str, dict] = {}
    weakness_door_type: dict[str, str] = {}
    for (dock_type, wname), w in header.dock_weakness.items():
        weakness_requirements[f"{dock_type}::{wname}"] = dock_open_requirement(
            header, dock_type, wname)
        dt = (w.get("extra") or {}).get("type")
        if dock_type == "door" and dt:
            weakness_door_type[wname] = dt

    # Door-lock rando config (which door weaknesses may change, and into what).
    door_dr = header.dock_rando.get("door", {})
    door_change_from = set(door_dr.get("change_from", []))

    scenario_of = {region: ad["extra"].get("scenario_id")
                   for region, ad in areas.items()}

    nodes: dict = {}
    conn_edges: list = []           # (u, v, ast)
    for region, ad in areas.items():
        for sub_name, sub in ad["areas"].items():
            for n_name, n in sub["nodes"].items():
                key = (region, sub_name, n_name)
                nodes[key] = n
                for tgt, req in n.get("connections", {}).items():
                    conn_edges.append((key, (region, sub_name, tgt),
                                       translate_requirement(req, header)))

    # Dock edges + per-side metadata. Rando-eligible doors emit a symbolic atom;
    # transports (elevator/shuttle) are pulled out into a shuffle pool (their
    # edge is added by the apworld per the per-seed matching); everything else
    # bakes its vanilla open requirement.
    dock_edges: list = []           # (u, v, ast)
    dock_sides: dict = {}
    transport_raw: dict = {}        # side_id -> endpoint meta (comp filled below)
    for key, n in nodes.items():
        if n.get("node_type") != "dock":
            continue
        dc = n.get("default_connection")
        if not dc:
            continue
        region, sub_name, n_name = key
        dock_type = n.get("dock_type")
        weakness = n.get("default_dock_weakness")
        far = (dc["region"], dc["area"], dc["node"])
        side_id = "::".join(key)
        override = n.get("override_default_open_requirement")

        # Transport endpoint: collect for the shuffle pool, don't bake an edge.
        if dock_type in TRANSPORT_DOCK_TYPES:
            ex = n.get("extra", {})
            # ``start_point_actor_name`` is the spawn actor PHYSICALLY in THIS
            # endpoint's room — where Samus lands when a ride arrives here. That
            # is exactly what the patcher must point a shuffled ride at (mirrors
            # Randovania's ``_start_point_ref_for`` reading the DESTINATION node's
            # ``start_point_actor_name``). Do NOT use ``target_spawn_point``: it
            # is this endpoint's VANILLA-destination landing platform (it lives in
            # a different scenario), so feeding it as a shuffled destination spawn
            # makes the engine load the right scenario but fail to find the spawn
            # actor → in-game crash on the ride.
            transport_raw[side_id] = {
                "key": key,
                "far": "::".join(far),
                "type": dock_type,
                "scenario": scenario_of.get(region),
                "actor": ex.get("actor_name"),
                "start_point": ex.get("start_point_actor_name"),
                "transporter_name": ex.get("transporter_name", ""),
            }
            continue
        # Eligible per Randovania's door dock_rando: the door's CURRENT weakness
        # must be in change_from (only those get reassigned), not per-node
        # excluded, and not carrying a bespoke open override.
        rando_eligible = (
            dock_type in RANDOMIZABLE_DOCK_TYPES
            and weakness in door_change_from
            and not n.get("exclude_from_dock_rando")
            and override is None
            and bool(n.get("extra", {}).get("actor_name"))
        )
        if rando_eligible:
            ast = {"type": "dock", "side_id": side_id}
            dock_sides[side_id] = {
                "dock_type": dock_type,
                "default_weakness": weakness,
                "paired_side_id": "::".join(far),
                "incompatible_weaknesses": n.get("incompatible_dock_weaknesses", []),
                "patcher": {"scenario": scenario_of.get(region),
                            "actor": n.get("extra", {}).get("actor_name")},
            }
        elif override is not None:
            ast = translate_requirement(override, header)
        else:
            ast = dock_open_requirement(header, dock_type, weakness)
        dock_edges.append((key, far, ast))

    comp = _trivial_scc(nodes, conn_edges)
    n_regions = (max(comp.values()) + 1) if comp else 0

    # Cross-region entrances (intra-component edges are free → dropped).
    entrances: list = []
    for u, v, ast in conn_edges + dock_edges:
        if v in nodes and comp[u] != comp[v]:
            entrances.append([comp[u], comp[v], ast])

    # Transport endpoints (elevator/shuttle), with comps resolved. The apworld
    # adds the ride edges from the per-seed matching (default = vanilla pairing).
    transports: dict = {}
    for side_id, meta in transport_raw.items():
        transports[side_id] = {
            "comp": comp[meta["key"]],
            "type": meta["type"],
            "default_dest": meta["far"],
            "scenario": meta["scenario"],
            "actor": meta["actor"],
            "start_point": meta["start_point"],
            "transporter_name": meta["transporter_name"],
        }

    pickups: list = []              # [comp, ap_location_name]
    events: list = []               # [comp, event_name]
    start_comps: dict = {}          # "region::area::node" -> comp (valid starts)
    for key, n in nodes.items():
        nt = n.get("node_type")
        if nt == "pickup":
            actor = n.get("extra", {}).get("actor_name")
            name = (ap_loc_by_actor.get((key[0], actor))
                    or ap_loc_by_pickup_index.get(n.get("pickup_index")))
            if name:
                pickups.append([comp[key], name])
        elif nt == "event":
            events.append([comp[key], n["event_name"]])
        if n.get("valid_starting_location"):
            region = key[0]
            start_comps["::".join(key)] = {
                "comp": comp[key],
                "patcher": {"scenario": scenario_of.get(region),
                            "actor": n.get("extra", {}).get("actor_name")},
            }

    start = header.starting_location
    start_key = (start["region"], start["area"], start["node"])

    # Vanilla shield baseline per scenario. open-dread-rando renames every
    # existing shielded door's shields into the SAME 100-id pool the rando
    # shields draw from (door_locks/door_patcher.py), so DoorRando must seed its
    # per-scenario shield budget with this baseline or a roll can exhaust the
    # pool. Count unique door actors whose vanilla weakness needs a shield.
    # (SHIELDED set mirrors DoorRando.SHIELDED_DOOR_TYPES — kept inline to avoid
    # an apworld import from this build script.)
    _SHIELDED = {"wide_beam", "plasma_beam", "wave_beam", "missile",
                 "super_missile", "ice_missile", "diffusion_beam",
                 "storm_missile", "bomb", "cross_bomb", "power_bomb", "closed"}
    vanilla_shield_ids: dict[str, int] = {}
    _seen_shield_actor: set = set()
    for key, n in nodes.items():
        if n.get("node_type") != "dock" or n.get("dock_type") != "door":
            continue
        if weakness_door_type.get(n.get("default_dock_weakness")) not in _SHIELDED:
            continue
        actor = (n.get("extra") or {}).get("actor_name")
        scn = scenario_of.get(key[0])
        if not actor or not scn or (scn, actor) in _seen_shield_actor:
            continue
        _seen_shield_actor.add((scn, actor))
        vanilla_shield_ids[scn] = vanilla_shield_ids.get(scn, 0) + 1

    return {
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "n_regions": n_regions,
        "start_comp": comp[start_key],
        "start_comps": start_comps,
        "entrances": entrances,
        "dock_sides": dock_sides,
        "transports": transports,
        "weakness_requirements": weakness_requirements,
        "door_rando": {
            "change_from": sorted(door_change_from),
            "change_to": door_dr.get("change_to", []),
            "weakness_door_type": weakness_door_type,
            "locked_weakness": door_dr.get("locked"),
            "unlocked_weakness": door_dr.get("unlocked"),
            "vanilla_shield_ids": vanilla_shield_ids,
        },
        "pickups": pickups,
        "events": events,
        "victory_condition": victory_item_only,
    }


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
        help="compile every area",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat any unmapped actor or compile warning as a hard error",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DATA_DIR / "logic_graph.json",
        help="output path for logic_graph.json",
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
    ap_locations = json.loads((DATA_DIR / "locations.json").read_text())

    # AP location maps: actor pickups by (region, actor); non-actor
    # (boss/EMMI/cutscene/corex) by pickup_index.
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

    all_area_data: dict[str, dict] = {}
    for area_name in ALL_AREAS:
        p = CACHE_DIR / f"{area_name}.json"
        if not p.exists():
            print(f"missing cache file: {p}", file=sys.stderr)
            return 2
        all_area_data[area_name] = json.loads(p.read_text())

    # Victory condition: translate the raw RDV goal into our AST with event atoms
    # intact. The graph model handles them natively via can_reach_region("Event:X").
    victory_ast = translate_requirement(header_data["victory_condition"], header)

    graph = emit_graph(all_area_data, header,
                       ap_loc_by_actor, ap_loc_by_pickup_index, victory_ast)
    graph["pinned_commit"] = pinned
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(graph, indent=2))
    print(f"wrote {args.out} — {graph['n_regions']} regions, "
          f"{len(graph['entrances'])} entrances, {len(graph['pickups'])} "
          f"pickups, {len(graph['events'])} event nodes, "
          f"{len(graph['dock_sides'])} rando-eligible doors")

    return 0


if __name__ == "__main__":
    sys.exit(main())
