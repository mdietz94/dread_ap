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
[ Switch / Dread 2.1.0 ]  <--UDP discover-->  [ PC Client (Python) ]  <--ws-->  [ AP server ]
   exlaunch sysmodule       <--TCP dial-->    DreadContext(CommonContext)     archipelago.gg
   (HARD FORK: mdietz94/                      BridgeServer on :17777
    open-dread-rando-                         DiscoveryResponder on :17776
    exlaunch-bridge)                          Kivy GUI (SwitchesPopup)
   - sweeps /24 for PC                        Forked apworld machinery
   - dials PC:17777                           - Lua-eval RPC over JSON
   - JSON envelope wraps                        - per-Switch device_id +
     Lua-eval RPC                                  active/inactive promotion
   romfs/
   (our forked open-dread-rando output)
```

## Wire protocol (line-delimited JSON, TCP :17777, UDP :17776)

The Switch is now the TCP **dialer**; DreadClient binds. UDP discovery on
17776 lets the Switch find the PC on the LAN automatically — no IP entry.
See [docs/wire-protocol.md](docs/wire-protocol.md) for the full envelope
reference.

**Discovery** (UDP 17776):

  - Switch sweeps `BRIDGE_HOST_STRING` /24 (baked at build time from
    `detect_lan_ip()`) plus loopback (Ryujinx). 2 s collect window.
  - Probe: `{"t":"discover","mod_ver":"<x>"}\n`
  - Reply: `{"t":"bridge","host":"<lan-ip>","port":17777,"seed":"<seed>"}\n`

**TCP envelope** (line-delimited UTF-8 JSON, ≤ 8 KiB / line):

| `t` | Direction | Fields |
|---|---|---|
| `hello` | Switch→PC | `mod_ver`, `dread_ver`, `layout_uuid`, `device_id` |
| `hello_ack` | PC→Switch | `ok`, `slot`, `seed`, `subs`, `err?` |
| `lua_exec` | PC→Switch | `seq`, `src` |
| `lua_exec_reply` | Switch→PC | `seq`, `ok`, `result` |
| `log` | Switch→PC | `level`, `msg` |
| `inventory` | Switch→PC | `index`, `inventory` (array of numbers) |
| `collected` | Switch→PC | `hex` (lowercase hex bitfield) |
| `received_pickups` | Switch→PC | `count` |
| `game_state` | Switch→PC | `scenario`, `beaten` |
| `layout_uuid` | Switch→PC | `value` |
| `ping`/`pong` | both | `ts_ms` |
| `kick` | PC→Switch | `reason` |

The Lua-eval RPC is preserved as `lua_exec` / `lua_exec_reply` — the
entire Randovania `RL.*` bootstrap is unchanged at the Lua level; only
the byte stream underneath it changed shape.

**Connect sequence**:
1. Worker thread on Switch: `nn::nifm` init + UDP discovery (loopback +
   /24 sweep) → first valid `bridge` reply wins.
2. TCP connect to `host:port`, send `hello` (with empty `layout_uuid`).
3. PC's BridgeServer responds with `hello_ack`. First Switch becomes
   ACTIVE; subsequent get `kick("inactive")` and are registered for
   manual / auto promotion.
4. PC sends `lua_exec` chunks of the bootstrap Lua (chunker unchanged
   from before — same `RL.*` definitions, same chunking algorithm).
5. PC's 2 s poll loop fires `RL.GetInventoryAndSend` /
   `GetCollectedIndicesAndSend` / `GetReceivedPickupsAndSend` via
   `lua_exec`; each triggers a push (`inventory` / `collected` /
   `received_pickups`).

Backoff on disconnect: 1 → 2 → 5 → 10 → 30 s cap (matches SMO).
TCP `SO_KEEPALIVE = 1` set on connect.

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
longer trivially reachable; Trick Level is a user option (NOTE: superseded by
the per-trick model — see the "Per-trick toggles" update below; tricks are no
longer pre-collapsed into three files). The compiler is now deterministic
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

UPDATE (softlock prevention removed — recovery moves to runtime `/warp`):
softlocks are NO LONGER prevented in logic. Three mechanisms were removed
wholesale because the client's `/warp` command (warp to the starting save
station — see [[dread-delivery-protocol]] / `client/context.py::_warp_to_start`)
now recovers from any stuck placement at runtime:

  1. The **reverse-reachability "escape" pass** in
     `scripts/extract_dread_rules.py` (`compute_escape_rules` +
     `_reverse_edges` + `_safe_terminal_keys`), which AND-ed "items needed to
     LEAVE each pickup node" into per-location entry rules. GONE.
  2. Its **`_strip_fill_fragile_items` mitigation** (which stripped single-pool-
     copy items from escape ASTs to stop the Morph-Ball fill cascade). GONE —
     it only existed to make (1) fill-solvable.
  3. The **`softlock_locks.json` table** (8 vanilla-item pins + 4 filler-only
     sibling rooms) and its consumer in `Rules.py::set_rules` (old section 2c).
     GONE. `scripts/diagnose_reverse_reachability.py` (the tool that derived
     that table) is also deleted.

UPDATE (`/warp` now refuses to fire from inside a boss arena): warping out of a
boss fight with `Game.LoadScenario` corrupts the encounter (a user warped out of
Kraid mid-fight → couldn't re-enter normally, the fight broke when entered from
the exit, and the death-respawn bricked the game). So `/warp` now blocks while
Samus stands in a boss arena. The engine has no getter for the live collision
camera, but fires `Scenario.OnSubAreaChange(...)` on every subarea transition
(the same hook the room-name display rides). `client/lua/warp_guard.lua` (our
original non-vendored bootstrap extra, like `deathlink.lua`; added to
`bootstrap._EXTRAS`) wraps that callback — chaining, not replacing, so the
upstream progressive-model / blast-shield / room-name updates still run, and
installed once via `RL._WarpGuardInstalled` so a reconnect can't double-wrap —
to record the live `CurrentScenarioID` + subarea, and defines `RL.IsInBossArena()`
which checks them against a baked boss-arena table. That table is
`protocol.BOSS_ARENA_CAMERAS` (scenario → collision-camera id set, curated from
the published room-name dict; EMMI zones deliberately excluded — warp there is
legit recovery) rendered to Lua by `build_boss_arenas_lua_table`. The warp src
calls `if RL.IsInBossArena and RL.IsInBossArena() then return "in_boss"` before
`LoadScenario`; the client surfaces a "reload your last save" message. Residual
gap: connecting fresh while already standing in a boss arena (subarea untracked
until the next transition) isn't caught; a death-respawn inside the same arena
IS (we don't reset the tracked subarea on load). `fakeswitch` models it via
`in_boss_arena`; tests in `test_warp.py` / `test_session_e2e.py` /
`test_bootstrap.py` / `test_protocol.py`.

Consequence by design: AP logic now gates only **entry** to a pickup, never
the ability to leave it. Fill may place any item in a one-way room; if the
player gets stuck, they `/warp`. Generation is *strictly easier* than before
(escape rules and vanilla pins only ever added constraints), so the prior
"0 fill failures" coverage still holds. `compiled_rules.json` is
regenerated escape-free by `conftest._ensure_compiled_rules` (it's
gitignored, never committed; one file since the per-trick update). The historical escape-cascade saga (commit
`7c5a451`, the Morph-Ball single-copy circular-dependency, the 106→24
tightening count) is kept here only as the rationale for *why* the runtime
approach replaced it — none of that code remains.

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

Progressive items: SHIPPED (world_version 0.4.0). Six opt-out-by-default toggles
mirror Randovania — `progressive_suit` (Varia→Gravity), `progressive_spin`
(Spin Boost→Space Jump), `progressive_charge_beam` (Charge→Diffusion),
`progressive_beam` (Wide→Plasma→Wave), `progressive_missile` (Super→Ice),
`progressive_bomb` (Bomb→Cross Bomb). items.json carries one entry per group with
a `progression_tiers` list (the only schema addition); `Items.PROGRESSIVE_GROUPS`
/ `PROGRESSIVE_TIERS` / `PROGRESSIVE_MAP_ICON` are the single source of truth.
When a group is enabled, `World.create_items` drops its tier items and adds the
`Progressive X` item at one copy per tier (pool-size neutral). The compiled rules
are UNCHANGED: `World.collect`/`remove` mirror the k-th progressive copy onto the
k-th tier name in `state.prog_items` (standard AP idiom), so `state.has("Wave
Beam")` etc. still works. Delivery is the key faithfulness win — both the local
(seed-baked) patcher resources AND the wire `RL.ReceivePickup` send the FULL
multi-stage progression with `cls = pickup_class_for(first_tier_id)` (=
open-dread-rando's `get_parent_for`). `randomizer_powerup.lua`'s
`HandlePickupResources` grants the first stage whose first item the player lacks,
so the game auto-advances to the next missing tier identically for local and
remote pickups — no client-side tier counting, idempotent across restarts by the
ReceivedPickups cursor. `_build_placements_payload` emits `progression_stages` /
`models` / `map_icon_id` (PROGRESSIVE_* icon) for own progressive items;
`patcher_pipeline.placements_to_overrides` threads them through (upstream
`open_dread_rando` builds the `RandomizerProgressive…` class + animated models
from the multi-stage `resources` + model list). `fakeswitch` is now stage-aware
(`_grant_progression` models `HandlePickupResources`). Tests: pool swap +
collect/remove round-trip (test_item_pool, AP-gated), datapackage progression +
class (test_datapackage), patcher merge (test_seed_to_patcher), e2e ordered
Wide→Plasma→Wave + saturation + restart (test_session_e2e).

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

Map-screen item icons: SHIPPED. The in-game pause map shows a per-pickup icon
(`map_icon` on each template pickup); the starter preset bakes Randovania's OWN
placement icon at every spot, so after AP shuffling each icon LIED — a relocated
Missile Tank still showed whatever vanilla item used to sit there, and a foreign
item advertised the Dread item it replaced. `patcher_pipeline` now rewrites
`map_icon` alongside the model + caption it already rewrote, mirroring
Randovania's own exporter (`patch_data_factory._pickup_detail_for_target`) via
`_map_icon_override`: own item w/ a concrete model → `{"icon_id": <model>}`; own
item rendered as the orb (model `itemsphere`, e.g. Metroid DNA) →
`{"custom_icon": {"label": NAME}}`; cross-slot item (always the orb here) →
`{"custom_icon": {"label": NAME, "base_icon": "unknown"}}` (the "?" glyph, =
`CROSS_SLOT_MAP_BASE_ICON`, matching upstream's off-world treatment and pairing
with `CROSS_SLOT_MODEL`'s neutral orb). `merge_overrides` only rewrites an
EXISTING `map_icon` (every actor pickup has one; the 12 non-actor boss/EMMI
drops don't, and don't show on the item map) and preserves the template's
`original_actor` via `_merge_map_icon` (the schema's `map_icon` is a oneOf of
{empty, icon_id, custom_icon} + optional original_actor, so we swap exactly one
icon branch). Verified: a full 137-pickup merge (own/cross mix) validates against
the real upstream `open_dread_rando` `schema.json`, and all 12 original_actor
refs survive. Tests in `scripts/tests/test_seed_to_patcher.py` +
`test_build_patcher_json.py`.

Per-trick toggles: SHIPPED (world_version 0.5.0). Tricks are no longer collapsed
to Trivial/Impossible at compile time against one global level (the old
`compiled_rules{,_l2,_l3}.json` triple-bake is GONE — `.gitignore`, release.yml,
conftest, install_apworld all single-file now; `SCHEMA_VERSION` 2→3). Instead the
compiler keeps each trick SYMBOLIC: `translate_requirement` emits
`{"type":"trick","name","level"}`, `ast_to_dnf`/`dnf_to_ast` carry a
`("trick", name, level)` atom (the DNF engine treats it as an opaque cost atom;
`_substitute_events` already passes non-event atoms through), and
`_disjunct_sort_key` penalizes trick atoms so item-only paths win at the cap —
truncation can only ever over-restrict a trick-disabled player, never
falsely-reach (AP accessibility stays sound). One `compiled_rules.json` now
carries trick atoms. **Two-pass union** (the bake runs the forward resolver
twice, ~10 min): keeping ALL tricks symbolic adds disjuncts that crowd the
bounded-DNF cap and truncate the long item-only / Beginner-trick detours to ~9
deep pickups (Burenia/Cataris/Ferenia/Hanubia), marking them unreachable at low
trick configs though a path exists. So `main()` runs a second `compile_forward`
with `strip_tricks_above=1` (level>=2 trick edges → impossible, Beginner tricks
kept SYMBOLIC) and OR-s it into the per-pickup rules + victory. The union is
sound — every recovered disjunct is item-only or gated on a still-symbolic
Beginner trick (resolved per-trick at generation, disable-able with no false
positive) — and the full symbolic pass still supplies level>=2 shortcuts. Result:
all 149 pickups + victory reachable at Beginner with a full loadout (old default
behavior preserved), verified. At AP-generation time `Rules.compile_to_lambda(ast, player,
trick_levels)` resolves each atom against `Tricks.effective_trick_levels(options)`
— a constant per seed (depends only on options, not items), so it never
reintroduces the item↔event cycle the forward resolver breaks. `apworld/dread/Tricks.py`
is the single source of truth: the 26 Randovania tricks (short/long name, hidden
flag; Suitless is hidden→always follows global), level names (1=Beginner…5=Mastery),
and the effective-level helper. Options: the global `TrickLevel` stays as the
BASELINE (now extended with Expert/Mastery) applied to any trick left on
`follow_global`; `Options.py` generates one `TrickOverride(Choice)` subclass per
non-hidden trick (`trick_<name>`, default follow_global) from `VISIBLE_TRICKS`
and composes `DreadOptions` via `make_dataclass`. Untouched YAMLs behave exactly
as the old Beginner default. Tests: `tests/test_trick_level.py` (rewritten:
effective-level map + symbolic-atom resolution + artifact carries trick atoms),
`tests/test_rule_compiler.py` + `scripts/tests/test_extract_dread_rules.py`
(trick translation now symbolic, DNF round-trip, sort-key penalty). Faithful win:
levels 4–5 (Expert/Mastery) are now reachable, which the old 1–3 file system
could not express.

Flash Shift / Speed Booster chain upgrades: SHIPPED. Neither the Flash Shift
Upgrade nor the Speed Booster Upgrade is a base-game item — Randovania adds them
as custom pickups and, by default, shuffles NONE of them. So both now have
`pool_count: 0` in items.json, and two new count options
(`flash_shift_upgrade_count` / `speed_booster_upgrade_count`, default 0, the
single override consulted in `World.create_items` `pool_overrides`) shuffle them
in when raised. The chains/charges are instead baked into the MAIN pickup via two
`…_from_main` options (`flash_shift_chains_from_main` /
`speed_booster_charges_from_main`, both default 3 = vanilla). This is the key
faithfulness point: the access logic gates some routes on `Flash AND
FlashUpgrade>=N` / `Speed AND SpeedBoostUpgrade>=N` (N up to 3, verified against
the upstream snapshot), and in vanilla the main grants the chains that satisfy
them. So "from main" is NOT cosmetic — it feeds logic. Three coupled mechanisms:
(1) `protocol.pickup_resource_stage` expands `ITEM_GHOST_AURA` / `ITEM_SPEED_BOOSTER`
into `[{unlock:1},{chain/charge:N}]` (same paired-resource pattern as the main
Power Bomb), where N is the `…_from_main` value — this rides each main's
placement `quantity` (set via `ammo_amount_override["Flash Shift"]` / `["Speed
Booster"]`) so BOTH the seed-baked patcher path AND the wire `RL.ReceivePickup`
path (slot_data `item_amounts`) bundle the chains into the main's first/only
pickup; the unlock-flag class (RandomizerFlashShift / RandomizerSpeedBooster)
grants both because the "already has Flash Shift" zero-out branch never fires for
the main's own grant. (2) `World.collect`/`remove` credit `…_from_main` copies of
the chain-upgrade item onto `state` when the MAIN is collected (the established
progressive-collect idiom; backend-agnostic — both the closed-form and native
graph read `state.has`), so `state.has("Flash Shift Upgrade", 3)` clears once the
main is reachable with 0 upgrades shuffled. (3) classification is dynamic: any
shuffled copies are progression only for the first `max(0, MAX_CHAIN_REQ -
from_main)` (= 0 at the default `from_main=3`), else `useful` — `MAX_CHAIN_REQ=3`.
If the main plus shuffled copies can't reach a route's requirement that route
drops out of logic (AP accessibility stays sound; no silent over-reach). The two
upgrades are no longer in `MIXED_CLASSIFICATION_FIRST_N`. Tests:
`test_item_pool` (pool_count=0, default-absent, count-drives-pool, dynamic
progression split, collect/remove chain credit round-trip), `test_protocol`
(pickup_resource_stage GHOST_AURA / SPEED_BOOSTER baking). NOTE: the existing
`flash_shift_upgrade_amount` / `speed_booster_upgrade_amount` options (per-pickup
grant) are unchanged and stay logic-inert (each shuffled upgrade still credits 1
in logic, conservatively).

Outstanding (non-blocking for v0.1): ammo/damage/E-tank counting (v0.3 — rules
collapse ammo to >=1 and damage to suit ownership); door/elevator randomization.
Real-hardware (or Ryujinx)
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
