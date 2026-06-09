"""Transporter/elevator rando for the native-graph logic.

Shuffles where each elevator/shuttle ride arrives, as a TWO-WAY matching: if the
transport at endpoint X now reaches Y, then Y's transport reaches X. Shuffling is
WITHIN type (elevator<->elevator, shuttle<->shuttle) so the patcher always has a
compatible target spawn point. Teleporters are left vanilla (open-dread-rando's
elevator patcher only fully supports the TRANSPORT type — elevators/shuttles).

The matching feeds two consumers:
  * logic — ``graph_logic.build_regions`` adds the ride edges from the matching,
    so AP's live sweep re-derives reachability for this seed.
  * patcher — ``matching_to_elevators`` emits the open-dread-rando ``elevators``
    config (source actor -> destination scenario + target spawn point).

Like door rando this re-rolls until the matching keeps every pickup reachable
with a full loadout (a random matching can otherwise strand a transport-only
region); a connected matching plus the door start-guard keeps fill bootstrappable.
"""
from __future__ import annotations

from typing import Any


def _by_type(graph: dict) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for sid, meta in graph.get("transports", {}).items():
        groups.setdefault(meta["type"], []).append(sid)
    # Deterministic order before shuffling (rng drives the actual permutation).
    for g in groups.values():
        g.sort()
    return groups


def roll_matching(graph: dict, rng, mode: str = "randomized") -> dict[str, str]:
    """Return a two-way endpoint matching ``{side_id: dest_side_id}`` (both
    directions). ``mode`` 'off'/'vanilla' yields ``{}`` (default pairing)."""
    if mode in ("off", "vanilla", None):
        return {}
    matching: dict[str, str] = {}
    for _type, sides in _by_type(graph).items():
        order = list(sides)
        rng.shuffle(order)
        # Pair consecutive endpoints. Odd tail (shouldn't happen — counts are
        # even) keeps its vanilla destination.
        for i in range(0, len(order) - 1, 2):
            a, b = order[i], order[i + 1]
            matching[a] = b
            matching[b] = a
    return matching


def _full_reachable_ok(graph: dict, matching: dict[str, str], tl: dict) -> bool:
    """Every pickup reachable with a full loadout under this matching (i.e. the
    transport graph didn't strand a region)."""
    from .DoorRando import early_reachable
    full = {nm: 99 for nm in _ALL_ITEMS}
    reach, _ = early_reachable(graph, full, tl, use_events=True,
                               transport_matching=matching)
    return all(comp in reach for comp, _name in graph["pickups"])


_ALL_ITEMS = (
    "Morph Ball", "Bomb", "Cross Bomb", "Power Bomb", "Charge Beam", "Wide Beam",
    "Plasma Beam", "Wave Beam", "Diffusion Beam", "Grapple Beam", "Ice Missile",
    "Storm Missile", "Super Missile", "Varia Suit", "Gravity Suit",
    "Phantom Cloak", "Flash Shift", "Pulse Radar", "Speed Booster",
    "Spider Magnet", "Spin Boost", "Space Jump", "Screw Attack", "Slide",
    "Energy Tank", "Energy Part", "Missile Tank", "Missile+ Tank",
    "Power Bomb Tank", "Flash Shift Upgrade", "Speed Booster Upgrade",
)


def roll_connected_matching(graph: dict, rng, tl: dict,
                            mode: str = "randomized", attempts: int = 50):
    """Roll a matching that keeps every pickup reachable with a full loadout,
    retrying up to ``attempts`` times; falls back to vanilla if none found."""
    if mode in ("off", "vanilla", None):
        return {}
    for _ in range(attempts):
        m = roll_matching(graph, rng, mode)
        if _full_reachable_ok(graph, m, tl):
            return m
    return {}  # give up -> vanilla (always reachable)


def matching_to_elevators(matching: dict[str, str], graph: dict) -> list[dict[str, Any]]:
    """Build the open-dread-rando ``elevators`` config from the matching. One
    entry per transport endpoint whose destination changed from vanilla."""
    tr = graph["transports"]
    out: list[dict[str, Any]] = []
    for sid, dest in matching.items():
        src = tr.get(sid)
        dmeta = tr.get(dest)
        if src is None or dmeta is None:
            continue
        if dest == src["default_dest"]:
            continue  # unchanged
        out.append({
            "teleporter": {"scenario": src["scenario"], "actor": src["actor"]},
            # Land at the DESTINATION endpoint's own landing platform
            # (``start_point`` = Randovania's ``start_point_actor_name``), which
            # physically lives in ``dmeta["scenario"]``. Using ``target_spawn_point``
            # here was a bug: that actor belongs to the destination's VANILLA
            # destination scenario, so the engine loaded the right room but
            # couldn't find the spawn → crash on the ride.
            "destination": {"scenario": dmeta["scenario"],
                            "actor": dmeta["start_point"]},
            "connection_name": dmeta.get("transporter_name", ""),
        })
    return out
