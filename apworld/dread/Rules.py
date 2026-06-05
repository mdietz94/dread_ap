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
  * Per-trick configuration (world_version 0.5.0). Tricks are kept SYMBOLIC
    in one ``compiled_rules.json`` (``{"type":"trick","name","level"}``);
    ``compile_to_lambda`` resolves each against the slot's effective per-trick
    levels (``Tricks.effective_trick_levels`` — global ``Trick Level`` baseline
    plus per-trick overrides). The old three-file (Beginner/Intermediate/
    Advanced) bake is gone.
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


def compile_to_lambda(
    ast: dict, player: int, trick_levels: dict[str, int] | None = None
) -> Predicate:
    """Translate a compiled rule AST into a Predicate.

    ``trick_levels`` maps each trick short_name to the slot's effective level
    (``Tricks.effective_trick_levels``); a ``trick`` atom of level N is assumed
    iff ``N <= trick_levels[name]``. A trick missing from the map (or no map at
    all) resolves to level 0 = disabled, the conservative fallback.

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
        # Symbolic (v3): the trick is assumed iff the slot's effective level for
        # it is at least this requirement's level. A constant per seed (depends
        # only on options, not collected items), so it never reintroduces the
        # item↔event cycle the forward resolver exists to break.
        level = int(ast.get("level", 1))
        eff = (trick_levels or {}).get(ast["name"], 0)
        return _const_true if level <= eff else _const_false

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
        children = [compile_to_lambda(c, player, trick_levels) for c in ast["items"]]
        if not children:
            return _const_true
        if len(children) == 1:
            return children[0]
        return lambda state, cs=children: all(c(state) for c in cs)

    if t == "or":
        children = [compile_to_lambda(c, player, trick_levels) for c in ast["items"]]
        if not children:
            return _const_false
        if len(children) == 1:
            return children[0]
        return lambda state, cs=children: any(c(state) for c in cs)

    raise ValueError(f"unknown rule AST type: {t!r}")


# Must match scripts/extract_dread_rules.py::SCHEMA_VERSION. A mismatch means
# the on-disk artifact predates a vocabulary change (v1 had `damage` nodes, v2
# added `sum` + `damage_threshold`, v3 keeps tricks symbolic) and would silently
# route through stale semantics. Fail closed and prompt for a regen.
EXPECTED_SCHEMA_VERSION = 3


def load_compiled_rules() -> dict[str, Any]:
    """Load the single compiled rule set.

    Since v3 there is ONE artifact: tricks are kept symbolic and resolved
    per-trick at AP-generation time (see ``compile_to_lambda`` /
    ``Tricks.effective_trick_levels``), so there is no longer a file per trick
    level. A missing file raises FileNotFoundError, preserving the "everything
    reachable" fallback that set_rules / create_regions honor.

    Raises ``RuntimeError`` if the artifact's ``schema_version`` does not match
    ``EXPECTED_SCHEMA_VERSION`` — regenerate with
    ``python scripts/extract_dread_rules.py --all``."""
    compiled = load_json("compiled_rules.json")
    version = compiled.get("schema_version")
    if version != EXPECTED_SCHEMA_VERSION:
        raise RuntimeError(
            f"compiled_rules.json schema_version={version!r} but loader "
            f"expects {EXPECTED_SCHEMA_VERSION}. Regenerate with "
            f"`python scripts/extract_dread_rules.py --all`."
        )
    return compiled


def set_rules(world) -> None:
    """Apply add_rule for every compiled location rule.

    Locations not present in compiled_rules.json (boss / EMMI / cutscene
    pickups) get no per-pickup rule, but they are NOT trivially reachable:
    Regions.py gates each region's Menu edge on its global region_access
    rule, so a boss is reachable only once its region is. Also locks the
    Metroid DNA goal items (prefer_bosses).

    Logic models entry reachability only — it does NOT guarantee a player
    can leave a pickup room. Soft-lockable placements are therefore
    possible by design; the client's ``/warp`` command (warp to the
    starting save station) is the recovery path. See CLAUDE.md.
    """
    from worlds.generic.Rules import add_rule  # local import for test isolation

    from .Tricks import effective_trick_levels

    multiworld = world.multiworld
    player = world.player
    trick_levels = effective_trick_levels(world.options)

    try:
        compiled = load_compiled_rules()
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
        predicate = compile_to_lambda(rule_ast, player, trick_levels)
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

    # 2c. (removed) Softlock prevention is no longer done in logic. Logic
    #     models entry reachability only; a room that needs an item to LEAVE
    #     can be assigned any item by fill. Recovery is the client's ``/warp``
    #     command (warp to the starting save station) rather than pinned
    #     items / filler-only constraints. See CLAUDE.md.

    # 3. Real victory condition. The compiled victory_condition is
    #    {type: event, name: Ship} after M2 — compile_to_lambda maps that to
    #    state.has("Event: Ship", player). When DNA is required, AND in the
    #    "collected N Metroid DNA" check; N=0 leaves the bare ship goal.
    victory_ast = compiled.get("victory_condition", {"type": "trivial"})
    base_victory = compile_to_lambda(victory_ast, player, trick_levels)
    if n_dna > 0:
        dna_names = tuple(f"Metroid DNA {k}" for k in range(1, n_dna + 1))
        multiworld.completion_condition[player] = (
            lambda state, b=base_victory, ns=dna_names:
                b(state) and all(state.has(n, player) for n in ns)
        )
    else:
        multiworld.completion_condition[player] = base_victory
