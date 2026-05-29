# Wire wiring — close the E2E gap

## Context (read this first)

The `dread_ap` project ships a working Metroid Dread Archipelago client
(see [CLAUDE.md](../CLAUDE.md)). Phases 1–5 + Logic Milestones 1 & 2
Gate A are complete. 144 unit tests pass.

The one remaining gap before a real human can play Dread + another game
in a multiworld is the **runtime wire wiring**. The AP→Switch direction
(receiving items via `RL.ReceivePickup`) is wired and unit-tested. The
**Switch→AP direction (the player collecting a pickup in-game → AP
server seeing a CheckLocations) is a no-op**:

- [context.py:291-297](../apworld/dread_archipelago/client/context.py#L291-L297)
  `_dispatch_collected_response` does `del resp` and returns.
- [context.py:299-310](../apworld/dread_archipelago/client/context.py#L299-L310)
  `_on_switch_push` logs the payload bytes and drops them on the floor.

There's also a missing translation step between an AP-generated seed and
the patcher's input format — no current code path takes a generated seed
zip and produces the JSON our patcher adapter consumes.

Your job is to close both gaps + the wire-layer plumbing they expose.

## Scope

This is **wire wiring**: parse push frames, map pickup_index ↔ AP
location_id, dispatch CheckLocations, build a seed→patcher converter.
You are NOT building a GUI (Kivy comes later as a separate milestone).
You are NOT extending the rule compiler (it's done). You are NOT
touching the patcher itself (only the adapter input).

### Done when

Split into two gates so the harder one (Gate A) can ship even if Gate B
runs long.

**Gate A — runtime wire E2E.** Required.

1. `lua_executor` distinguishes push frames from Lua-exec replies via
   type-byte inspection. Pushes route to `on_push` with the correct
   `PacketType`; replies route to the pending future via the existing
   FIFO lock.
2. `_on_switch_push` parses `PACKET_COLLECTED_INDICES` payloads
   (bitfield of pickup_index ints), maps to AP location_ids, and
   sends `CheckLocations` to the AP server. Idempotent (re-receiving
   the same indices on a Switch reconnect doesn't duplicate the AP
   message).
3. A pickup_index ↔ AP location_id mapping exists — see "Mapping
   strategy" below for two options.
4. New tests in `test_lua_executor.py` confirm push demux works
   against the in-process fake server (fake server emits a
   `PACKET_COLLECTED_INDICES` frame; assert `on_push` got called
   with the right type and payload).
5. New tests in `test_context_e2e.py` (new file) confirm:
   collected-indices payload → CheckLocations message via a mocked
   AP `send_msgs`.
6. All 144 pre-existing tests still pass.

**Gate B — seed → patcher translation.** Try to land; descope cleanly.

7. `scripts/seed_to_patcher_overrides.py` exists and converts a
   generated AP seed (the per-slot data inside the seed zip) to the
   JSON our existing `scripts/build_patcher_json.py` consumes.
8. A pipeline that runs end-to-end: generate seed → convert →
   build_patcher_json → produces a romfs/ tree the exlaunch
   sysmodule could serve. Document with an example invocation in
   `docs/e2e-runbook.md`.
9. A 2-slot test fixture: `apworld/dread_archipelago/tests/seeds/
   dread_clique.yaml` (Dread + Clique). Used by the integration
   tests to prove a real multiworld generates clean.
10. A short `docs/e2e-runbook.md` walking a user from "fresh Switch
    dump" to "first multiworld gameplay session".

### Explicitly NOT in scope

- Kivy GUI (separate, later milestone).
- Cross-region access wiring in Regions.py (M2 Gate B, separate).
- Trick-level Option (M2 Gate B, separate).
- Modifying the patcher itself (we only build its input JSON).
- Modifying the wire layer beyond push demux (no protocol changes).
- Real-hardware test execution (you may not have a Switch; the runbook
  documents the manual steps, that's enough).

## Gate A — the high-impact wiring

### A1. Push-frame demux in `lua_executor`

**Today's behavior**: [lua_executor._read_loop](../apworld/dread_archipelago/client/lua_executor.py#L143-L166)
treats every Switch-originated frame as a Lua-exec reply, fulfilling
the pending future positionally. Push frames (NEW_INVENTORY,
COLLECTED_INDICES, etc.) interleaving with Lua-exec replies will
corrupt the FIFO — today's tests don't catch it because the fake
server only sends replies.

**You must first verify the exact wire format of push frames.** Fetch
upstream Randovania to check this:

```
https://raw.githubusercontent.com/randovania/randovania/main/randovania/game_connection/executor/dread_executor.py
```

Specifically look at the `read_loop` method (or similarly-named) and
`_check_header`. The question to answer: do push frames begin with a
1-byte PacketType prefix, or do they share the same `[success][len_24]
[payload]` shape as Lua-exec replies (and rely on out-of-band context
to distinguish)?

The most likely shape based on the existing `PacketType` enum spanning
both directions is: **push frames begin with a type byte** (one of
0x05–0x09 for `PACKET_NEW_INVENTORY`/`COLLECTED_INDICES`/`RECEIVED_PICKUPS`/
`GAME_STATE`/`MALFORMED`), then the standard `[success][len_24][payload]`.
Lua-exec replies don't have the type byte because the receiver knows
it just sent a `PACKET_REMOTE_LUA_EXEC` request.

If that's confirmed, the demux is:

```python
async def _read_loop(self) -> None:
    while True:
        # Peek the first byte
        first = await self._reader.readexactly(1)
        try:
            packet_type = lp.PacketType(first[0])
        except ValueError:
            # Unknown leading byte — protocol drift, log and reconnect
            ...
        if packet_type in (lp.PacketType.NEW_INVENTORY,
                           lp.PacketType.COLLECTED_INDICES,
                           lp.PacketType.RECEIVED_PICKUPS,
                           lp.PacketType.GAME_STATE,
                           lp.PacketType.LOG_MESSAGE,
                           lp.PacketType.MALFORMED):
            # Push: read [success][len_24][payload]
            resp = await self._read_response_body()
            if self.on_push is not None:
                await self.on_push(packet_type, resp)
            continue
        # No leading type byte? Then `first` was already the success byte
        # of a Lua-exec reply. Read the rest.
        rest = await self._reader.readexactly(3)
        ...  # parse as Lua-exec reply, fulfill pending future
```

**This logic is delicate**. Whichever shape upstream actually uses,
mirror it exactly. If you can't determine the shape, **stop and ask** —
guessing wrong here corrupts every Lua call.

Update [tests/test_lua_executor.py](../apworld/dread_archipelago/tests/test_lua_executor.py)'s
`FakeSwitch` to emit push frames in the format you confirm, and add a
test that asserts pushed frames invoke `on_push` with the right
`PacketType`.

### A2. Pickup-index ↔ AP-location-id mapping

The `PACKET_COLLECTED_INDICES` payload from the Switch is a bitfield (or
JSON, depending on what the bootstrap Lua chose — check
[vendor/open-dread-rando/src/open_dread_rando/lua_libraries/](../vendor/open-dread-rando/src/open_dread_rando/lua_libraries/)
for the exact emit format). Bit N being set means pickup_index N has
been collected.

The pickup_index assignment is **the order of pickups in the patcher
input JSON**. Our patcher template is
[vendor/open-dread-rando/tests/test_files/patcher_files/starter_preset_patcher.json](../vendor/open-dread-rando/tests/test_files/patcher_files/starter_preset_patcher.json)
(149 pickups). Our adapter ([scripts/build_patcher_json.py](../scripts/build_patcher_json.py))
preserves the order. So pickup_index 0..148 maps deterministically to
the 149 `(scenario, actor)` tuples in template order.

**Two options for storing the map.** Pick whichever is simpler to
implement and test.

**Option (a) — pre-compute and ship as data**:
- Add a `scripts/extract_pickup_index_map.py` that reads the template
  and emits `apworld/dread_archipelago/data/pickup_index_map.json`:
  ```json
  [
    {"pickup_index": 0, "scenario": "s010_cave", "actor": "ItemSphere_ChargeBeam"},
    {"pickup_index": 1, "scenario": "s010_cave", "actor": "Item_MissileTank011"},
    ...
  ]
  ```
- Add a corresponding `pickup_index` field to `locations.json` (append
  the field; existing AP IDs unchanged).
- Client loads the map at startup via `DataPackage`.

**Option (b) — embed in locations.json directly**:
- Just add `pickup_index` to each entry in `locations.json` (the
  extraction script already builds this file; extend it).
- No separate data file.

Option (b) is fewer moving parts. Recommended.

Either way: write a `test_pickup_index_map.py` that asserts every
actor-type location in locations.json has a pickup_index, and that the
indices form a contiguous 0..136 range for actor pickups (the 12
non-actor pickups — EMMI/corex/corpius/cutscene — get indices
137..148 based on their position in the template). Confirm the
ordering matches the template by spot-checking 3 entries.

### A3. Parse COLLECTED_INDICES → CheckLocations

The shape of the COLLECTED_INDICES payload is determined by the
bootstrap Lua's `RL.GetCollectedIndicesAndSend()` implementation.
Look at it in
[vendor/open-dread-rando-exlaunch/](../vendor/open-dread-rando-exlaunch/)
or pull the latest bootstrap files from upstream. Most likely formats:

- A binary bitfield (149 bits → 19 bytes, little-endian)
- JSON: `{"indices": [0, 5, 12, ...]}` listing set indices
- A comma-separated string

**Once you know the format, parse it.** Then:

```python
async def _on_switch_push(self, packet_type: PacketType, resp: Response) -> None:
    if packet_type == PacketType.COLLECTED_INDICES:
        indices = parse_collected_indices(resp.payload)
        new_loc_ids = []
        for idx in indices:
            loc = self.datapackage.pickup_index_to_location(idx)
            if loc is None:
                continue
            if loc.ap_id in self.state.all_collected_ids():
                continue  # dedup against our own mirror
            self.state.mark_collected(CollectedLocationEvent(location_id=loc.ap_id))
            new_loc_ids.append(loc.ap_id)
        if new_loc_ids:
            await self.send_msgs([{"cmd": "LocationChecks",
                                   "locations": new_loc_ids}])
    elif packet_type == PacketType.GAME_STATE:
        # parse and stash in self.state.game_state — Init.bBeatenSinceLastReboot
        # is already covered by the explicit poll, but other state lands here
        ...
    elif packet_type == PacketType.NEW_INVENTORY:
        # parse and stash in self.state.inventory — diagnostic only
        ...
    # PACKET_RECEIVED_PICKUPS, LOG_MESSAGE, MALFORMED: log + skip for v0.1
```

**Dedup is critical.** On every Switch reconnect, the bootstrap Lua's
poll dumps the FULL collected-indices set, not a delta. Without dedup
we'd send AP a duplicate `LocationChecks` containing every previously-
collected location, every 2 seconds. AP handles duplicates gracefully
but it's noisy and could hide real bugs. Use `BridgeState.mark_collected`
which already dedupes (returns False on already-seen IDs).

### A4. Update `_dispatch_collected_response` and `_poll_once`

Now that pushes do the heavy lifting, `_dispatch_collected_response`
becomes truly vestigial. Either delete it (and call sites) or document
it as "intentionally empty — see `_on_switch_push` for the real path."
Either is fine; deletion is cleaner.

`_poll_once` still fires `RL.GetCollectedIndicesAndSend()` to TRIGGER the
push. Leave that alone. Same for `Init.bBeatenSinceLastReboot` — that
returns a direct value via the Lua-exec reply, not a push, so it stays
in the explicit polling path.

### A5. Gate A tests

Required new tests:

```
apworld/dread_archipelago/tests/test_lua_executor.py
  - test_push_frame_routes_to_on_push
  - test_lua_exec_reply_still_routes_to_pending_future
  - test_interleaved_push_and_reply (push arrives between request and reply)

apworld/dread_archipelago/tests/test_pickup_index_map.py
  - test_every_actor_location_has_pickup_index
  - test_pickup_indices_are_unique
  - test_pickup_indices_match_template_order  (spot-check 3 known mappings)

apworld/dread_archipelago/tests/test_context_e2e.py
  - test_collected_indices_push_emits_location_checks
  - test_duplicate_indices_dont_double_send
  - test_unknown_index_is_skipped (out-of-range index, e.g. from a
    bootstrap Lua that emits a sentinel — don't crash)
```

The `test_context_e2e.py` tests can use a fake AP-server mock:

```python
class FakeApServer:
    def __init__(self):
        self.sent = []
    async def send_msgs(self, msgs):
        self.sent.extend(msgs)

async def test_collected_indices_push_emits_location_checks():
    ctx = DreadContext(...)
    ctx.send_msgs = FakeApServer().send_msgs  # monkey-patch
    resp = Response(success=True,
                    payload=b'{"indices":[0,1,5]}')  # or whatever format
    await ctx._on_switch_push(PacketType.COLLECTED_INDICES, resp)
    assert ctx.send_msgs.sent == [{"cmd": "LocationChecks",
                                    "locations": [<id_for_0>, <id_for_1>, <id_for_5>]}]
```

## Gate B — seed → patcher translation

### B1. `scripts/seed_to_patcher_overrides.py`

**Input**: a generated AP seed (typically a zip — `AP_<seed_id>.zip` from
`scripts/ap_generate.py`). Inside is per-slot data including the
location → item placements for each player.

**Output**: a JSON file matching the contract in
[scripts/build_patcher_json.py](../scripts/build_patcher_json.py)'s
docstring:

```json
{
  "layout_uuid": "<derived>",
  "configuration_identifier": "AP-<seed>-<slot>",
  "starting_location": {"scenario": "s010_cave", "actor": "StartPoint0"},
  "starting_items": {...},
  "pickup_resources": {
    "s010_cave/ItemSphere_ChargeBeam": [[
      {"item_id": "<patcher_item_id>", "quantity": <n>}
    ]],
    ...
  },
  "pickup_captions": {
    "s010_cave/Item_MissileTank011": "Sent Missile Tank to Player 2"
  }
}
```

**Translation rules:**

- For each placement in the Dread slot, look up:
  - `location_id` → `(scenario, actor)` via `apworld/dread_archipelago/data/locations.json`
  - `item_id` → `(patcher_item_id, quantity)` via
    `apworld/dread_archipelago/data/items.json`
  - If the item is going to ANOTHER player (cross-slot), the caption
    should reflect that ("Sent X to Player N"). For own-slot items,
    use the natural vanilla caption.
- `starting_location` comes from `slot_data["starting_area"]` (0 =
  Artaria; others not yet supported per Options.py).
- `starting_items`: from precollected items in the seed. Translate via
  the same items.json lookup.
- `layout_uuid`: derive from seed_id + slot_name (stable per slot,
  unique per seed). Format must match the UUID regex in
  `vendor/open-dread-rando/src/open_dread_rando/files/schema.json` line 16.
- `configuration_identifier`: human-readable, e.g.
  `f"AP-{seed_id[:8]}-{slot_name}"`.

**For non-actor pickups** (boss/EMMI/cutscene — pickup_type != "actor"
in locations.json): they don't go in `pickup_resources` because the
patcher distinguishes them by `pickup_lua_callback` not `pickup_actor`.
The template already has them; our adapter currently doesn't override
them. For v0.1 leave them at their vanilla resources — they're locked
to the Dread player's pool (per [test_data_tables.py] those 12 are not
in `compiled_rules.json` either, intentionally).

### B2. End-to-end pipeline

Document the pipeline in
[docs/e2e-runbook.md](../docs/e2e-runbook.md):

```pwsh
# 1. Generate the AP seed (Dread + Clique)
python scripts/ap_generate.py apworld/dread_archipelago/tests/seeds/dread_clique.yaml

# 2. Translate the Dread slot to patcher overrides
python scripts/seed_to_patcher_overrides.py \
    apworld/dread_archipelago/tests/seeds/out/AP_<id>.zip \
    --slot Samus \
    --output build/dread_overrides.json

# 3. Build the patcher input JSON
python scripts/build_patcher_json.py \
    --template vendor/open-dread-rando/tests/test_files/patcher_files/starter_preset_patcher.json \
    --ap-overrides build/dread_overrides.json \
    --output build/dread_patcher_input.json

# 4. Run the patcher (requires an extracted Dread 2.1.0 RomFS)
python -m open_dread_rando \
    --input-path C:/path/to/dread/romfs \
    --output-path build/dread_seed \
    --input-json build/dread_patcher_input.json

# 5. Deploy
#    Copy build/dread_seed/* to your SD card's
#    /atmosphere/contents/010093801237C000/romfs/

# 6. Run the Dread Client (PC side, talks to AP and to the Switch)
python -m worlds.dread_archipelago.client.main \
    --connect localhost:38281 --name Samus --switch-host 192.168.1.42

# 7. Boot Dread on the Switch, play, watch items flow
```

### B3. 2-slot test fixture

[apworld/dread_archipelago/tests/seeds/dread_clique.yaml](../apworld/dread_archipelago/tests/seeds/dread_clique.yaml):

```yaml
# Dread + Clique 2-slot multiworld smoke test.
# Clique is AP's built-in "click the button" filler — simplest possible
# second slot, exercises the cross-slot item flow without a real game.

name: Samus
game: Metroid Dread
description: Dread + Clique 2-slot E2E smoke

Metroid Dread:
  accessibility: items
  starting_area: artaria
  include_boss_pickups: true

---
name: ButtonPusher
game: Clique
description: Clique slot for cross-slot item exchange

Clique:
  hard_mode: false
```

Add a regression test that generates this seed and asserts:
- Both slots' placements are valid (the AP generator does this; we just
  invoke it and check exit code)
- Dread's `pickup_resources` after seed→patcher conversion has at least
  one cross-slot item (something going to ButtonPusher)
- ButtonPusher's Clique-side placements include at least one Dread
  item

### B4. Real-hardware E2E (documented, not executed)

You probably don't have a Switch. That's fine — write the runbook
exhaustively enough that someone with the Switch can execute it without
asking questions. Include:

- Prerequisites: Atmosphere CFW version, Dread 2.1.0 dump, prod.keys
  location, gateway IP discovery
- Expected output at each step (e.g. "scripts/ap_generate.py should
  print `wrote .../AP_<id>.zip`")
- Common failure modes: socket connection refused → exlaunch sysmodule
  not installed; "no Dread mapping for AP item id X" → datapackage
  out of sync between seed and client; etc.
- A "smoke checklist" the user runs through: collect first Missile Tank
  → confirm Clique shows "received Missile Tank from Samus"; have Clique
  push their button → confirm Dread popup shows the AP item.

## Pitfalls

1. **Push-frame format uncertainty.** Don't guess. Read
   `dread_executor.py`'s read loop. If it's still unclear, run the
   `phase1_validate.py` script against a Ryujinx instance and packet-dump
   the responses. Wrong demux corrupts every Lua call silently.

2. **Closure-capture trap revisited.** `_on_switch_push` will likely
   close over `self` and various callable bindings. Use default-arg
   capture if you build any lambdas inside loops (same pattern as
   Rules.py's `compile_to_lambda`).

3. **CheckLocations idempotence.** AP server accepts duplicates but
   reasonable hygiene says don't send them. Use the existing
   `BridgeState.mark_collected` which returns False on dupes.

4. **Out-of-range pickup indices.** A bootstrap Lua might emit indices
   for events too (event nodes have indices in Randovania's model
   though not necessarily in exlaunch's). Defensive default: if the
   index doesn't resolve via `datapackage.pickup_index_to_location`,
   log and skip — don't crash.

5. **Seed zip layout.** AP's seed zip format changed in recent
   releases. Test the converter against an actual zip from
   `scripts/ap_generate.py`, not a hand-written fixture. The smoke
   test already produces one at
   `apworld/dread_archipelago/tests/seeds/out/AP_<id>.zip`.

6. **Pickup_index for event locations.** Our locations.json now
   includes 184 event locations (M2 Gate A). Those are AP-synthetic and
   have no Switch-side pickup_index — they're never collected via the
   COLLECTED_INDICES push. Filter them out when building the
   pickup_index ↔ location map. Only `pickup_type == "actor"` (and
   maybe `emmi/corex/corpius/cutscene`) entries get an index.

7. **Non-actor pickup indices.** EMMI/corex/corpius/cutscene pickups
   have entries in the template and therefore have pickup_indices, but
   the Switch reports their "collected" state via a different
   mechanism (event flags, not the COLLECTED_INDICES bitfield). For
   v0.1 you can either: (a) include them in the map but accept their
   bit never being set; (b) handle them via a separate push parser.
   Pick (a) — keeps the map simple, doesn't lose any AP checks since
   those locations stay accessible to AP via their inclusion in
   locations.json.

8. **Don't change `data/items.json` or `data/locations.json` AP IDs.**
   Append-only — same rule as Logic M2. If you add `pickup_index` as a
   new field on existing entries, that's fine (new fields don't change
   the ID); reordering entries is NOT fine.

9. **Datapackage cache.** `DataPackage` is loaded once at client
   startup. If you add the pickup_index map to it, make sure it loads
   even when the apworld is zipped (importlib.resources path — same
   issue called out in the M1 retro).

## Files you'll touch

### Modify

| Path | Why |
|---|---|
| `apworld/dread_archipelago/client/lua_executor.py` | Type-byte push demux in `_read_loop` |
| `apworld/dread_archipelago/client/lua_packets.py` | Maybe: add `parse_push_header` helper |
| `apworld/dread_archipelago/client/context.py` | `_on_switch_push` real parsing; remove or no-op `_dispatch_collected_response` |
| `apworld/dread_archipelago/client/datapackage.py` | `pickup_index_to_location` lookup |
| `apworld/dread_archipelago/data/locations.json` | Append `pickup_index` field to actor/EMMI/corex/corpius/cutscene entries |
| `scripts/extract_dread_data.py` | Emit `pickup_index` per location (preserve template order) |
| `apworld/dread_archipelago/tests/test_lua_executor.py` | New push-demux tests |
| `apworld/dread_archipelago/tests/test_data_tables.py` | Bump assertions to cover new `pickup_index` field |
| `docs/randovania-logic-port-notes.md` | Append a wire-wiring section |
| `CLAUDE.md` | Refresh Status |

### Create

| Path | Why |
|---|---|
| `scripts/seed_to_patcher_overrides.py` | Gate B core |
| `apworld/dread_archipelago/tests/seeds/dread_clique.yaml` | Gate B fixture |
| `apworld/dread_archipelago/tests/test_pickup_index_map.py` | Map invariants |
| `apworld/dread_archipelago/tests/test_context_e2e.py` | Fake-AP collected-indices test |
| `apworld/dread_archipelago/tests/test_seed_to_patcher.py` | Gate B unit tests |
| `docs/e2e-runbook.md` | The user-facing E2E playbook |

## Verification

```pwsh
# 1. All pre-existing tests still pass
python -m pytest apworld/dread_archipelago/tests/ scripts/tests/ -q

# 2. Push-demux works against the fake server
python -m pytest apworld/dread_archipelago/tests/test_lua_executor.py -v

# 3. Pickup-index map is consistent
python -m pytest apworld/dread_archipelago/tests/test_pickup_index_map.py -v

# 4. Mocked AP receives CheckLocations on collected push
python -m pytest apworld/dread_archipelago/tests/test_context_e2e.py -v

# 5. Generation + seed→patcher pipeline produces a valid romfs JSON
python scripts/ap_generate.py apworld/dread_archipelago/tests/seeds/dread_clique.yaml
python scripts/seed_to_patcher_overrides.py \
    apworld/dread_archipelago/tests/seeds/out/AP_<id>.zip \
    --slot Samus --output build/test_overrides.json
python scripts/build_patcher_json.py \
    --template vendor/open-dread-rando/tests/test_files/patcher_files/starter_preset_patcher.json \
    --ap-overrides build/test_overrides.json \
    --output build/test_input.json
# The output JSON should validate against the patcher's schema.json
python -c "import json, jsonschema; jsonschema.validate(
    json.load(open('build/test_input.json')),
    json.load(open('vendor/open-dread-rando/src/open_dread_rando/files/schema.json')))"
```

## Doc deliverable

Append a "## Wire wiring retrospective" section to
[docs/randovania-logic-port-notes.md](randovania-logic-port-notes.md)
(or split off into a new `wire-wiring-notes.md` — your call). Document:
- The push-frame format you discovered (this is the answer to a
  question nobody has written down yet)
- Whether you went with Option (a) or (b) for the pickup_index map
- What the smoke pipeline run produced
- What v0.2 / Kivy / cross-region M2 Gate B needs to budget for

## Success criteria checklist

- [ ] All 144 pre-existing tests pass
- [ ] Push frames demux'd by type byte; pushes route to `on_push`
- [ ] `_on_switch_push` parses COLLECTED_INDICES → CheckLocations
- [ ] Pickup_index ↔ AP-location-id map exists (either embedded in
      locations.json or as a side-channel data file)
- [ ] Tests assert duplicate indices don't double-send
- [ ] Tests assert out-of-range indices are skipped
- [ ] `_dispatch_collected_response` removed or documented as no-op
- [ ] (Gate B) `scripts/seed_to_patcher_overrides.py` exists and
      converts a real seed
- [ ] (Gate B) `dread_clique.yaml` test fixture works through the full
      pipeline
- [ ] (Gate B) `docs/e2e-runbook.md` covers steps 1–7 with expected
      outputs and common failure modes
- [ ] `__version__` bumped to `0.0.1-phase4-logic-m2-wire`
- [ ] Wire-wiring retro appended to docs
