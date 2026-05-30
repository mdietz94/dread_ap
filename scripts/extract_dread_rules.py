"""Compile Randovania Dread logic JSON → AP-shaped compiled_rules.json.

Reads ``.dread-cache/randovania-logic/{header,Elun,...}.json`` and emits
two artifacts the apworld consumes:

  apworld/dread/data/compiled_rules.json   per-location rule AST
  apworld/dread/data/events.json           event-item pool entries

The serialized AST is intentionally simple — no Python objects, no
lambdas — so the apworld can re-compile against the live ``player``
index at world-creation time without importing this script.

AST shape:

    {"type": "and",  "items": [...]}     all children must hold
    {"type": "or",   "items": [...]}     at least one child must hold
    {"type": "item", "name": "...", "amount": 1}
    {"type": "event", "name": "..."}
    {"type": "trick", "name": "...", "level": 1}   v0.1: always False
    {"type": "sum", "terms": [{"name", "per_unit"}, ...],
                    "base": int, "threshold": int}
                                         base + Σ count(name) * per_unit ≥ threshold
    {"type": "damage_threshold",
        "suit_options": [...], "hp_needed": int}
                                         any suit OR 99+100·ETank+25·EPart ≥ hp
    {"type": "trivial"}                  always True
    {"type": "impossible"}               always False

The schema version is written to compiled artifacts as ``schema_version``;
the loader (apworld/dread/Rules.py::load_compiled_rules) refuses mismatched
versions so a stale artifact never silently passes.
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

# Bumped whenever the AST vocabulary changes. The loader refuses anything
# that doesn't match — we never want a stale artifact silently passing.
# v1: pre-v0.3 (no sum / damage_threshold)
# v2: sum + damage_threshold nodes (v0.3, ammo + HP-budget gating)
SCHEMA_VERSION = 2

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


# Items where Randovania's "amount" is an unlock-flag, not a count: any one
# pickup satisfies the requirement. (Supers is gated by any Missile+ Tank,
# MissileLauncher/MainPB by any tank/PB.) These stay collapsed to amount=1.
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
    # Highest trick tier the solver may assume (1=Beginner, 2=Intermediate,
    # 3=Advanced). A TrickReq of level N translates to Trivial when
    # N <= trick_level, else Impossible. Default 1 keeps existing callers
    # (and the M1/Gate-A behavior) unchanged.
    trick_level: int = 1

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
            # A TrickReq of level N (amount) is assumed satisfiable when the
            # configured trick_level is at least N — i.e. the seed ASSUMES the
            # player can perform every trick up through `header.trick_level`.
            # Level 1 ("Beginner") covers basic shinesparks / bomb jumps from
            # clear ledges; higher tiers unlock progressively harder tech.
            # Baked per level into compiled_rules_l{1,2,3}.json.
            if amount <= header.trick_level:
                return TRIVIAL
            return IMPOSSIBLE

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
        # Cold rooms in Ferenia — gravity / varia both fine in vanilla.
        return {"type": "damage_threshold",
                "suit_options": ["Gravity Suit", "Varia Suit"],
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
    """Stable total order for the disjunct cap. Primary key = length, so
    truncation keeps the shortest (easiest) paths and drops the longest;
    the secondary key (sorted atoms) replaces frozenset iteration order,
    which is hash-seed-dependent, so repeated bakes are byte-reproducible."""
    return (len(disjunct), tuple(sorted(disjunct)))


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


def build_global_graph(
    areas: dict[str, dict],
    header: Header,
) -> tuple[dict, dict]:
    """Merge every area into one graph with region-qualified node keys and
    real (not sentinel) cross-region dock edges."""
    nodes: dict = {}
    edges: dict = {}

    for region, area_data in areas.items():
        for sub_name, sub in area_data["areas"].items():
            for n_name, n in sub["nodes"].items():
                key = (region, sub_name, n_name)
                nodes[key] = n
                for tgt_name, req in n.get("connections", {}).items():
                    edges.setdefault(key, []).append(
                        ((region, sub_name, tgt_name),
                         translate_requirement(req, header))
                    )

    # Dock edges connect to the real target node, possibly in another region.
    for key, n in list(nodes.items()):
        if n.get("node_type") != "dock":
            continue
        dc = n.get("default_connection")
        if not dc:
            continue
        dock_req = dock_open_requirement(
            header, n.get("dock_type"), n.get("default_dock_weakness")
        )
        override_open = n.get("override_default_open_requirement")
        if override_open is not None:
            dock_req = translate_requirement(override_open, header)
        edges.setdefault(key, []).append(
            ((dc["region"], dc["area"], dc["node"]), dock_req)
        )

    return edges, nodes


def _strip_events(ast: dict) -> dict:
    """Return the AST with all event requirements treated as satisfied.

    region_access gates Menu→region on the AP side, so it MUST stay
    bootstrappable from items alone: events are themselves locked progression
    whose area-relative reach rules assume free area entry, and coupling region
    entry to them deadlocks the goal (Ship becomes unreachable). Dropping event
    atoms keeps the real ITEM gating (e.g. Cataris needs Charge + Cross Bomb)
    while staying over-permissive about event-gated traversal — the safe
    direction (never makes a region falsely unreachable)."""
    t = ast.get("type")
    if t == "event":
        return TRIVIAL
    if t == "and":
        return mk_and([_strip_events(c) for c in ast["items"]])
    if t == "or":
        return mk_or([_strip_events(c) for c in ast["items"]])
    return ast


def _strip_self_event(ast: dict, name: str) -> dict:
    """Remove self-references from an event's own reach rule. A disjunct that
    requires event E in order to reach E is circular (un-bootstrappable in a
    monotonic solver), so replace the self-reference with Impossible and
    simplify. If every path self-references, the event is only reachable
    post-trigger → Impossible (a back-path the area BFS picked up)."""
    t = ast.get("type")
    if t == "event" and ast.get("name") == name:
        return IMPOSSIBLE
    if t == "and":
        return mk_and([_strip_self_event(c, name) for c in ast["items"]])
    if t == "or":
        return mk_or([_strip_self_event(c, name) for c in ast["items"]])
    return ast


def compute_region_access(
    areas: dict[str, dict],
    header: Header,
    regions: list[str],
) -> dict[str, dict]:
    """Global reachability → per-region entry rule AST (item-only), for the
    given AP region names."""
    edges, nodes = build_global_graph(areas, header)
    start = header.starting_location
    entry = (start["region"], start["area"], start["node"])
    if entry not in nodes:
        print(f"  WARN: global start {entry!r} not in node set", file=sys.stderr)

    # Inbound cross-region docks: a dock node in region R whose default
    # connection leaves R. Reaching one == being able to enter R.
    inbound_by_region: dict[str, list] = {}
    targets: set = set()
    for key, n in nodes.items():
        if n.get("node_type") != "dock":
            continue
        region = key[0]
        dc = n.get("default_connection") or {}
        if dc.get("region") and dc["region"] != region:
            targets.add(key)
            inbound_by_region.setdefault(region, []).append(key)

    reach = enumerate_paths([entry], edges, targets)

    region_access: dict[str, dict] = {}
    for R in regions:
        if R == start["region"]:
            region_access[R] = TRIVIAL
            continue
        rules = [reach.get(k, IMPOSSIBLE) for k in inbound_by_region.get(R, [])]
        region_access[R] = _strip_events(mk_or(rules)) if rules else IMPOSSIBLE
    return region_access


def _substitute_events(ast: dict, event_cost: dict) -> dict:
    """Inline each event's ITEM-ONLY reach cost in place of its atom. Events not
    yet collected (absent from event_cost) → Impossible (their edge is blocked
    this round). The result is item-only — no event atoms remain — which is what
    lets AP's monotonic item sweep bootstrap everything (the event-as-locked-
    item model created item↔event cycles AP couldn't unwind)."""
    t = ast.get("type")
    if t == "event":
        return event_cost.get(ast["name"], IMPOSSIBLE)
    if t == "and":
        return mk_and([_substitute_events(c, event_cost) for c in ast["items"]])
    if t == "or":
        return mk_or([_substitute_events(c, event_cost) for c in ast["items"]])
    return ast


