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

v0.1 simplifications still in flight (see
``docs/randovania-logic-port-notes.md``):
  * Tricks at level=1 evaluate as Trivial; level≥2 as False. The
    user-facing trick-level Choice option is Gate B / a follow-up.
  * Damage requirements collapse to suit ownership (Lava/Heat → Varia
    or Gravity; Cold → Gravity; raw → True). No E-Tank counting.
  * Cross-region access is unmodeled (Regions.py still uses star
    topology). Once per-region edges land, the area-isolated reach
    rules already in compiled_rules.json compose with them naturally.
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

    if t == "damage":
        return _const_true

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


def load_compiled_rules() -> dict[str, Any]:
    try:
        return load_json("compiled_rules.json")
    except FileNotFoundError:
        # Preserve the "everything reachable" fallback that set_rules has
        # always honored — raising FileNotFoundError keeps the call site
        # behavior identical to the old Path(...).read_text() error.
        raise


def set_rules(world) -> None:
    """Apply add_rule for every compiled location rule.

    For locations not present in compiled_rules.json (e.g. boss /
    EMMI / cutscene pickups whose progression is handled by the
    patcher's pickup_lua_callback hooks), no rule is added — the
    location stays trivially reachable.
    """
    from worlds.generic.Rules import add_rule  # local import for test isolation

    multiworld = world.multiworld
    player = world.player

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
        predicate = compile_to_lambda(rule_ast, player)
        add_rule(location, predicate)

    # 2. Per-event reach rules + lock the event item to the event
    #    location. The event branch of compile_to_lambda already
    #    points event references at "Event: <name>" items; here we
    #    place exactly one of each so the solver can satisfy them.
    for event in compiled.get("events", []):
        loc_name = f"Event: {event['name']}"
        item_name = f"Event: {event['name']}"
        try:
            location = multiworld.get_location(loc_name, player)
        except KeyError:
            continue
        predicate = compile_to_lambda(event["rule"], player)
        add_rule(location, predicate)
        # Pin the event item to its event location. The item was added
        # to the itempool by World.create_items; pull it out and lock
        # it here. (Double-placement crashes generation, so each event
        # item must appear in the pool exactly once.)
        item = next(
            (i for i in multiworld.itempool
             if i.player == player and i.name == item_name),
            None,
        )
        if item is not None:
            multiworld.itempool.remove(item)
            location.place_locked_item(item)

    # 3. Real victory condition. The compiled victory_condition is
    #    {type: event, name: Ship} after M2 — but compile_to_lambda
    #    already maps that to state.has("Event: Ship", player), so
    #    just reuse it.
    victory_ast = compiled.get("victory_condition", {"type": "trivial"})
    multiworld.completion_condition[player] = compile_to_lambda(victory_ast, player)
