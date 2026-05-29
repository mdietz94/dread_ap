"""Pre-computed map of ``location_id -> (item_id, recipient_slot_idx)``.

Lifted from smo_archipelago. Wire-agnostic — works the same for Dread.

The idea: the moment Samus collects a pickup, we already know via the
scout cache what AP item that location was *going to* produce and which
slot will receive it. That lets us synthesize the in-game popup text
(``"Sent Missile Tank → Player 2"``) without waiting for the AP server
round-trip. Without the cache the popup would either show "received
nothing" for outbound items or stall for the round-trip latency.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

log = logging.getLogger(__name__)

SCOUT_CHUNK_SIZE = 200


@dataclass(frozen=True)
class ScoutInfo:
    item_id: int
    recipient: int
    flags: int = 0


class ScoutCache:
    def __init__(self) -> None:
        self._by_loc: dict[int, ScoutInfo] = {}

    def absorb(self, location: int, item: int, recipient: int, flags: int = 0) -> None:
        self._by_loc[int(location)] = ScoutInfo(int(item), int(recipient), int(flags))

    def absorb_network_item(self, ni: Any) -> None:
        if hasattr(ni, "location"):
            self.absorb(ni.location, ni.item, ni.player, getattr(ni, "flags", 0))
        elif isinstance(ni, dict):
            self.absorb(ni["location"], ni["item"], ni["player"], ni.get("flags", 0))
        else:
            raise TypeError(f"unsupported network item: {type(ni).__name__}")

    def absorb_location_info(self, args: dict) -> int:
        n = 0
        for ni in args.get("locations") or ():
            try:
                self.absorb_network_item(ni)
                n += 1
            except (KeyError, TypeError, AttributeError) as e:
                log.warning("malformed scout entry: %r (%s)", ni, e)
        return n

    def lookup(self, location_id: int) -> ScoutInfo | None:
        return self._by_loc.get(int(location_id))

    def __contains__(self, location_id: int) -> bool:
        return int(location_id) in self._by_loc

    def __len__(self) -> int:
        return len(self._by_loc)

    def clear(self) -> None:
        self._by_loc.clear()


async def request_scout(ctx: Any, location_ids: Iterable[int],
                        cache: ScoutCache | None = None) -> int:
    ids = [int(i) for i in location_ids if i and int(i) > 0]
    if cache is not None:
        ids = [i for i in ids if i not in cache]
    if not ids:
        return 0
    sent = 0
    for i in range(0, len(ids), SCOUT_CHUNK_SIZE):
        chunk = ids[i : i + SCOUT_CHUNK_SIZE]
        await ctx.send_msgs([{"cmd": "LocationScouts", "locations": chunk}])
        sent += len(chunk)
    log.info("scout: requested %d locations in %d chunk(s)",
             sent, (sent + SCOUT_CHUNK_SIZE - 1) // SCOUT_CHUNK_SIZE)
    return sent