def _location_easiest_hp(ast: dict) -> int | float:
    """For a compiled-rule AST, return the HP needed via the EASIEST
    surviving disjunct — min over OR-paths of (max no-suit
    damage_threshold ``hp_needed`` along that path).

    Returns 0 if a disjunct has no no-suit damage gates (player needs no
    HP for that path); returns ``inf`` if the rule is IMPOSSIBLE. Suit-typed
    ``damage_threshold`` nodes are treated as 0 (they short-circuit on a
    suit, which a normal player has by the time they enter that region).
    """
    t = ast.get("type")
    if t == "or":
        if not ast["items"]:
            return float("inf")
        return min(_location_easiest_hp(c) for c in ast["items"])
    if t == "and":
        worst = 0
        for c in ast["items"]:
            child = _location_easiest_hp(c)
            if child == float("inf"):
                return float("inf")
            if child > worst:
                worst = child
        return worst
    if t == "damage_threshold" and not ast.get("suit_options"):
        return int(ast["hp_needed"])
    if t == "impossible":
        return float("inf")
    return 0


def compute_region_etank_floors(
    rules_by_loc: dict[str, dict],
    ap_region_names: list[str],
) -> dict[str, int]:
    """Per region, the E-Tank count required to enter — derived from the
    75th-percentile location HP across the region.

    Each location's easiest_hp = "cheapest reach disjunct's worst hit."
    For easy locations (the missile tank near the region entry), the rule
    has a damage-free disjunct gated only on items → easiest_hp=0. For
    bottleneck locations (Golzuna, Kraid, Hanubia bosses), every disjunct
    includes a no-suit hit → easiest_hp ≥ 349.

    Using P75 instead of MAX keeps single-outlier regions ungated: Cataris
    has 24 locations at hp=0 and 1 (Kraid) at hp=349 → P75=0 → no gate.
    Hanubia has all 4 locations at hp=349 → P75=349 → 3 tanks. This
    matches "if most-but-not-all locations in R need HP, gate the region;
    if it's one outlier, leave fill to handle it via item progression."

    Floor = ceil((p75_hp - 99) / 100) since 99 base HP + 100/tank.

    Per-region gating instead of per-location: Randovania's solver
    pre-orders pickups to accumulate HP; AP's general-purpose fill doesn't.
    A per-location HP gate generates hundreds of overlapping constraints
    and deadlocks. One gate per region (≤8) is satisfiable.
    """
    per_region: dict[str, list[int]] = {r: [] for r in ap_region_names}
    for loc_name, rule in rules_by_loc.items():
        region = loc_name.split(":", 1)[0]
        if region not in per_region:
            continue
        hp = _location_easiest_hp(rule)
        if hp == float("inf"):
            continue
        per_region[region].append(int(hp))

    floors: dict[str, int] = {}
    for r, hps in per_region.items():
        if not hps:
            floors[r] = 0
            continue
        hps.sort()
        # 75th percentile (nearest-rank). For small n, this is close to max.
        p75 = hps[(3 * len(hps)) // 4] if len(hps) >= 4 else hps[-1]
        if p75 <= 99:
            floors[r] = 0
        else:
            floors[r] = (p75 - 100) // 100 + 1
    return floors


def _strip_no_suit_damage_thresholds(ast: dict) -> dict:
    """Replace every ``damage_threshold`` with empty ``suit_options`` with
    TRIVIAL, simplifying the surrounding AND/OR structure. Suit-typed
    nodes are preserved (they short-circuit on Varia/Gravity and don't
    over-constrain fill).

    Called AFTER ``compute_region_etank_floors`` extracts the amounts —
    once region_access carries the per-region floor, the per-location
    no-suit HP gates are redundant *and* fill-fatal."""
    t = ast.get("type")
    if t == "damage_threshold" and not ast.get("suit_options"):
        return TRIVIAL
    if t == "and":
        return mk_and([_strip_no_suit_damage_thresholds(c) for c in ast["items"]])
    if t == "or":
        return mk_or([_strip_no_suit_damage_thresholds(c) for c in ast["items"]])
    return ast


def compile_forward(
    areas: dict[str, dict],
    header: Header,
    ap_loc_by_actor: dict,
    ap_loc_by_pickup_index: dict,
    *,
    max_rounds: int = 40,
    cap: int = 32,
) -> tuple[dict, dict, dict]:
    """Randovania-style forward resolver over the global graph, emitting
    ITEM-ONLY rules.

    Collects events in dependency SPHERES: each round, every event atom in an
    edge is replaced by the ITEM-ONLY reach cost of events collected in EARLIER
    rounds (uncollected events block that edge), reachability is recomputed, and
    newly-reachable events record their own item-only cost for the next round.

    Inlining is the crux: the old event-as-locked-item model created item↔event
    bootstrap cycles (an event needs an item whose location needs that event)
    that AP's monotonic, precollected-only sweep could not unwind, so
    accessibility=items/full failed. Folding each event's cost into pure item
    requirements removes events from the dependency graph entirely, so the rules
    bootstrap like ordinary AP item logic. Returns
    (rules_by_loc, event_rule_by_name, event_region_by_name); all rules are
    item-only and global (region_access becomes a star, boss/EMMI gated
    directly). Requires translate_requirement's negated temporal → Trivial."""
    edges, nodes = build_global_graph(areas, header)
    start = header.starting_location
    entry = (start["region"], start["area"], start["node"])
    ev_name_by_node = {k: n.get("event_name") for k, n in nodes.items()
                       if n.get("node_type") == "event"}
    ev_nodes_by_name: dict[str, list] = {}
    for k, nm in ev_name_by_node.items():
        ev_nodes_by_name.setdefault(nm, []).append(k)

    # event_cost[E] = item-only reach DNF, captured the round E is collected
    # (so it references only earlier events' costs → already inlined → acyclic).
    event_cost: dict[str, dict] = {}
    node_reach: dict = {}
    for _ in range(max_rounds):
        round_edges = {
            u: [(v, _substitute_events(req, event_cost)) for v, req in adj]
            for u, adj in edges.items()
        }
        node_reach = enumerate_paths(
            [entry], round_edges, set(nodes.keys()),
            max_disjuncts_per_node=cap, max_disjuncts_per_edge=cap,
        )
        newly = {nm for k, nm in ev_name_by_node.items()
                 if nm not in event_cost
                 and node_reach.get(k, IMPOSSIBLE).get("type") != "impossible"}
        if not newly:
            break
        for nm in newly:
            event_cost[nm] = mk_or([node_reach.get(k, IMPOSSIBLE)
                                    for k in ev_nodes_by_name[nm]])
    event_rule_by_name = event_cost

    # Map pickup nodes (actor + boss/EMMI/cutscene) to AP locations.
    rules_by_loc: dict[str, dict] = {}
    for key, n in nodes.items():
        if n.get("node_type") != "pickup":
            continue
        region = key[0]
        ast = node_reach.get(key, IMPOSSIBLE)
        actor_name = (n.get("extra") or {}).get("actor_name")
        loc_name = None
        if actor_name and (region, actor_name) in ap_loc_by_actor:
            loc_name = ap_loc_by_actor[(region, actor_name)]
        elif n.get("pickup_index") is not None and n["pickup_index"] in ap_loc_by_pickup_index:
            loc_name = ap_loc_by_pickup_index[n["pickup_index"]]
        if loc_name is not None:
            rules_by_loc[loc_name] = ast

    # event_rule_by_name was captured per-round above (acyclic). Region tag =
    # first contributing area (deterministic).
    event_region_by_name: dict[str, str] = {
        nm: sorted(k[0] for k in ks)[0] for nm, ks in ev_nodes_by_name.items()
    }
    return rules_by_loc, event_rule_by_name, event_region_by_name


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
    parser.add_argument(
        "--trick-level",
        type=int,
        default=1,
        choices=(1, 2, 3),
        help="highest trick tier the solver may assume "
             "(1=Beginner, 2=Intermediate, 3=Advanced). Bake one file per level.",
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
    header.trick_level = args.trick_level

    ap_loc_by_key, ap_region_scenario = load_ap_locations()
    ap_items = json.loads((DATA_DIR / "items.json").read_text())
    ap_locations = json.loads((DATA_DIR / "locations.json").read_text())
    # The event ID base must stay anchored to the BASE-item max so re-running
    # the compiler never renumbers events out from under existing seeds. Exclude
    # both the event items this script previously appended (else each re-run
    # shifts the base by len(events)) and the "Metroid DNA" goal item appended
    # after events. The result is stable across repeated bakes.
    existing_item_max = max(
        it["ap_id"] for it in ap_items
        if not it["name"].startswith("Event: ")
        and not it["name"].startswith("Metroid DNA")
    )
    existing_loc_max = max(l["ap_id"] for l in ap_locations)

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

    print("running forward resolver (item-only inlining) over all areas ...")
    out_rules, event_rule_by_name, event_region_by_name = compile_forward(
        all_area_data, header, ap_loc_by_actor, ap_loc_by_pickup_index)

    # Victory: inline the goal event's item-only cost (no event atoms remain).
    victory_ast = _substitute_events(
        translate_requirement(header_data["victory_condition"], header),
        event_rule_by_name,
    )

    # ---- Build the events list ------------------------------------------------
    # event_region_by_name covers every event NODE (stable across bakes), so the
    # sorted order — and thus the append-only IDs — match items.json. Item-only
    # event rules (IMPOSSIBLE if an event was never reached) gate the (now
    # vestigial) event locations; nothing in the pickup rules references them.
    event_names = sorted(event_region_by_name.keys())
    event_item_base = existing_item_max + 1
    event_loc_base = existing_loc_max + 1

    events_out: list[dict] = []
    for i, ename in enumerate(event_names):
        events_out.append({
            "name": ename,
            "region": event_region_by_name.get(ename, ""),
            "rule": event_rule_by_name.get(ename, IMPOSSIBLE),
            "item_ap_id": event_item_base + i,
            "location_ap_id": event_loc_base + i,
        })

    # ---- Per-region E-Tank floor + no-suit damage_threshold strip -------------
    # While compile_forward was running, generic Damage emitted
    # damage_threshold(no_suit) carrying the original HP amount. Now:
    #   1. Derive per-region E-Tank floors from those amounts (the "easiest
    #      surviving path to any pickup in the region" metric).
    #   2. Strip the no-suit nodes from per-location rules (TRIVIAL them) so
    #      AP's fill isn't deadlocked by per-location HP gates. Per-region
    #      gating via region_access is the substitute — coarse but
    #      fill-solvable, and matches the resource-accumulation pattern
    #      Randovania's solver bakes in by ordering its own placements.
    ap_region_names = [e["name"] for e in json.loads((DATA_DIR / "regions.json").read_text())]
    region_etank_floors = compute_region_etank_floors(out_rules, ap_region_names)

    out_rules = {ln: _strip_no_suit_damage_thresholds(r) for ln, r in out_rules.items()}
    victory_ast = _strip_no_suit_damage_thresholds(victory_ast)
    for ev in events_out:
        ev["rule"] = _strip_no_suit_damage_thresholds(ev["rule"])

    region_access: dict[str, dict] = {}
    for r in ap_region_names:
        floor = region_etank_floors.get(r, 0)
        if floor > 0:
            region_access[r] = {"type": "item", "name": "Energy Tank",
                                "amount": floor}
        else:
            region_access[r] = dict(TRIVIAL)
    print("region E-Tank floors:", {r: f for r, f in region_etank_floors.items() if f})

    output = {
        "schema_version": SCHEMA_VERSION,
        "pinned_commit": pinned,
        "areas_compiled": sorted(all_area_data.keys()),
        "trick_level": args.trick_level,
        "victory_condition": victory_ast,
        "region_access": region_access,
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
