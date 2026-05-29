# Wire-wiring retrospective — closing the Switch↔AP gap

Companion to [docs/wire-wiring-e2e.md](wire-wiring-e2e.md) (the plan).
This doc captures the actual experience landing Gate A + Gate B: what
was easy, what wasn't, what the next milestone has to budget for.

## TL;DR

Gate A is in. The AP↔Switch direction was already wired and unit-tested,
but the Switch↔AP path (player collects → AP server sees CheckLocations)
was a no-op. That's closed: every Switch→PC frame is now demuxed by
type byte, COLLECTED_INDICES is parsed against the `locations:` bitfield
contract, mapped to AP location_ids via a new `pickup_index` field on
`locations.json`, deduped against `BridgeState`, and forwarded as
`LocationChecks` to the AP server.

Gate B is also in. `DreadWorld.generate_output` now writes a per-slot
placements JSON during AP generation; `scripts/seed_to_patcher_overrides.py`
consumes that JSON (extracted from the seed zip) and emits the override
shape that `scripts/build_patcher_json.py` already consumed. The pipeline
generate → convert → merge → patcher → RomFS now works end-to-end.
186 tests pass.

## The headline surprise: the wire format documented in CLAUDE.md was wrong

The plan's framing assumption was:

> **Any response frame**: `[1-byte success bool][3-byte LE length][payload]`

And the wire-wiring plan correctly predicted the demux problem but
guessed at the push-frame shape:

> "The most likely shape … push frames begin with a type byte … then the
> standard `[success][len_24][payload]`. Lua-exec replies don't have the
> type byte because the receiver knows it just sent a `PACKET_REMOTE_LUA_EXEC`
> request."

Reading the actual exlaunch C++ sender
(`vendor/open-dread-rando-exlaunch/source/program/{remote_api.cpp,main.cpp}`)
showed both assumptions were wrong:

- **Every** Switch→PC frame begins with a 1-byte `PacketType` (including
  Lua-exec replies). The Python upstream parser reads that byte first
  and `match`-dispatches.
- The Lua-exec reply layout is
  `[0x03][req_num:1][success:1][len:3 LE u24][payload]` — there's a
  `request_number` echo byte we didn't know about.
- Push frames use a **4-byte u32** length, not the 3-byte u24 used in
  replies. So they're `[type:1][len:4 LE u32][payload]`.
- The HANDSHAKE reply is just `[0x01][req_num]` — 2 bytes total, no
  body. Not a 4-byte `[success][len_24]` like our code assumed.
- `PACKET_MALFORMED` is a fixed 10-byte
  `[0x09][failing_type][rcv:4][should:4]` diagnostic.

So before any push-demux work, the entire reply-reading code had to be
rewritten. Our old `lp.parse_response_header` was reading
`[type][req_num][success_byte_0]` and interpreting it as
`[success][len_byte_0][len_byte_1][len_byte_2]`. That accidentally worked
in tests because `FakeSwitch` emitted the same wrong shape; on real
hardware the executor would have stalled or corrupted every frame.

`scripts/phase1_validate.py` had the same bug and was documented as
"Lifted verbatim from upstream Randovania" — almost certainly a misread
of the upstream code, possibly never run against real hardware.

### What changed

- `apworld/dread_archipelago/client/lua_packets.py`: replaced the single
  `parse_response_header` with three type-specific parsers
  (`parse_lua_exec_reply_header`, `parse_push_length`,
  `parse_malformed_body`), plus `PUSH_TYPES` / `REPLY_TYPES` sets.
- `apworld/dread_archipelago/client/lua_executor.py`: collapsed the old
  `_read_one` into a `_read_frame` that reads the type byte and
  dispatches; `_read_loop` then routes to either the pending-future or
  `on_push` based on the type byte.
- `apworld/dread_archipelago/tests/test_lua_executor.py`: `FakeSwitch`
  rewritten to emit the actual wire bytes. Added 4 new tests
  (push routes to handler, reply still routes to future, interleaved
  push+reply, unknown type raises).
- `scripts/phase1_validate.py`: same protocol fix applied. Now correctly
  drains any pushes that arrive while waiting for a Lua-exec reply.

## Pickup-index map — went with Option (b)

Plan offered (a) a separate `data/pickup_index_map.json` data file vs
(b) adding a `pickup_index` field directly to existing `locations.json`
entries. Picked (b): fewer moving parts, no DataPackage cache loading
churn, the field is just additive on each row.

Discovery making this trivial: when I diffed locations.json against
the patcher template, the non-event locations.json entries were already
in the exact template-pickups-array order. So
`pickup_index = position in locations.json (filtered to non-event)`.
Updated `scripts/extract_dread_data.py` to emit it as `offset`;
re-running extract + append_event_data left every AP ID unchanged
(hash-comparison validated).

`DataPackage.pickup_index_to_location_id()` is a one-line dict lookup.

Spot-checks in `test_pickup_index_map.py`:
- pickup_index 0 → Artaria/ItemSphere_ChargeBeam ✓
- pickup_index 137 → first non-actor pickup ✓
- pickup_index 148 → Cataris/OnKraidDeath_CUSTOM ✓

## COLLECTED_INDICES payload format

