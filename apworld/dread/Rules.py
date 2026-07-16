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


# Base (pre-tank) max HP is NOT a fixed constant: both the game and Randovania
# start Samus at ``energy_per_tank - 1`` HP (open-dread-rando
# ``dread_patcher.py``: ``max_life = energy_per_tank - 1``; Randovania
# ``DreadBootstrap.create_damage_state``: ``DreadDamageState(energy_per_tank - 1,
# ...)``). BASE_HP below is only the VANILLA value (100/tank -> 99), used for the
# default-config energy-progression sizing in World.py and vanilla-budget tests;
# the damage predicate itself derives the base from the slot's energy_per_tank.
# An Energy Part is worth 1/4 of a tank (4 parts == 1 tank), matching
# Randovania's pickup config and the in-game grant.
BASE_HP = 99
VANILLA_ENERGY_PER_TANK = 100
PARTS_PER_TANK = 4

# Faithful v0.3 ammo model. The baked ``sum`` atoms carry the vanilla starting
# capacity in ``base`` and the vanilla per-tank yield in each term's
# ``per_unit`` (missiles: base 15, +2/Missile Tank, +10/Missile+ Tank; power
# bombs: base 0, +2/Power Bomb launcher item, +2/Power Bomb Tank). The
# ``threshold`` is the route's raw ammo demand and is independent of the user's
# options. ``ammo_amounts`` (a dict, see ``compile_to_lambda``) lets a slot
# rescale ``base``/``per_unit`` to its own ``starting_missiles`` /
# ``*_tank_ammo`` / ``starting_power_bombs`` settings, so those knobs feed
# LOGIC instead of being pure difficulty. Keys are the resource lines below;
# values are (base_key, {item_name: per_unit_key}). An absent key keeps the
# graph's baked value.
AMMO_BASE_KEYS = {
    "Missile Tank": "missile_base",
    "Missile+ Tank": "missile_base",
    "Power Bomb": "pb_base",
    "Power Bomb Tank": "pb_base",
}
AMMO_PER_UNIT_KEYS = {
    "Missile Tank": "missile_tank_per_unit",
    "Missile+ Tank": "missile_plus_tank_per_unit",
    "Power Bomb": "power_bomb_per_unit",
    "Power Bomb Tank": "power_bomb_tank_per_unit",
}


def compile_to_lambda(
    ast: dict, player: int, trick_levels: dict[str, int] | None = None,
    graph_mode: bool = False, energy_per_tank: int = VANILLA_ENERGY_PER_TANK,
    ammo_amounts: dict[str, int] | None = None,
    door_lock_rando: bool = False,
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

    ``energy_per_tank`` scales the HP budget a ``damage_threshold`` atom is
    checked against (faithful v0.3 damage model). The pre-tank base is
    ``energy_per_tank - 1`` (the game / Randovania start Samus one below a full
    tank), each Energy Tank grants ``energy_per_tank`` and each Energy Part 1/4
    of it; the baked ``hp_needed`` values are raw damage amounts (independent of
    this knob), so a player who lowers ``energy_per_tank`` correctly needs more
    tanks/parts to clear the same gate.

    ``ammo_amounts`` similarly rescales ``sum`` (missile / power-bomb capacity)
    atoms to the slot's ammo settings — see ``AMMO_BASE_KEYS`` /
    ``AMMO_PER_UNIT_KEYS``. Recognized keys: ``missile_base`` (starting missile
    capacity), ``missile_tank_per_unit``, ``missile_plus_tank_per_unit``,
    ``pb_base`` (starting power bombs), ``power_bomb_per_unit`` (launcher grant),
    ``power_bomb_tank_per_unit``. Absent keys keep the graph's baked vanilla
    value. The ``threshold`` (raw ammo demand of the route) is never rescaled.
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

    if t == "misc":
        # DoorLocks is Randovania's "Door Lock Randomizer" misc resource: True
        # when door-lock rando is active for the seed. ``NOT DoorLocks`` guards
        # 83 door-interacting maneuvers (shinesparks carried through a door,
        # opening a specific vanilla door type through terrain with Wave, ...)
        # that are only valid while every door has its VANILLA lock. Randovania
        # itself kills ALL of these branches in a door-rando seed; we mirror
        # that via the ``door_lock_rando`` argument.
        #
        # History: this used to hardcode ``door_locks_active = False`` (always
        # credit the maneuvers) on the WRONG belief that the doors involved are
        # never randomized. The doors are modelled as ``dock`` atoms elsewhere,
        # but the maneuver PHYSICALLY crosses them mid-move (e.g. the Artaria
        # Map Station shinespark runs through the Map Station↔Waterfall door):
        # a seed that rolls that door to Grapple makes the sprint impossible
        # while the old resolution still credited it — a live false positive
        # (seed AP-00908778: both Waterfall pickups "in logic" for a player who
        # could not reach them). An even earlier fix (#124) resolved this
        # faithfully but caused FillErrors: severing the branches walls off the
        # Cataris Underlava pocket whose only full-loadout entry is such an
        # edge. That is now absorbed by World._compute_dropped_locations (the
        # walled-off pickup is dropped under ``full``/``items`` and stays as
        # Randovania-faithful maybe-unreachable filler under ``minimal``), so
        # the faithful resolution is safe.
        negate = bool(ast.get("negate", False))
        holds = (not door_lock_rando) if negate else door_lock_rando
        return _const_true if holds else _const_false

    if t == "sum":
        amounts = ammo_amounts or {}
        base = int(ast["base"])
        thr = int(ast["threshold"])
        terms_list = []
        for tr in ast["terms"]:
            nm = tr["name"]
            per = int(tr["per_unit"])
            bkey = AMMO_BASE_KEYS.get(nm)
            if bkey is not None and bkey in amounts:
                base = int(amounts[bkey])
            pkey = AMMO_PER_UNIT_KEYS.get(nm)
            if pkey is not None and pkey in amounts:
                per = int(amounts[pkey])
            terms_list.append((nm, per))
        terms = tuple(terms_list)
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
        per_tank = int(energy_per_tank)
        per_part = per_tank / PARTS_PER_TANK
        # Base (pre-tank) max HP scales with energy_per_tank: the game and
        # Randovania both start Samus at ``energy_per_tank - 1`` (see BASE_HP
        # note above). At the vanilla 100/tank this is 99.
        base_hp = per_tank - 1
        def _dthr_pred(state, _p=player, _suits=suits, _hp=hp,
                       _base=base_hp, _pt=per_tank, _pp=per_part):
            for s in _suits:
                if state.has(s, _p):
                    return True
            budget = _base + _pt * state.count("Energy Tank", _p) \
                        + _pp * state.count("Energy Part", _p)
            return budget >= _hp
        return _dthr_pred

    if t == "and":
        children = [compile_to_lambda(c, player, trick_levels, graph_mode,
                                      energy_per_tank, ammo_amounts,
                                      door_lock_rando)
                    for c in ast["items"]]
        if not children:
            return _const_true
        if len(children) == 1:
            return children[0]
        return lambda state, cs=children: all(c(state) for c in cs)

    if t == "or":
        children = [compile_to_lambda(c, player, trick_levels, graph_mode,
                                      energy_per_tank, ammo_amounts,
                                      door_lock_rando)
                    for c in ast["items"]]
        if not children:
            return _const_false
        if len(children) == 1:
            return children[0]
        return lambda state, cs=children: any(c(state) for c in cs)

    raise ValueError(f"unknown rule AST type: {t!r}")
