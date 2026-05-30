"""Access rules — loaded from compiled_rules.json.

The rule AST is produced by ``scripts/extract_dread_rules.py`` from
Randovania's authoritative Dread logic. Rules.py just compiles the AST
to ``state.has(...)`` lambdas and applies them via ``add_rule``.

Milestone 2 plumbing (this file):
  * All 9 areas compiled; 137 actor pickup rules + ~184 event reach
    rules consumed end-to-end.
  * Events are real AP items locked to synthetic event locations; the
    ``event`` branch of compile_to_lambda calls
    ``state.has("Event: <name>", player)``.
  * ``completion_condition`` reads ``victory_condition`` from the
    compiled artifact (currently ``state.has("Event: Ship", player)``).

Gate B shipped (see ``docs/randovania-logic-port-notes.md``):
  * Trick level is a user option. ``load_compiled_rules(trick_level)``
    picks one of three pre-baked artifacts; each collapses tricks at or
    below the chosen tier to Trivial and higher tiers to False.
  * Cross-region access is modeled. ``compiled_rules.json`` carries a
    ``region_access`` map (global reach rule per region); Regions.py
    gates Menu→region on it, composing with the per-pickup reach rules.

v0.3: ammo + HP-budget damage gating shipped. Two new AST node types:
  * ``sum`` — ``base + Σ state.count(name, p) · per_unit ≥ threshold``.
    Used for raw Randovania missile / power-bomb counts (e.g. a shielded
    door needing 75 missiles).
  * ``damage_threshold`` — ``any suit_option holds OR
    99 + 100·count(Energy Tank) + 25·count(Energy Part) ≥ hp_needed``.
    Replaces the old all-or-nothing suit-OR damage collapse.

The compiler tags artifacts with ``schema_version``; ``load_compiled_rules``
refuses anything that doesn't match the expected version so a stale bake
can't silently pass.
"""
from __future__ import annotations

from typing import Any, Callable

from ._data_loader import load_json

# A Predicate is a function (state) -> bool, with `player` already
# closed over. Kept duck-typed (no CollectionState import) so the unit
# tests can exercise compile_to_lambda without an Archipelago install.
Predicate = Callable[[Any], bool]


def _const_true(_: Any) -> bool:
    return True


def _const_false(_: Any) -> bool:
    return False


def compile_to_lambda(ast: dict, player: int) -> Predicate:
    """Translate a compiled rule AST into a Predicate.

    Closure-capture care: every list-comprehension binds locals (`name`,
    `amount`, etc.) eagerly so the resulting lambda isn't bitten by
    Python's late-binding rule.
    """
    t = ast["type"]

    if t == "trivial":
        return _const_true
    if t == "impossible":
        return _const_false

    if t == "item":
        name = ast["name"]
        amount = int(ast.get("amount", 1))
        if amount <= 1:
            return lambda state, n=name: state.has(n, player)
        return lambda state, n=name, a=amount: state.has(n, player, a)

    if t == "event":
        # M2: each event is an AP item locked to its event location.
        # The event item's name is "Event: <name>" — see Items.py /
        # locations.json synthetic event entries.
        name = ast["name"]
        return lambda state, n=f"Event: {name}": state.has(n, player)

    if t == "trick":
        level = int(ast.get("level", 1))
        # The compiler should have already collapsed these, but defend
        # in case a hand-edited rule slips through.
        return _const_true if level <= 1 else _const_false

    if t == "sum":
        terms = tuple((tr["name"], int(tr["per_unit"])) for tr in ast["terms"])
        base = int(ast["base"])
        thr = int(ast["threshold"])
        def _sum_pred(state, _p=player, _terms=terms, _base=base, _thr=thr):
            total = _base
            for name, per in _terms:
                total += state.count(name, _p) * per
                if total >= _thr:
                    return True
            return total >= _thr
        return _sum_pred

    if t == "damage_threshold":
        suits = tuple(ast.get("suit_options", []))
        hp = int(ast["hp_needed"])
        def _dthr_pred(state, _p=player, _suits=suits, _hp=hp):
            for s in _suits:
                if state.has(s, _p):
                    return True
            budget = 99 + 100 * state.count("Energy Tank", _p) \
                        + 25 * state.count("Energy Part", _p)
            return budget >= _hp
        return _dthr_pred

    if t == "and":
        children = [compile_to_lambda(c, player) for c in ast["items"]]
        if not children:
            return _const_true
        if len(children) == 1:
            return children[0]
        return lambda state, cs=children: all(c(state) for c in cs)

    if t == "or":
        children = [compile_to_lambda(c, player) for c in ast["items"]]
        if not children:
            return _const_false
        if len(children) == 1:
            return children[0]
        return lambda state, cs=children: any(c(state) for c in cs)

    raise ValueError(f"unknown rule AST type: {t!r}")


# Trick-level → pre-baked rule file. Beginner is the canonical
# compiled_rules.json (so the default reproduces historical behavior); the
# higher tiers are baked siblings. All three are produced by
# scripts/extract_dread_rules.py --trick-level {1,2,3}.
_TRICK_LEVEL_FILE = {
    1: "compiled_rules.json",
    2: "compiled_rules_l2.json",
    3: "compiled_rules_l3.json",
}