Confirmed from upstream `MercuryConnector.new_collected_locations_received`:

```python
start_of_bytes = b"locations:"
if new_indices.startswith(start_of_bytes):
    index = 0
    for c in new_indices[len(start_of_bytes):]:
        for i in range(8):
            if c & (1 << i):
                locations.add(PickupIndex(index))
            index += 1
```

So: `b"locations:" + packed_bitfield`. Each byte represents 8 pickup
indices, LSB-first. The bootstrap Lua dumps the FULL collected set on
every poll (and every reconnect), so dedup against `BridgeState`
matters — without it we'd send a LocationChecks containing every
previously-collected location every 2 seconds.

## Other push payloads

- `NEW_INVENTORY`: JSON `{"index":int,"inventory":[float,...]}`. The
  inventory array is positional (slot 0, 1, ...). We stash it as
  `slot0`/`slot1`/etc. in BridgeState for diagnostics. A proper
  slot↔name map is v0.2.
- `GAME_STATE`: semicolon-delimited `<state>[;<beaten:bool>]`.
- `LOG_MESSAGE`: utf-8 string → BridgeState log surface.
- `RECEIVED_PICKUPS`: ignored for v0.1 (we already track position
  cursor on the PC side).

## Gate B — generate_output + converter

The plan envisioned a converter that reads the seed zip. Going the
other way around — having `DreadWorld.generate_output` write per-slot
placement JSON, which AP auto-bundles into the seed zip — avoided
parsing the binary `.archipelago` multidata entirely.

The placements JSON shape is documented at the top of
[scripts/seed_to_patcher_overrides.py](../scripts/seed_to_patcher_overrides.py).
The converter:

- Maps own-slot items → `pickup_resources` with the Dread `patcher_item_id`
  and quantity from `items.json`.
- Maps cross-slot items → a `CROSS_SLOT_PLACEHOLDER` (Missile Tank-shaped)
  resource + a `"Sent <item> to <recipient>"` caption.
- Skips event-type placements (synthetic, no Switch counterpart).
- Skips non-actor pickups (EMMI/corex/corpius/cutscene) — the patcher
  keys them by `pickup_lua_callback`, not `pickup_actor`, and
  `build_patcher_json.py` only knows how to override actor-keyed ones.
  For v0.1 they stay at vanilla resources. v0.2 work item.

The derived `layout_uuid` is a SHA-256 hash of `seed_id:slot_name` sliced
into the schema-required `8-4-4-4-12` hex shape, with version-4 +
variant nibbles forced. Stable across re-runs, unique per slot.

## What v0.2 / next milestones need to budget for

- **Kivy GUI** is still a separate milestone. The headless client works
  but the user-facing dev experience needs the in-app status pane,
  command bar, item history view. Lift from `smo_archipelago/client/gui.py`.
- **Cross-region access rules** — Regions.py is still a star.
  `accessibility: items` fails. M2 Gate B punch list in
  [randovania-logic-port-m2plumbing.md](randovania-logic-port-m2plumbing.md).
- **Trick-level UI Choice** — currently no logic-difficulty option.
- **Progressive items** — Progressive Beam / Progressive Suit not yet
  modeled.
- **Non-actor pickup overrides** in the patcher converter. EMMI/boss/
  cutscene rewards currently stay vanilla. To AP-ify them we need
  `build_patcher_json.py` to handle lua_callback-keyed pickups.
- **Real-hardware E2E run.** The dev machine for this milestone didn't
  have a Switch. The runbook documents the manual steps; an actual
  human session on hardware is the next gate.
- **Better cross-slot placeholder.** Right now cross-slot Dread locations
  always get a Missile Tank. Vanilla Randovania uses a "Multiworld Marker"
  with a dedicated icon. Pick that up when the patcher exposes the IDs.
- **`RL.ReceivePickup` during cinematics** — `RL.GivePendingPickup`
  no-ops while a cutscene is playing. The smo_archipelago pattern of a
  pending queue + post-HELLO replay needs lifting.

## Test count

| Milestone | tests passing |
|---|---|
| Pre-milestone (baseline)            | 144 |
| + wire-format fix + push-demux      | 153 (-5 old, +14 new) |
| + pickup_index map                  | 163 |
| + context Switch→AP path            | 172 |
| + seed→patcher converter            | 186 |

The 5 deleted tests were all `test_parse_response_header_*` against the
old (incorrect) reply parser. Replaced by `test_parse_lua_exec_reply_header_*`
+ `test_parse_push_length_*` + `test_parse_malformed_body`.

## Verification

```pwsh
# All tests pass
python -m pytest apworld/dread_archipelago/tests/ scripts/tests/ -q

# Wire-format unit tests
python -m pytest apworld/dread_archipelago/tests/test_lua_packets.py apworld/dread_archipelago/tests/test_lua_executor.py -v

# Pickup-index map invariants
python -m pytest apworld/dread_archipelago/tests/test_pickup_index_map.py -v

# Switch→AP path with a fake AP server
python -m pytest apworld/dread_archipelago/tests/test_context_e2e.py -v

# Seed → patcher converter
python -m pytest scripts/tests/test_seed_to_patcher.py -v
```
