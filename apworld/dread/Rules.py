"""AST → lambda compiler for the native region-graph logic model.

``compile_to_lambda`` translates the symbolic rule AST emitted by
``scripts/extract_dread_rules.py`` (graph edges, victory condition) into
a ``state.has(...)`` / ``state.can_reach_region(...)`` predicate.  It is
called by ``graph_logic.py`` with ``graph_mode=True`` for every entrance
rule and for the victory condition.

Closure-capture care: every list-comprehension binds locals (``name``,
``amount``, etc.) eagerly so the resulting lambda is not bitten by
Python's late-binding rule.
"""
from __future__ import annotations

from typing import Any, Callable

# A Predicate is a function (state) -> bool, with `player` already
# closed over. Kept duck-typed (no CollectionState import) so the unit
# tests can exercise compile_to_lambda without an Archipelago install.
Predicate = Callable[[Any], bool]


def _const_true(_: Any) -> bool:
    return True


def _const_false(_: Any) -> bool:
    return False


def compile_to_lambda(
    ast: dict, player: int, trick_levels: dict[str, int] | None = None,
    graph_mode: bool = False,
) -> Predicate:
    """Translate a compiled rule AST into a Predicate.

    ``trick_levels`` maps each trick short_name to the slot's effective level
    (``Tricks.effective_trick_levels``); a ``trick`` atom of level N is assumed
    iff ``N <= trick_levels[name]``. A trick missing from the map (or no map at
    all) resolves to level 0 = disabled, the conservative fallback.

    ``graph_mode`` selects the native-graph event model: an ``event`` atom
    compiles to ``state.can_reach_region("Event:<name>")`` instead of
    ``state.has("Event: <name>")``.  In graph mode, ``dock`` atoms must be
    pre-substituted by the caller.
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
        name = ast["name"]
        if graph_mode:
            rn = f"Event:{name}"
            return lambda state, r=rn: state.can_reach_region(r, player)
        return lambda state, n=f"Event: {name}": state.has(n, player)

    if t == "trick":
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
        children = [compile_to_lambda(c, player, trick_levels, graph_mode)
                    for c in ast["items"]]
        if not children:
            return _const_true
        if len(children) == 1:
            return children[0]
        return lambda state, cs=children: all(c(state) for c in cs)

    if t == "or":
        children = [compile_to_lambda(c, player, trick_levels, graph_mode)
                    for c in ast["items"]]
        if not children:
            return _const_false
        if len(children) == 1:
            return children[0]
        return lambda state, cs=children: any(c(state) for c in cs)

    raise ValueError(f"unknown rule AST type: {t!r}")
