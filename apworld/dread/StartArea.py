"""More starting areas (Randovania-style) for the native-graph logic.

Changing where Samus spawns changes per-seed reachability — so it rides the same
native graph as door rando. AP's stock fill needs a non-empty EARLY-reachable
pickup set from the start; deep spawns (e.g. Hanubia) reach nothing with the base
kit, so fill can't bootstrap. We therefore grant a MINIMAL set of extra starting
items per spawn — the fewest that open a healthy early sphere — exactly as
Randovania picks starting items per starting location. Verified: with the extra
kit every region's spawn generates; without it only Artaria/Cataris do.

``start_node_for`` maps the ``StartingArea`` option to a concrete logic node
(deterministically, preferring save/navigation stations) and its patcher
``{scenario, actor}`` spawn point.
"""
from __future__ import annotations

from .DoorRando import early_reachable

# StartingArea option index -> Dread region. Artaria (0) is the vanilla spawn.
# Elun and Itorash (endgame/X-release areas with isolated single spawn nodes that
# don't admit a bootstrappable early sphere) are intentionally not offered.
REGION_BY_OPTION: dict[int, str] = {
    0: "Artaria", 1: "Cataris", 2: "Dairon", 3: "Burenia",
    4: "Ghavoran", 5: "Ferenia", 6: "Hanubia",
}

# Candidate starting items the minimal-kit search may grant, in rough
# early-game priority. Traversal unlocks only (no tanks/DNA — those don't open
# the early sphere and shouldn't be force-started).
_START_CANDIDATES: tuple[str, ...] = (
    "Morph Ball", "Charge Beam", "Bomb", "Spider Magnet", "Spin Boost",
    "Varia Suit", "Grapple Beam", "Wide Beam", "Power Bomb", "Speed Booster",
    "Flash Shift", "Plasma Beam", "Space Jump", "Gravity Suit", "Wave Beam",
    "Screw Attack", "Super Missile", "Cross Bomb",
)

# Early-reachable pickups the spawn must afford for fill to bootstrap. Artaria
# and Cataris (which generate as-is) sit at ~2.
_FOOTHOLD_TARGET = 2
# A probe kit for ranking start nodes by connectivity (which is best-connected).
_PROBE_KIT = ("Morph Ball", "Bomb", "Charge Beam", "Spider Magnet", "Spin Boost")
# Hard cap on the bootstrap kit so a pathological spawn can't force a huge start.
_MAX_START_ITEMS = 8


def _start_node_keys(graph: dict, region: str) -> list[str]:
    return sorted(k for k in graph["start_comps"] if k.split("::")[0] == region)


def _reach_pickup_count(graph, items, tl, start_comp, transport_matching=None,
                        is_door_rando=False) -> int:
    # Item-only (events off): a conservative lower bound on AP's live reach, so a
    # met foothold target is a real early sphere fill can use.
    reach, _ = early_reachable(graph, items, tl, start_comp, use_events=False,
                               transport_matching=transport_matching,
                               is_door_rando=is_door_rando)
    return sum(1 for comp, _name in graph["pickups"] if comp in reach)


def start_node_for(graph: dict, option_value: int, tl: dict | None = None,
                   base_items: dict | None = None, transport_matching=None,
                   door_lock_rando: bool = False):
    """Return ``(start_key, comp, patcher)`` for the chosen StartingArea, or
    ``None`` for the vanilla Artaria spawn. Among the region's save/navigation
    stations, pick the BEST-CONNECTED one (most early-reachable pickups with a
    probe kit) so the bootstrap kit stays small. Deterministic tie-break by key."""
    region = REGION_BY_OPTION.get(int(option_value), "Artaria")
    if region == "Artaria":
        return None
    tl = tl or {}
    probe = dict(base_items or {})
    for n in _PROBE_KIT:
        probe[n] = 1
    keys = _start_node_keys(graph, region)
    with_actor = [k for k in keys
                  if graph["start_comps"][k]["patcher"].get("actor")] or keys
    # Max probe-kit reach; tie-break on key for stability.
    key = max(with_actor,
              key=lambda k: (_reach_pickup_count(
                  graph, probe, tl, graph["start_comps"][k]["comp"],
                  transport_matching, door_lock_rando), k))
    meta = graph["start_comps"][key]
    return key, meta["comp"], meta["patcher"]


def minimal_start_items(graph, start_comp, base_items, tl,
                        target=_FOOTHOLD_TARGET, transport_matching=None,
                        door_lock_rando: bool = False) -> list[str]:
    """Greedily pick the fewest extra starting items (beyond ``base_items``) that
    give the spawn at least ``target`` early-reachable pickups. Handles combo
    gates (e.g. Morph + Bomb, where neither alone helps) by speculatively adding
    the next priority unlock when no single item improves reach."""
    items = dict(base_items)
    added: list[str] = []
    cur = _reach_pickup_count(graph, items, tl, start_comp, transport_matching,
                              door_lock_rando)
    while cur < target and len(added) < _MAX_START_ITEMS:
        best = None
        best_gain = 0
        for cand in _START_CANDIDATES:
            if items.get(cand, 0) >= 1:
                continue
            items[cand] = 1
            gain = _reach_pickup_count(
                graph, items, tl, start_comp, transport_matching,
                door_lock_rando) - cur
            del items[cand]
            if gain > best_gain:
                best_gain = gain
                best = cand
        if best is None:
            best_gain = 0
        if best_gain == 0:
            # No single item helps — speculatively add the next priority unlock
            # to break a combo gate (re-evaluated next loop).
            best = next((c for c in _START_CANDIDATES if items.get(c, 0) < 1), None)
            if best is None:
                break
        items[best] = 1
        added.append(best)
        cur = _reach_pickup_count(graph, items, tl, start_comp, transport_matching,
                              door_lock_rando)
    return added
