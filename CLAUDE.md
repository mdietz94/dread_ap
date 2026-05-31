# CLAUDE.md — context for the next session

Fast-load brief for picking up **dread_ap** cold. This is the Metroid Dread
sibling of [smo_archipelago](../smo_archipelago/CLAUDE.md). The shape mirrors
that project's two-tier architecture — read its CLAUDE.md for the parent
pattern; this file only documents what's different for Dread.

## What we're building

A real Archipelago client for **Metroid Dread 2.1.0 on a modded Switch
(Atmosphere CFW)**, with Ryujinx as the dev iteration target. Builds on
Randovania's existing Dread infrastructure:

- [open-dread-rando-exlaunch](https://github.com/randovania/open-dread-rando-exlaunch) — the in-game sysmodule that exposes a Lua-eval TCP socket on port 6969
- [open-dread-rando](https://github.com/randovania/open-dread-rando) — the RomFS patcher (soft-forked here to consume AP slot_data)
- [mercury-engine-data-structures](https://github.com/randovania/mercury-engine-data-structures) — file format library (pip dep)

**The headline architectural fact**: unlike smo_archipelago, we write zero
Switch-side C++ code and do zero Ghidra work. Randovania did it for us. The
entire problem reduces to (a) a Python translator from Archipelago to the
existing Lua-eval protocol, and (b) an adapter that makes
open-dread-rando consume an AP-shaped seed.

## ⚠️ CRITICAL: Never commit Nintendo IP

Same rule as smo_archipelago. The Dread-specific list:

- `*.nsp`, `*.nca`, `*.xci`, `*.nso`, `*.npdm` — raw Switch dumps
- `prod.keys`, `dev.keys`, `title.keys` — console keys
- `*.bmsad`, `*.bmsld`, `*.bmscd`, `*.bmsbk`, `*.brfld`, `*.brsa`, `*.bmtre`, `*.bmsem`, `*.bmtun` — Mercury Engine scenario / actor / behavior data
- `*.bfres`, `*.bcskla`, `*.bwav`, `*.szs`, `*.byml`, `*.msbt` — model / animation / audio / config / text
- `*.lc` — Lua bytecode (Mercury Engine compiles its Lua to bytecode)
- `.romfs-cache/`, `romfs-extracted/`, `out/` — extraction caches and per-seed patcher output

All gitignored. Treat any pasted excerpt from these files in commit messages
or doc comments as the same exposure as the file itself.

**Safe pattern**: functional identifiers (scenario names like `s010_cave`,
actor names from Randovania's published JSON) are OK. Bulk-extracted Nintendo
strings are not.

## Architecture (three tiers)

```
[ Switch / Dread 2.1.0 ]  <--TCP/binary LAN-->  [ PC Client (Python) ]  <--ws-->  [ AP server ]
   exlaunch sysmodule                              DreadContext(CommonContext)     archipelago.gg
   (UPSTREAM — no fork)                            Kivy GUI
   - bootstraps RL.* Lua namespace                 LuaProtocol on port 6969
   - opens TCP :6969                               Forked apworld machinery
   - runs arbitrary Lua via PACKET_REMOTE_LUA_EXEC
   romfs/
   (our forked open-dread-rando output)
```

## Wire protocol (exlaunch, port 6969)

Lifted verbatim from [randovania/game_connection/executor/dread_executor.py](https://github.com/randovania/randovania/blob/main/randovania/game_connection/executor/dread_executor.py)
and [randovania/game_connection/connector/dread_remote_connector.py](https://github.com/randovania/randovania/blob/main/randovania/game_connection/connector/dread_remote_connector.py).
Both are GPL-3.0 (this whole project is GPL-3.0 as a result — see [LICENSE](LICENSE)).

**Packet types** (the upstream `IntEnum(b"N")` trick evaluates as `int("N")==N`,
so on the wire these are raw `0x01`..`0x09`, not ASCII):

| Code | Name | Direction |
|---|---|---|
| 0x01 | PACKET_HANDSHAKE | both |
| 0x02 | PACKET_LOG_MESSAGE | Switch→PC |
| 0x03 | PACKET_REMOTE_LUA_EXEC | both |
| 0x04 | PACKET_KEEP_ALIVE | both |
| 0x05 | PACKET_NEW_INVENTORY | Switch→PC |
| 0x06 | PACKET_COLLECTED_INDICES | Switch→PC |
| 0x07 | PACKET_RECEIVED_PICKUPS | Switch→PC |
| 0x08 | PACKET_GAME_STATE | Switch→PC |
| 0x09 | PACKET_MALFORMED | Switch→PC |

Request frames (PC → Switch):

**Lua-exec request**: `[0x03][4-byte LE length][utf-8 source]`
**Handshake request**: `[0x01][1 interest byte]` (no length; multiworld interest = `0x02`)
**Keep-alive request**: `[0x04]` (one byte, no reply)

Response frames (Switch → PC) — every frame begins with a 1-byte
PacketType, then the layout varies by type:

**HANDSHAKE reply** `[0x01][req_num:1]` (2 bytes; no body)
**REMOTE_LUA_EXEC reply** `[0x03][req_num:1][success:1][len:3 LE u24][payload]`
**Push frames** (0x02/0x05/0x06/0x07/0x08) `[type:1][len:4 LE u32][payload]`
**MALFORMED** `[0x09][failing_type:1][rcv:4 LE u32][should:4 LE u32]`

This is the AUTHORITATIVE wire format, confirmed from
[randovania/open-dread-rando-exlaunch/source/program/{remote_api.cpp,main.cpp}](https://github.com/randovania/open-dread-rando-exlaunch/tree/main/source/program)
(GPL-2.0, not redistributed here — clone separately if you need to read it).
Prior versions of this document described the response shape as
`[success][len_24][payload]` for all frames — that was wrong (see
[docs/wire-wiring-notes.md](docs/wire-wiring-notes.md)).

**Connect sequence**:
1. Open TCP to switch:6969
2. Send `PACKET_HANDSHAKE` with multiworld interest
3. Read response
4. Send Lua-exec: `return string.format('%d,%d,%s,%s,%s', RL.Version, RL.BufferSize, tostring(RL.Bootstrap), Init.sLayoutUUID, GameVersion)`
5. Read response, split on `,` → `(api_version, buffer_size, bootstrap, layout_uuid, game_version)`
6. **REQUIRED, not optional** — send the `RL.*` bootstrap Lua (vendored under
   `client/lua/`, assembled by `client/bootstrap.py`, chunked to `buffer_size`).
   The exlaunch ROM ships only stubs; without this the API probe at step 4 even
   fails (`RL.Version` is nil). `connect_switch` does this before polling.
   Earlier docs called this optional — WRONG; randovania bootstraps every connect.
7. Start the 2s poll loop, which calls `RL.GetInventoryAndSend` /
   `GetCollectedIndicesAndSend` / `GetReceivedPickupsAndSend` directly each tick
   (we don't rely on `RL.UpdateRDVClient` self-scheduling) + reads the goal flag.

**RL namespace** (the Lua API exposed by the bootstrap files):

| Lua call | Purpose |
|---|---|
| `RL.GetInventoryAndSend()` | Reads `RandomizerPowerup.GetItemAmount()` per tracked item; replies with `PACKET_NEW_INVENTORY` |
| `RL.GetCollectedIndicesAndSend()` | Reads Blackboard pickup-collected bits; replies with `PACKET_COLLECTED_INDICES` |
| `RL.GetReceivedPickupsAndSend()` | Reads `Blackboard.GetProp(playerSection, "ReceivedPickups")`; replies with `PACKET_RECEIVED_PICKUPS` |
| `RL.ReceivePickup(message, cls, progression_string, num_pickups, inventory_index)` | Grants an item live by calling the game's native `OnPickedUp` callback |
| `RL.UpdateRDVClient(arg)` | Periodic 2s poller — fires the three queries above + game state |
| `Game.GetCurrentGameModeID()` | Game state read (title vs in-game vs paused) |
| `Init.bBeatenSinceLastReboot` | Goal detection — flips true after the final cutscene completes |

## Project layout (planned)

```
C:\Users\maxwe\Documents\dread_ap\
  README.md
  CLAUDE.md                  ← this file
  LICENSE                    GPL-3.0 (combined work — GPL-3.0 patcher + GPL-2.0 sysmodule patches)
  PLAN.md                    Copy of the original implementation plan
  .gitignore                 Nintendo IP rules
  scripts/
    phase1_validate.py       Phase 1 wire-up test — TCP client for exlaunch :6969
    ap_generate.py           AP Generate wrapper (to be added Phase 3)
    ap_server.py             AP MultiServer wrapper (to be added Phase 3)
    install_apworld.py       Zip apworld into Archipelago's custom_worlds/
  apworld/dread/  (to be added Phase 4)
    __init__.py              World class + DreadSettings + "Dread Client" Component
    data/
      items.json             ~30 entries (Missile Expansion, Energy Tank, Suit upgrades, beams)
      locations.json         ~100 pickup nodes derived from randovania/games/dread JSON
      regions.json           6 areas: Artaria, Cataris, Dairon, Burenia, Ghavoran, Elun, etc.
      categories.json
      meta.json
    Data.py, Game.py, ...    World boilerplate
    hooks/                   Generation hook surfaces
    client/                  Python client (lifted from smo_archipelago/apworld/smo_archipelago/client/)
      context.py             DreadContext(CommonContext) + DreadClientCommandProcessor
      gui.py                 DreadManager(GameManager) — Kivy UI
      lua_executor.py        TCP client for exlaunch :6969 (replaces SMO's switch_server.py)
      lua_packets.py         Frame encode/decode (replaces SMO's protocol.py)
      state.py               Thread-safe state mirror — same pattern as SMO
      datapackage.py         AP id↔name + classifier
      scout_cache.py         LocationScouts pre-fetch (lifted as-is)
      discovery.py           UDP discovery responder (lifted as-is)
      commands.py, display.py
    tests/
  vendor/                    Upstream Randovania repos (soft fork)
    open-dread-rando/        Forked patcher
    open-dread-rando-exlaunch/  Reference copy of the sysmodule build
    CHANGES.md               Per-vendor diff notes for upstream PRs
  docs/
    architecture.md          Two-tier diagram, threading
    wire-protocol.md         Lua-eval framing + RL namespace reference
    install-switch.md        Atmosphere CFW + exlaunch sysmodule install
    first-time-setup.md      End-user walkthrough
```

## Decisions already made (and why)

| Decision | Why |
|---|---|
| **No subsdk module of our own** | Randovania already shipped one (exlaunch). Writing a parallel one duplicates work and forks the community. Only revisit if exlaunch lacks a hook we genuinely need. |
| **No Ghidra work** | Implied by the above. If we ever reach for it the plan needs revisiting — that signals exlaunch is insufficient for the use case. |
| **Soft fork w/ credit, not pip-install dependency** | Upstream's release cadence is monthly, AP-relevant patches will likely lag. Vendored fork lets us iterate, with a discipline of filing genuine bugs upstream. |
| **Target Dread 2.1.0 (not 1.0.0)** | Newest. Already dumped. Randovania has actively shifted here. |
| **PC client, not direct Switch→AP** | Same reasoning as smo_archipelago — websocket+deflate+TLS+reconnect on Switch is months of work; PC bridge solves it via `CommonContext`. |
| **No deathlink/hints/traps for v0.1** | MVP discipline. Land item flow + goal first. |

## Status

Phase 1 deliverable: [scripts/phase1_validate.py](scripts/phase1_validate.py).
Run with `python scripts/phase1_validate.py <switch-ip>` after installing
upstream exlaunch on the Switch. Exit status 0 means the wire is up and
the rest of the plan can proceed. Non-zero status means stop and triage.

Logic: M2 plumbing Gate A + Gate B shipped. All 137 actor pickups have
non-trivial rules; 184 events are real AP items locked to synthetic
event locations; the lambda compiler's event branch consults
`state.has("Event: <name>", player)`; completion_condition reads
`victory_condition` from compiled output. Gate B: cross-region access is
modeled via a global-reachability `region_access` map (item-only — see the
notes retro for why) that gates `Menu→region`, so boss/EMMI locations are no
longer trivially reachable; Trick Level is a user option backed by three
pre-baked `compiled_rules{,_l2,_l3}.json`. The compiler is now deterministic
(stable disjunct-cap tie-break). Generation smoke produces a solvable seed
under `accessibility: minimal` across trick levels and DNA configs.
Negation handling was made faithful (config-`misc` flags resolved against our
config; temporal negated item/event → drop-the-transient = impossible, relying
on the stable post-event path; self-referential event rules stripped). Starting
items (Slide, Pulse Radar, missile capacity) are now `push_precollected` into AP
logic. `accessibility: items`/`full` NOW WORK (verified 8/8 across seeds at every trick
level). The compiler uses a forward resolver (`compile_forward` in
scripts/extract_dread_rules.py) that INLINES events into ITEM-ONLY rules — each
event atom is replaced by that event's item-only reach cost, computed in
dependency-sphere order. This removes events from the dependency graph, so the
old item↔event bootstrap cycle (which AP's monotonic `fulfills_accessibility`
sweep couldn't unwind) is gone, and the rules bootstrap like ordinary AP item
logic. Events are therefore NO LONGER AP items/locations (World/Regions/Rules
skip them; data tables keep them for ID stability). Two more pieces were
required: a classification fix (Missile Tank was `filler`, Missile+ Tank /
Flash Shift Upgrade / Speed Booster Upgrade were `useful` — all logic-required,
now progression(_skip_balancing)). region_access is a star (cost
inlined per-rule). Smoke seed is now `accessibility: items`. See the notes retro
for the full diagnosis.

UPDATE (post-logic-fixes): `World.EXTRA_STARTING_ITEMS` is now EMPTY — Charge
Beam used to be force-started to clear a fill bottleneck, but once Missile Tank
became advancement the early reachable set opened up and Charge Beam places as a
normal findable item (verified 146 generations: solo+multiworld × all trick
levels × minimal/items/full, 0 fill failures).

UPDATE (escape-rule fill cascade — fixed): the reverse-reachability escape pass
(commit `7c5a451`) AND-ed "items needed to leave each pickup node" into per-
location entry rules to plug AP's missing exit-side accessibility. The first cut
folded raw item atoms in, so e.g. ~80/137 actor pickups picked up a hard
`Morph Ball` requirement. Morph Ball is a single-copy progression item — the
moment AP places it at any of those 80 spots, `reach(P)` needs Morph Ball and
Morph Ball is *at* P, so `sweep_from_pool` can never collect it. `fill_restrictive`'s
swap dance can't unwind the cascade because every viable spot still wants the
same item. Result: generation failed across all accessibility levels and trick
tiers; the original "100/100 verified" claim did not reproduce.

Fix: `scripts/extract_dread_rules.py` now strips fill-fragile atoms (single-pool-
copy, non-precollected items — Morph Ball, Charge Beam, every unique beam/suit,
etc.) from each escape AST before AND-ing it into the entry rule, via the new
`_strip_fill_fragile_items` pass. Multi-copy items (Missile Tank, Energy Tank,
Power Bomb Tank, Missile+ Tank, Flash Shift Upgrade, Speed Booster Upgrade) and
precollected starters (Slide, Pulse Radar) stay — `state.has` resolves them from
other instances / from start state, so they don't cascade. Tightening count drops
from 106 → 24 at L1 (also smaller at L2/L3). Generation now passes 0 fill
failures across 99+ seeds spanning trick `beginner/intermediate/advanced` ×
accessibility `minimal/items/full`, plus 50 seeds of the user-reported config
and a 16-seed Dread+Clique multiworld smoke. Residual gameplay-softlock risk:
a one-way-entry room whose escape needs `Morph Ball` no longer protects against
AP placing a different item there. Same trade-off the original commit already
accepted for boss/EMMI/cutscene/corex pickups (skipped for the same circular
reason). See `_strip_fill_fragile_items` docstring for the full rationale.

Also: `objective.hints` in the
patcher output is now regenerated to a neutral count line — the starter
template's per-guardian hints ("DNA 1 guarded by Corpius") are false under AP
placement. Of the precollected starters, only **Slide** (191 rule atoms) and
**Missile Tank** are real logic gates; **Pulse Radar is logic-INERT** (0 rule
atoms — Randovania never gates traversal on it, despite the old "EMMI routes
need it" folklore), so it's now an opt-out `start_with_pulse_radar` option
(default on): off ⇒ not precollected, dropped from patcher `starting_items`,
shuffled into the findable pool as `useful`; solvability is identical. The only
starter baked into compilation is the 15 starting missile capacity (the ammo
`sum` thresholds assume it).

Wire wiring: Gate A + B shipped. Every Switch→PC frame is now
demuxed by leading type byte; the wire format documented previously
in this file (and in phase1_validate.py) was WRONG — actual format
discovered from [randovania/open-dread-rando-exlaunch/source/program/](https://github.com/randovania/open-dread-rando-exlaunch/tree/main/source/program)
and now used throughout. The Switch→AP path emits
`LocationChecks` from `PACKET_COLLECTED_INDICES` pushes
(`locations:`-prefixed bitfield → AP location_ids via the new
`pickup_index` field on `locations.json`). The PC→Switch path was
already wired. `DreadWorld.generate_output` writes a per-slot
placements JSON; `scripts/seed_to_patcher_overrides.py` converts
that to the override shape `scripts/build_patcher_json.py` consumes.
2-slot Dread+Clique fixture lives at
`apworld/dread/tests/seeds/dread_clique.yaml`. End-to-end
runbook at [docs/e2e-runbook.md](docs/e2e-runbook.md); wire-wiring
retrospective at [docs/wire-wiring-notes.md](docs/wire-wiring-notes.md).

Bootstrap + RL.ReceivePickup delivery port (resolves risk #1 from source — see
the delivery-protocol reading below). The earlier "idempotent-delivery
groundwork behind a flag" was built on a WRONG premise and has been replaced.
Reading upstream (`randovania/games/dread/assets/lua/bootstrap_part_*.lua`,
open-dread-rando `randomizer_powerup.lua`, exlaunch `main.cpp`) established:
(1) there are TWO counters — `InventoryIndex` (bumped by EVERY `OnPickedUp`,
local or remote) and `ReceivedPickups` (bumped ONLY by `RL.ConfirmPickup`);
(2) our old `OnPickedUp`-direct delivery moved `InventoryIndex`, never
`ReceivedPickups`, so gating on `ReceivedPickups` was a no-op — the flag never
worked; (3) `RL.ReceivePickup` already provides idempotence (it grants only when
`receivedPickupIndex==ReceivedPickups() and inventoryIndex==InventoryIndex()`,
guards a single `PendingPickup`, defers through cutscenes via
`Scenario.IsUserInteractionEnabled`, and bumps the counter on confirm); and (4)
**the exlaunch ROM ships only RL.* stubs — the real functions are Lua randovania
sends at every connect.** Our client never sent it, so it could not have worked
against a real ROM (the API probe alone reads `RL.Version`, nil pre-bootstrap).
So now: `client/lua/bootstrap_part_*.lua` + `bootstrap_locations.lua` are
vendored verbatim (randovania `68a2b52`, see `client/lua/NOTICE.md`);
`client/bootstrap.py` reproduces `get_bootstrapper_for` from OUR data tables and
`connect_switch` sends the chunked bootstrap before polling;
`protocol.build_receive_pickup_lua` emits `RL.ReceivePickup(...)`;
`DreadContext` tracks both game counters (`RECEIVED_PICKUPS` + `NEW_INVENTORY`
`index`) and `_attempt_delivery` sends the pickup at `received_pickup_index ==
ReceivedPickups`, tagged with the live `InventoryIndex`, one per poll tick.
Delivery is idempotent + cutscene-safe BY CONSTRUCTION; no flag. The validation
harness `apworld/dread/tests/fakeswitch.py` (stateful fake modelling the two
counters + `RL.ReceivePickup` + cutscene deferral) drives the REAL `DreadContext`
over a loopback socket in `test_session_e2e.py` (connect→bootstrap→collect→
`LocationChecks`→ordered exactly-once delivery→restart-no-double-grant→cutscene-
deferral→goal). That harness also caught a real bug: a push handler calling
`run_lua` deadlocks the read loop, so delivery is driven only from the poll /
AP-message tasks. See [[dread-delivery-protocol]].
Options: beyond StartingArea/IncludeBossPickups, the apworld now exposes
TrickLevel, a Metroid DNA collection goal (RequiredArtifacts 0-12 +
ArtifactPlacement), and cosmetic/combat passthrough (HUD toggles, room-name
display, death counter, Raven Beak damage table, nerf power bombs). Energy /
environmental-damage settings are intentionally NOT exposed (they need the
v0.3 damage model). DNA `Metroid DNA k` items map to `ITEM_RANDO_ARTIFACT_k`
and ride the normal item paths; non-actor (boss/EMMI) pickups are keyed by
`pickup_lua_callback`. 233 tests pass (182 apworld + 51 scripts; 1 pre-existing
vendor-fixture test needs the open-dread-rando checkout). Apworld now slugged
`0.0.1-phase4-logic-m2-gateB-options` (world_version 0.2.0).

`accessibility: items`/`full` now GENERATE (forward resolver + classification
fix + Charge Beam forced start — see above and the notes retro); the smoke seed
runs under `items`.

Per-item pickup classes: SHIPPED. `protocol.PATCHER_ITEM_ID_TO_CLASS` mirrors
upstream `open_dread_rando/pickups/lua_editor.py` `SPECIFIC_CLASSES` exactly
(13 entries), and `_attempt_delivery` resolves it per item via
`pickup_class_for(...)`. So input-toggle items (Speed Booster, Phantom Cloak)
and progressive beam/missile items (Wide/Plasma/Wave Beam, Ice/Storm/Super
Missile, Missile Launcher, Flash Shift, Power Bomb, Energy Part) now run
their own `Randomizer*.OnPickedUp` on remote delivery, matching what the
seed-baked path already does. Items not in the dict (Space Jump, Varia,
suits, tanks, DNA, Charge Beam, Grapple Beam, Flash Shift **Upgrade**,
Speed Booster Upgrade, ...) still go through `RandomizerPowerup` — its
additive-resource grant + the `tItemTunableHandlers` chain are correct for
them. **Audit note:** `RandomizerFlashShift.OnPickedUp` zeros out
`ITEM_UPGRADE_FLASH_SHIFT_CHAIN` resources when the player already has
Flash Shift, so the Upgrade item MUST fall through to `RandomizerPowerup`.
That parity with upstream is asserted in `test_pickup_class_for`. Local vs.
remote divergence (`actor == nil` for remote): only `MarkLocationCollected`
is skipped — fine, AP handles location reporting via the bitfield path.
Without this fix, the user-visible bug was that Wide/Plasma Beam, Speed
Booster, Ice Missile did nothing on a remote send and Storm Missile charged-
but-didn't-lock-on, while Space Jump/Varia worked.

Outstanding (non-blocking for v0.1): ammo/damage/E-tank counting (v0.3 — rules
collapse ammo to >=1 and damage to suit ownership); per-trick-category
granularity; door/elevator randomization. Real-hardware (or Ryujinx)
end-to-end run is the next manual gate — but now an *integration smoke* (does the
bootstrap load on the live ROM/2.1.0, does an item pop, does a check register),
NOT a semantics probe: the counter/cutscene questions are settled from source.

Kivy GUI: SHIPPED. `client/gui.py` defines `DreadManager(GameManager)` (lazy-
imported by `DreadContext.run_gui`, so Kivy is never pulled at apworld/generation
load time). It adds one "Dread" tab (50/50 split: an at-a-glance status panel +
a log pane tailing the `<pkg>.client` logger tree, into which Switch-forwarded
`PACKET_LOG_MESSAGE` lines are routed) and a top-bar Switch-status pill that
opens a reconnect popup (editable Switch IP + Reconnect button). The same retry
is available as `/dread_connect [ip[:port]]` (a superset of `/switch_reconnect`,
which is now an alias) — the recovery hatch for the Switch dial losing the race
with Dreadvania's startup. `main.py` calls `run_gui()` when `gui_enabled` and
`DREAD_NOGUI` is unset (the env var still forces the headless CLI used by the
e2e smoke). Pure formatters live in the Kivy-free `client/display.py`
(unit-tested); `tests/test_display.py` + `tests/test_gui_smoke.py` cover them.
The Launcher `Component` now carries `game_name="Metroid Dread"` so it groups
under the game and `.dreadap` files auto-route to it.

## Known unknowns / risks for new work

1. **Cutscene-blocked item delivery — RESOLVED from source (was risk #1).**
   We now deliver via the bootstrap's `RL.ReceivePickup`, which is idempotent
   and cutscene-safe by construction: it grants only when the sent indices match
   the game's live `ReceivedPickups`/`InventoryIndex`, holds one `PendingPickup`,
   defers the grant through cinematics (`Scenario.IsUserInteractionEnabled`), and
   bumps `ReceivedPickups` only on confirm. So a mid-cutscene delivery is
   deferred (never dropped), a duplicate/out-of-order send is ignored, and a
   client restart reads the real count and re-grants nothing. No
   hardware-validated counter mystery remains — the semantics are in
   `bootstrap_part_2.lua` + `randomizer_powerup.lua` (read them, not hardware).
   *Residual live check* (integration, not semantics): confirm the bootstrap
   loads on the actual 2.1.0 ROM and that an item pops + a check registers; see
   the e2e runbook. Per-item pickup classes are now wired (see
   [[dread-delivery-protocol]]). A possible future polish: a
   `Game.AddSF(2.0,RL.UpdateRDVClient,"")` arm could replace our explicit
   per-tick `RL.Get*AndSend` calls if we want game-driven pushes. **Hard rule learned here:** never call `run_lua` from inside a push
   handler (`_on_switch_push` / `_handle_*`) — it runs on the read loop and
   deadlocks awaiting a reply only that loop can read. Drive sends from the poll
   task or AP-message task. See [[dread-delivery-protocol]].

2. **Lua-eval poll latency (2s floor).** Acceptable for v0.1; revisit only if AP async features (deathlink) need it.

3. **`starting_location` regression** in MuratDev41's earlier AP fork — likely a Randovania `PatcherData` schema change between versions. Diff before assuming an AP-specific cause. See PLAN.md risk #6.

4. **Upstream RL.* API churn.** Pin to a specific exlaunch commit hash; smoke-test with `RL.Version` assertion at connect (already done by the Phase 1 script).

## Test commands worth knowing

```pwsh
# Phase 1 wire validation
cd C:\Users\maxwe\Documents\dread_ap
python scripts\phase1_validate.py <switch-ip>

# Once apworld lands:
python -m pytest apworld\dread\tests\ -v
```

## External paths (outside the repo)

| Path | Purpose |
|---|---|
| `C:\Users\maxwe\.switch\prod.keys` | Console keys (same as smo_archipelago) |
| `D:\switch\` | User's microSD |
| `<Dread 2.1.0 NSP>` | User-supplied game dump (copyrighted; never commit, path not stored in repo) |
| `C:\Users\maxwe\AppData\Roaming\Ryujinx\` | Ryujinx install + mods + logs |
| `C:\Users\maxwe\.claude\plans\https-github-com-muratdev41-open-dread-r-polymorphic-pike.md` | The implementation plan |
