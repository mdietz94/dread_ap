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

Like door rando this re-rolls until the matching doesn't strand any pickup that
was reachable under vanilla transports (a random matching can otherwise cut off a
transport-only region); that no-regression matching plus the door start-guard
keeps fill bootstrappable.
"""
from __future__ import annotations

from typing import Any


# NOTE on the Hanubia<->Itorash capsule rides (``capsulelaunchershipyard``
# up-launch + ``capsuleelevatorskybase`` post-Raven-Beak escape, USABLE component
# ``CCapsuleUsableComponent``): these WERE excluded from the shuffle pool after a
# game-side strlen(NULL) crash (cazadora.nss) at the Hanubia entrances /
# post-Raven-Beak was attributed to a shuffled capsule. That attribution was
# FALSIFIED (July 2026): the identical crash signature reproduced on a seed with
# transport AND door rando fully disabled (``elevators: []``), so the crash is a
# separate Hanubia-arrival bug, not the capsule shuffle. The exclusion has been
# reverted per the project owner's direction. Capsules are typed ``elevator`` by
# the extractor, so they shuffle in the GENERAL elevator pool — exactly matching
# Randovania, whose teleporter GUI lists every transporter (capsules included) as
# a shuffled-by-default checkbox with NO capsule special-casing, and whose
# ``all_settings`` patch-data fixture shuffles both capsules into ordinary
# elevator destinations with the same ``elevators`` entry shape we emit.
# OPEN VALIDATION ITEM: the post-Raven-Beak escape ride has never been
# play-verified in-game with a SHUFFLED escape capsule (neither by us nor,
# verifiably, by Randovania) — if the escape breaks, this is the first suspect.


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


def _no_reachability_regression(graph: dict, matching: dict[str, str],
                                tl: dict) -> bool:
    """The matching doesn't strand any pickup that was reachable under VANILLA
    transports (full loadout, given trick levels).

    NOT "every pickup reachable": at low/disabled trick levels some pickups are
    trick-gated and unreachable even with a full loadout AND vanilla transports
    (e.g. the all-tricks-disabled starter preset leaves 8 Speedbooster-gated
    pickups unreachable — they hold filler under ``accessibility: minimal``).
    Requiring full reachability there would reject every roll and silently fall
    back to vanilla. The correct invariant — like DoorRando's start-frontier
    guard — is that shuffling transports never makes reachability WORSE than the
    vanilla baseline.

    Besides pickups, every vanilla-reachable TRANSPORT ENDPOINT room must stay
    reachable. This matters since the Itorash capsule rides re-entered the pool:
    Itorash holds zero pickups (the pickup criterion alone can't see it) but the
    victory sweep needs it, so a matching that strands an endpoint-only room
    would surface only as a FillError at generation. Rejecting the roll here
    keeps the retry loop the recovery path, like every other bad roll."""
    from .DoorRando import early_reachable
    full = {nm: 99 for nm in _ALL_ITEMS}
    base, _ = early_reachable(graph, full, tl, use_events=True,
                              transport_matching={})
    baseline_pickups = {comp for comp, _name in graph["pickups"] if comp in base}
    baseline_endpoints = {m["comp"] for m in graph["transports"].values()
                          if m["comp"] in base}
    reach, _ = early_reachable(graph, full, tl, use_events=True,
                               transport_matching=matching)
    return baseline_pickups <= reach and baseline_endpoints <= reach


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
    """Roll a matching that strands no pickup reachable under vanilla transports
    (full loadout, given trick levels), retrying up to ``attempts`` times; falls
    back to vanilla if none found."""
    if mode in ("off", "vanilla", None):
        return {}
    for _ in range(attempts):
        m = roll_matching(graph, rng, mode)
        if _no_reachability_regression(graph, m, tl):
            return m
    return {}  # give up -> vanilla (always reachable)


# The Ghavoran "Flipper" shuttle exists as TWO actors in the same room: a
# cutscene variant (the one the logic node names) used on the first ride, and a
# plain variant used on every ride after. open-dread-rando only repoints the
# actor we hand it, so patching just the cutscene variant leaves later rides
# pointed at the VANILLA destination. Randovania duplicates the elevator entry
# onto the second actor (DreadPatchDataFactory.create_game_specific_data); we
# mirror that here. Keyed by the cutscene actor's name.
_DUP_ACTORS = {
    "wagontrain_quarantine_with_cutscene_000": "wagontrain_quarantine_000",
}


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
        entry = {
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
        }
        out.append(entry)
        # Patch the Flipper's second (non-cutscene) actor too, or later rides
        # silently snap back to the vanilla destination.
        dup_actor = _DUP_ACTORS.get(src["actor"])
        if dup_actor is not None:
            dup = {**entry, "teleporter": {**entry["teleporter"], "actor": dup_actor}}
            out.append(dup)
    return out


def matching_to_room_names(matching: dict[str, str], graph: dict) -> dict[str, dict[str, str]]:
    """Build the room-name-display override for shuffled transports.

    Returns ``{scenario: {collision_camera: "Transport to <dest>"}}`` for EVERY
    transport endpoint (not just the moved ones), so a transport-rando seed reads
    consistently — the unmoved rides keep their name, the moved ones point at
    their new destination. Mirrors Randovania's
    ``DreadPatchDataFactory._build_teleporter_name_dict``: each source room's
    collision camera maps to ``"Transport to {dest endpoint transporter_name}"``,
    and ``transporter_name`` (e.g. "Dairon - Superheated") is the same string the
    map-icon label uses (``connection_name``), so the two stay in sync.

    Empty ``matching`` (rando off, or the connected-matching roll gave up and fell
    back to vanilla) ⇒ ``{}`` so the template's vanilla names pass through
    untouched."""
    if not matching:
        return {}
    tr = graph["transports"]
    out: dict[str, dict[str, str]] = {}
    for sid, src in tr.items():
        cc = src.get("source_cc")
        scen = src.get("scenario")
        if not cc or not scen:
            continue
        dest = matching.get(sid) or src["default_dest"]
        dmeta = tr.get(dest)
        if dmeta is None:
            continue
        name = dmeta.get("transporter_name") or ""
        if not name:
            continue
        out.setdefault(scen, {})[cc] = f"Transport to {name}"
    return out