# Must match scripts/extract_dread_rules.py::SCHEMA_VERSION. A mismatch means
# the on-disk artifact predates a vocabulary change (e.g. v1 had `damage`
# nodes, v2 has `sum` + `damage_threshold`) and would silently route through
# stale collapses. Fail closed and prompt for a regen.
EXPECTED_SCHEMA_VERSION = 2


def load_compiled_rules(trick_level: int = 1) -> dict[str, Any]:
    """Load the pre-baked rule set for the given trick level.

    Unknown level → canonical L1. A missing ``_lN`` file falls back to the
    canonical ``compiled_rules.json`` so a dev box that only baked L1 still
    works; a missing canonical file raises FileNotFoundError, preserving the
    "everything reachable" fallback that set_rules / create_regions honor.

    Raises ``RuntimeError`` if the artifact's ``schema_version`` does not
    match ``EXPECTED_SCHEMA_VERSION`` — regenerate with
    ``python scripts/extract_dread_rules.py --trick-level {1,2,3}``."""
    name = _TRICK_LEVEL_FILE.get(int(trick_level), "compiled_rules.json")
    try:
        compiled = load_json(name)
    except FileNotFoundError:
        if name != "compiled_rules.json":
            return load_compiled_rules(1)
        raise
    version = compiled.get("schema_version")
    if version != EXPECTED_SCHEMA_VERSION:
        raise RuntimeError(
            f"compiled_rules.json schema_version={version!r} but loader "
            f"expects {EXPECTED_SCHEMA_VERSION}. Regenerate with "
            f"`python scripts/extract_dread_rules.py --trick-level "
            f"{{1,2,3}}`."
        )
    return compiled


def set_rules(world) -> None:
    """Apply add_rule for every compiled location rule.

    Locations not present in compiled_rules.json (boss / EMMI / cutscene
    pickups) get no per-pickup rule, but they are NOT trivially reachable:
    Regions.py gates each region's Menu edge on its global region_access
    rule, so a boss is reachable only once its region is. Also locks the
    event items and the Metroid DNA goal items to their locations.
    """
    from worlds.generic.Rules import add_rule  # local import for test isolation

    multiworld = world.multiworld
    player = world.player

    try:
        compiled = load_compiled_rules(int(world.options.trick_level.value))
    except FileNotFoundError:
        # No compiled rules — preserve "everything reachable" behavior
        # so the apworld still loads in pre-compile dev environments.
        compiled = {
            "rules": {},
            "events": [],
            "victory_condition": {"type": "trivial"},
        }

    # 1. Per-pickup reach rules.
    for loc_name, rule_ast in compiled.get("rules", {}).items():
        try:
            location = multiworld.get_location(loc_name, player)
        except KeyError:
            # Compiled rule for a location not in our data table —
            # surface but don't crash so we can iterate.
            continue
        predicate = compile_to_lambda(rule_ast, player)
        add_rule(location, predicate)

    # 2. Events are NOT AP items/locations anymore — their reach cost is inlined
    #    into the item-only compiled rules (and victory_condition), so there is
    #    nothing to lock here. See World.create_items / Regions.create_regions.

    # 2b. Metroid DNA goal. For prefer_bosses, lock the N "Metroid DNA k"
    #     items (added to the pool by World.create_items) to N random boss/
    #     EMMI/cutscene locations — same mechanism as events. For anywhere,
    #     leave them in the pool for the solver to place.
    n_dna = int(world.options.required_artifacts.value)
    if n_dna > 0 and world.options.artifact_placement.current_key == "prefer_bosses":
        from .Locations import location_table  # local import for test isolation
        boss_loc_names = [
            l.name for l in location_table
            if l.pickup_type in ("corpius", "emmi", "cutscene", "corex")
        ]
        chosen = world.random.sample(boss_loc_names, min(n_dna, len(boss_loc_names)))
        for k, loc_name in enumerate(chosen, start=1):
            item_name = f"Metroid DNA {k}"
            try:
                location = multiworld.get_location(loc_name, player)
            except KeyError:
                continue
            item = next(
                (i for i in multiworld.itempool
                 if i.player == player and i.name == item_name),
                None,
            )
            if item is not None:
                multiworld.itempool.remove(item)
                location.place_locked_item(item)

    # 3. Real victory condition. The compiled victory_condition is
    #    {type: event, name: Ship} after M2 — compile_to_lambda maps that to
    #    state.has("Event: Ship", player). When DNA is required, AND in the
    #    "collected N Metroid DNA" check; N=0 leaves the bare ship goal.
    victory_ast = compiled.get("victory_condition", {"type": "trivial"})
    base_victory = compile_to_lambda(victory_ast, player)
    if n_dna > 0:
        dna_names = tuple(f"Metroid DNA {k}" for k in range(1, n_dna + 1))
        multiworld.completion_condition[player] = (
            lambda state, b=base_victory, ns=dna_names:
                b(state) and all(state.has(n, player) for n in ns)
        )
    else:
        multiworld.completion_condition[player] = base_victory
