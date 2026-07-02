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

  - Switch sweeps the /24 whose seed is read at runtime from
    `rom:/ap_config.json` `bridge_host` (written at deploy time from
    `detect_lan_ip()`; no compile-time bake) plus loopback (Ryujinx). 2 s
    collect window.
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
display, death counter, Raven Beak damage table, nerf power bombs). Energy
settings (Energy Tank/Part counts, Energy Per Tank) ARE exposed and now feed
the faithful HP damage model — see the "faithful v0.3 HP damage model" update
below; only the environmental/energy-drain half of the damage model is still
deferred. (This paragraph is an older status entry; at the time energy was
unexposed and damage collapsed to suit ownership — no longer true.) DNA
`Metroid DNA k` items map to `ITEM_RANDO_ARTIFACT_k`
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
flag), level names (1=Beginner…5=Mastery), and the effective-level helper.
Suitless (Heat/Cold Runs) is now VISIBLE — Randovania hides it in its own UI, but
we expose it as a normal per-trick option so it can be DISABLED independently
(the global `TrickLevel` has no `disabled` tier, so a hidden Suitless could only
ever sit at the Beginner floor, blocking a faithful all-tricks-disabled
starter-preset port). So all 26 tricks are now in `VISIBLE_TRICKS`; the `hidden`
field is retained but no Dread trick currently sets it. Options: the global
`TrickLevel` stays as the BASELINE (now extended with Expert/Mastery) applied to
any trick left on `follow_global`; `Options.py` generates one
`TrickOverride(Choice)` subclass per non-hidden trick (`trick_<name>`, default
follow_global) from `VISIBLE_TRICKS` and composes `DreadOptions` via
`make_dataclass`. Untouched YAMLs behave exactly as the old Beginner default.
STARTER-PRESET PORT + accessibility guard: Randovania's Dread starter preset is
`minimal_logic: false` + `specific_levels: {}` = EVERY trick Disabled (verified
against the real `.rdvpreset` at the pinned commit). It still generates because
Randovania only guarantees BEATABILITY, not full reachability — its trick-gated
pickups just hold filler. That is exactly AP `accessibility: minimal`. So the
faithful port = all 26 `trick_*` = disabled + `accessibility: minimal` (and the
Suitless un-hide above is what makes "all disabled" expressible). Confirmed from
the cached RDV source that 8 pickups (Ferenia Speedboost Slopes Maze / Space Jump
Room, Burenia Early Gravity Speedboost, Cataris+Ghavoran Dairon Transport Access,
Dairon Freezer/Storm Missile Gate/Energy Recharge West, Elun Fan Room) have their
ONLY incoming edge gated on the `Speedbooster` (Speed Booster Conservation) trick
— no item-only path in Randovania either. So this is NOT an extraction gap; it's
faithful. Under AP `full` (and its `items` alias) those 8 must be reachable,
which all-tricks-off cannot satisfy, so `World._guard_full_accessibility`
(generate_early) now pre-empts the cryptic late FillError: it runs
`graph_logic.unreachable_pickup_locations` (a full-loadout `early_reachable`
sweep, door/transport/start-aware via the new `dock_assignments` arg threaded
through `early_reachable`) and, when accessibility != minimal and any pickup is
unreachable with EVEN a full loadout, raises an `OptionError` naming the stranded
locations + the recovering trick (narrowed to the actual recoverer by re-probing
each frontier-trick at max level — error path only, so the extra sweeps are free)
+ the two fixes (raise the trick / use `accessibility: minimal`). The guard never
fires for the default Beginner config (146 full gens still pass). Tests:
`test_door_start.py` (full raises / minimal generates / only-Suitless full /
default full), `test_graph_logic.py::test_unreachable_pickup_locations_all_tricks_disabled`
(exact 8-set + `gating=={"Speedbooster"}`, AP-free). Tests: `tests/test_trick_level.py` (rewritten:
effective-level map + symbolic-atom resolution + artifact carries trick atoms),
`tests/test_rule_compiler.py` + `scripts/tests/test_extract_dread_rules.py`
(trick translation now symbolic, DNF round-trip, sort-key penalty). Faithful win:
levels 4–5 (Expert/Mastery) are now reachable, which the old 1–3 file system
could not express.

Flash Shift / Speed Booster chain upgrades: SHIPPED — mirrors Randovania's
config 1:1 (verified against `dread/presets/starter_preset.rdvpreset`). Neither
upgrade is a base-game item; RDV shuffles NONE by default, and the two are
modelled DIFFERENTLY:

  * **Flash Shift Upgrade** is an AMMO pickup of the Flash Shift major. RDV:
    `pickup_count: 0`, `ammo_count: [1]`, `requires_main_item: true`; the Flash
    Shift major carries `included_ammo: [2]` (vanilla "2 flashes after the first"
    = 3 total dashes). Options: `flash_shift_upgrade_count` (pickup_count, 0),
    `flash_shift_upgrade_amount` (ammo_count, 1), `flash_shift_included_ammo`
    (included_ammo, **2**), `flash_shift_upgrade_requires_main_item`
    (requires_main_item, On — logic-inert/parity: every FlashUpgrade rule already
    AND-s Flash, and the dashes are inert in-game without the ability).
  * **Speed Booster Upgrade** is a STANDARD pickup (NOT ammo of Speed Booster).
    RDV: 0 shuffled by default; the Speed Booster major includes NOTHING (there
    is no "from main" charge amount). Option: `speed_booster_upgrade_count`
    (num_shuffled_pickups, 0).

Both have `pool_count: 0` in items.json (count options drive the pool). The
`included_ammo` feeds ACCESS LOGIC: the logic gates routes on `Flash AND
FlashUpgrade>=N` / `Speed AND SpeedBoostUpgrade>=N` (N up to 3). Three
mechanisms: (1) `protocol.pickup_resource_stage` expands `ITEM_GHOST_AURA` into
`[{unlock:1},{chain:N}]` (paired-resource pattern like the main Power Bomb),
N = `flash_shift_included_ammo`, riding the main's placement `quantity`
(`ammo_amount_override["Flash Shift"]`) so BOTH the seed-baked patcher path AND
the wire `RL.ReceivePickup` path (slot_data `item_amounts`) bundle the 2 chains
into the main; RandomizerFlashShift grants both because its "already has Flash
Shift" zero-out branch never fires for the main's own grant. `ITEM_SPEED_BOOSTER`
stays a single resource. (2) `World.collect`/`remove` credit `included_ammo`
copies of Flash Shift Upgrade onto `state` when the MAIN Flash Shift is collected
(established progressive-collect idiom; backend-agnostic), so
`state.has("Flash Shift Upgrade", 2)` clears once the main is reachable — Speed
Booster credits nothing. (3) classification is dynamic: shuffled Flash Shift
Upgrades are progression for the first `max(0, MAX_CHAIN_REQ - included_ammo)`
(= 1 at vanilla 2), Speed Booster Upgrades for the first `MAX_CHAIN_REQ` (no main
credit); `MAX_CHAIN_REQ=3`. The `FlashUpgrade>=3` route therefore needs one
shuffled Flash Shift Upgrade or an alternative — verified solvable at the
faithful defaults (flash included 2, speed 0, counts 0) across
minimal/beginner/mastery. The two upgrades are no longer in
`MIXED_CLASSIFICATION_FIRST_N`. Tests: `test_item_pool` (pool_count=0,
default-absent, count-drives-pool, dynamic progression split, collect/remove
credit round-trip — flash only), `test_protocol` (pickup_resource_stage
GHOST_AURA bundling; SPEED_BOOSTER passthrough).

Faithful HP damage model (v0.3 — DAMAGE half SHIPPED): the native graph emits
no-suit `damage_threshold` atoms with REAL `hp_needed` values (Randovania's
pre-resolved route damage, up to 1150). These are now HONORED, not stripped:
`Rules.compile_to_lambda` resolves them as `any suit OR 99 + energy_per_tank*
EnergyTank + (energy_per_tank/4)*EnergyPart >= hp_needed`. For that to be SOUND
under AP's advancement-only beatability sweep, `World.create_items` makes enough
Energy Tank / Energy Part copies `progression` to cover the worst gate at the
slot's `energy_per_tank` (`_energy_progression_counts`: credit tanks first, then
parts; default 100/tank → 8 tanks + 11 parts advancement, the rest `useful`;
items.json Energy Part flipped filler→progression). `energy_per_tank` now FEEDS
LOGIC (the old "pure difficulty, logic-inert" note is stale): lowering it shrinks
the budget so the player needs more energy for the same route (threaded through
`graph_logic`, `Rules`, and `DoorRando._eval`; a part = 1/4 tank, base 99 fixed).
`MAX_NO_SUIT_HP=1150` in World.py is pinned to the graph by
`test_graph_logic::test_max_no_suit_threshold_matches_world_constant`. KEY
soundness fact: every high no-suit gate is ALWAYS OR'd with a non-damage
alternative (a Combat/Knowledge trick branch and/or a Power Bomb route), so the
energy budget is rarely the sole binding factor — verified by 20/20 real
generations passing at FULL accessibility across default / DreadfulJim
(door+transport rando) / energy_per_tank=60 / energy counts 14+32, ~4.2s each
(fill ~150-200ms, no fragility even with the larger advancement pool). We
DELIBERATELY do not pre-guard low-energy configs: `energy_per_tank=60` and
`energy_tank_count=4` are solvable at full despite a sub-1150 max budget, so a
budget guard would reject solvable seeds; AP's own FillError is the accurate
signal. Known-unsupported extreme: `energy_tank_count=0` (16 parts self-gate —
parts behind the very damage route that needs them; neither full nor minimal
reliably generates — use low `energy_per_tank` instead). Residual gap: the baked
`hp_needed` values assume vanilla 100/tank, and we scale the BUDGET by
`energy_per_tank` but do NOT rescale the thresholds themselves (they're route
damage, independent of tank size — correct), so no rescaling is needed; the only
assumption is the fixed 99 base HP. Tests: `test_rule_compiler` (energy_per_tank
scaling + part-fraction), `test_item_pool` (energy progression visibility +
scaling + pool caps + low-energy still-generates), `test_graph_logic` (expanded
solvability matrix + threshold pin).

Faithful AMMO model (v0.3 — AMMO half SHIPPED): the native graph already emitted
`sum` capacity atoms for missiles (`base 15 + 2/Missile Tank + 10/Missile+ Tank`)
and power bombs (`base 0 + 2/Power Bomb launcher + 2/Power Bomb Tank`), plus a
plain `Missile Tank>=1` atom standing in for Randovania's MissileLauncher unlock.
These now FEED LOGIC two ways. (1) `Rules.compile_to_lambda` takes an
`ammo_amounts` dict (`graph_logic.ammo_amounts_from_options`) that rescales each
`sum` atom's `base`/`per_unit` to the slot's `starting_missiles` /
`*_tank_ammo` / `starting_power_bombs` settings — the `threshold` (route's raw
ammo demand) is never rescaled. Threaded through `graph_logic`, `Rules`, and
`DoorRando._eval`. The old "logic-inert difficulty knob" notes on those options
are stale. (2) `World.create_items` makes just enough Missile / Missile+ / Power
Bomb Tank copies `progression` (`_ammo_progression_counts`) to clear the binding
capacity at the slot's settings — dynamic, in `chain_progression_n`; the tanks
were REMOVED from `MIXED_CLASSIFICATION_FIRST_N`. KEY EMPIRICAL FINDING (walked
the graph, `scripts/analyze_ammo_binding.py`): the prior "ammo is never the sole
gate" is HALF TRUE. Zero ammo loses ~36 pickups AND the Ship goal, so ammo IS
binding — but the irreducible binding capacity is tiny: `MAX_BINDING_MISSILE_CAP=
15` and `MAX_BINDING_PB_CAP=2` (every higher `sum` threshold, up to 300, IS OR'd
with a reachable alternative). And the launcher-unlock `Missile Tank>=1` is
satisfied by the PRECOLLECTED Missile Tank. So at the VANILLA defaults (start 15
missiles / 2 PB) base capacity already meets both caps → `_ammo_progression_counts`
returns (0,0,0): NO findable tank is required, STRICTLY FEWER advancement copies
than the old static (3/1/1), a fill-health WIN. Findable progression copies only
appear when the user lowers starting/per-tank ammo (e.g. `starting_missiles=0`
→ 7 Missile Tanks). Soundness: every counted gate ≤ the cap is satisfiable by the
advancement sweep at the chosen amounts; gates above the cap are OR'd, so a thin
ammo budget never strands a location — AP's own FillError is the accurate signal
otherwise. Two clear OptionError guards replace cryptic FillErrors: (a) PB / missile
capacity that can NEVER reach the cap (no source), and (b) the self-gate extreme
`starting_missiles=0 AND missile_tank_ammo=0` (all capacity forced through the 12
Missile+ Tanks, which sit behind the very missile gates they'd open — unsolvable
at any accessibility, verified across seeds; the analogue of the
`energy_tank_count=0` extreme). `MAX_BINDING_*` pinned to the graph by
`test_graph_logic::test_max_binding_ammo_cap_matches_world_constants`. Robustness
+ timing: 4-slot ammo-extreme multiworld (start 0 / tank_ammo 0|1 / pb 0) ~3.6s,
8-slot DreadfulJim (door+transport rando) + ammo extremes ~10s, no fragility.
Tests: `test_rule_compiler` (sum option scaling + threshold invariance),
`test_item_pool` (vanilla 0-progression + low-ammo scaling + both guards),
`test_graph_logic` (ammo-stressing solvability matrix + cap pin),
`test_extract_dread_rules` (sum translation unchanged).

Environmental / energy-drain damage (the "other half" of v0.3) — INVESTIGATED,
NOT A REMAINING GAP at default config; one real leniency fix landed. Findings:
  * **`environmental_damage.py` is PURELY the patcher** (sets in-game heat/cold/
    lava room DPS+tick on the actor configs); it carries ZERO logic. All damage
    LOGIC lives in the cached RDV logic JSON we already extract — confirmed.
  * **RDV's RESOLVER is genuinely path-cumulative.** `energy_tank_damage_state.py`
    threads a `DamageState`: `apply_damage` subtracts each crossed segment's
    `amount` from CURRENT energy, `apply_node_heal` refills to max at the 26 heal
    nodes (save/recharge stations), and each gate is checked against the running
    `health_for_damage_requirements()` (= current, not max). The native
    `resolver_reach` builds a per-node current-health map. So RDV requires
    surviving the SUM of damage along a no-heal chain; AP (no path memory) checks
    each gate independently vs MAX HP — our model is LENIENT in principle (never
    subtracts across a chain).
  * **The per-requirement `amount` is the WHOLE segment damage, already baked**
    (e.g. the Cataris Kraid→Diffusion lava tunnel is ONE `Lava:1000` edge gated by
    `Suitless`-2). The cumulative-vs-max distinction only bites when 2+ damage
    edges chain between heal nodes.
  * **The gap is INERT at the default Beginner trick level.** A multi-source
    Dijkstra (refill at the 26 heal nodes) over the source DB shows the worst
    cumulative no-suit route to ANY node at `Suitless<=1` is ~549 HP — far under
    a single edge and under the 1150 budget — because every high-damage chain is
    gated behind `Suitless` 2-3 (an explicit opt-in mirroring RDV's own trick).
    The only nodes whose cumulative exceeds 1150 (the Diffusion Beam / Kraid lava
    cluster, ~1330) need Suitless 2-3 AND are always OR'd with a Gravity route, so
    the lenient no-subtraction path is never the SOLE reachability grant. Modeling
    true cross-room depletion is intractable in AP (item-set logic, no path
    memory) and unnecessary here. Verdict: per-gate max-HP thresholds are a sound,
    conservative-enough approximation of RDV's gating for Dread; no region-floor
    machinery is warranted.
  * **REAL FIX shipped — Lava suit list corrected to Gravity-only.** RDV's
    `damage_reductions` give Lava+Varia a 0.75x multiplier (a reduction, NOT a
    negation) and Lava+Gravity 0.0x; the logic DB never offers Varia as a direct
    lava pass. `translate_damage` previously listed BOTH suits for Lava (copied
    from Heat), letting a Varia-only player bypass every lava gate for free — a
    concrete leniency bug. Heat is unchanged (both suits ARE 0.0x = negated). Now
    Lava → `["Gravity Suit"]` (mirrors the existing Cold treatment). Sound: every
    lava gate already offers Gravity or the no-suit HP-budget route, so dropping
    Varia removes a false free pass without removing the only pass — verified
    solvable at full+minimal across trick 1/5, default + `energy_per_tank=60`, and
    the DreadfulJim door+transport stress seed. Tests:
    `test_translate_damage_lava_emits_threshold` pins Gravity-only.

Outstanding (non-blocking for v0.1): the v0.3 damage/ammo model is now essentially
complete — HP-threshold damage (faithful), ammo counting, and environmental/
energy-drain damage (at RDV parity — see above). Door-lock and transport
(elevator/shuttle) randomization are SHIPPED (`DoorRando.py` / `TransportRando.py`,
exposed as the `door_lock_rando` / `transport_rando` options).

UPDATE (transport room-name display): transport rando now rewrites the in-game
room-name-display label of each shuffled ride's SOURCE room so it names the NEW
destination instead of the vanilla one (previously it leaked the vanilla
"Transport to <old dest>", because the cosmetic `camera_names_dict` is keyed by
collision camera and was passed through untouched). Graph schema bumped 3→4:
each `transports` entry now carries `source_cc` (the source room's
collision-camera id, from the sub-area `extra.asset_id`). `TransportRando.
matching_to_room_names` builds `{scenario: {cc: "Transport to <dest
transporter_name>"}}` for ALL transport endpoints (resolved dest = matching-or-
vanilla), mirroring Randovania's `_build_teleporter_name_dict` — `transporter_name`
(e.g. "Dairon - Superheated") is the same string the map-icon `connection_name`
uses, so the room name and map icon stay in sync. `World._transport_room_names`
threads it into the placements payload; `patcher_pipeline.merge_overrides` merges
it into `cosmetic_patches.lua.camera_names_dict` at the collision-camera level
(non-transport room names untouched; empty ⇒ byte-identical, so non-rando seeds
are unchanged). It only corrects the TEXT — whether the label shows is still
governed by the Room Name Display option (NOT force-enabled, unlike Randovania
which force-shows transport names when room names are off). Tests:
`test_graph_logic` (source_cc presence + `matching_to_room_names`),
`test_seed_to_patcher` (camera-dict merge + empty no-op), `test_door_start`
(`test_transport_rando_rewrites_room_names`, full World→payload→patcher path).

UPDATE (transport rando crash fix — Itorash capsules + Flipper dual-actor): a user
hit a game-side `strlen(NULL)` crash (cazadora.nss) at both Hanubia entrances and
after killing Raven Beak with transport+door rando on. Root cause: the two
Hanubia↔Itorash rides are NOT ordinary elevators — they use
`CCapsuleUsableComponent` (`capsulelaunchershipyard` up-launch +
`capsuleelevatorskybase`, the post-Raven-Beak ESCAPE ride) and their landing is a
special scripted platform. Our extractor typed them `elevator` and dropped them in
the normal shuffle pool, so a shuffled capsule (or a normal ride routed onto its
platform) loads a room that can't resolve the capsule's scripted spawn/cutscene
actor → null-string deref on the ride. Randovania never ships transport rando
enabled (both Dread presets keep teleporters vanilla), so this was never exercised
upstream. Fix: graph schema 5→6 now threads each transport's USABLE `component`;
`TransportRando._by_type`/`_shufflable` exclude `CCapsuleUsableComponent` endpoints
(kept vanilla — they're each other's only same-type pair, so zero variety lost;
`transport_pairs` still emits their `default_dest` edge so Itorash/Raven Beak stays
reachable — strictly easier, no fill regression). Also fixed a co-located bug: the
Ghavoran "Flipper" shuttle has a cutscene actor AND a plain actor in one room;
open-dread-rando only repoints the one we pass, so `matching_to_elevators` now
duplicates the entry onto `wagontrain_quarantine_000` (mirrors Randovania's
`create_game_specific_data`), or later rides snapped back to the vanilla dest.
Tests: `test_door_start` (`test_itorash_capsule_rides_never_shuffled`,
`test_flipper_shuttle_patches_both_actors`), `test_graph_logic` (schema==6).
NOTE: needs `python scripts/extract_dread_rules.py --all` to regen the gitignored
`logic_graph.json` at schema 6 (conftest auto-regens when the script is newer).

CONFIRMED — DO NOT RE-ENABLE the capsule shuffle without proving the post-Raven-Beak
escape works in-game. A follow-up investigation nearly reverted this exclusion on a
WRONG premise, so the trap is documented here: Randovania's `all_settings` patch-data
fixture (`test/test_files/patcher_data/dread/dread/all_settings/world_1.json`) DOES
shuffle both capsules (capsulelaunchershipyard -> Burenia; capsuleelevatorskybase ->
Burenia) with the exact `elevators` entry shape we emit — so the capsule looks
"supportable" and our config looks faithful to RDV. It is a TRAP: RDV ships
teleporters VANILLA in every preset, so that fixture is patch DATA that was never
played through the Dread ending. The decisive evidence the exclusion is right: in the
crashing seed the client room-name-display log shows the player at `commander_elevator`
(Raven Beak's elevator in Itorash) AT the crash — they rode it UP fine, beat the boss
(`DefeatedBossID 4`), then crashed on the way OUT, i.e. the post-RB ESCAPE (which rides
`capsuleelevatorskybase` back to Hanubia). Itorash (`s090_skybase`) has ZERO
rando-eligible doors, so door rando is excluded as the cause. A user's "Itorash access
from lower Burenia" RDV memory was the ACCESS path (capsulelaunchershipyard up-launch),
which works; the ESCAPE is a different scripted path that shuffling breaks.

IMPORTANT nuance (don't over-read the exclusion as "RDV can't do it"): RDV's GUI
(`dread_teleporters_tab._create_source_teleporters` + `on_preset_changed`) lists EVERY
transporter — capsule included — as a checkbox that is CHECKED (= shuffled) by default
unless the user adds it to `excluded_teleporters`. There is NO capsule special-casing.
So real RDV users on "Two-way, between regions" DO shuffle this capsule. That means the
exclusion is a SAFE STOPGAP, not proof RDV is incapable: the open question is whether
RDV's post-RB escape actually survives a shuffled escape-capsule in-game (we have not
tested an RDV build through the ending), OR whether OUR patcher output diverges from
RDV's for these connections. Judge stability by what RDV's GUI exposes, NOT by the
shipped presets (which are vanilla). Resolve definitively with the crashing seed's
`ap_patcher_input.json` (diff our s080/s090 `elevators` + `door_patches` + pickups vs
RDV's shape) — the user still has the seed. Full capsule support, if pursued, would
need whatever drives the post-RB escape transition (none found in open-dread-rando:
`static_fixes._apply_boss_cutscene_fixes`, `game_patches` chozocommander tweaks, and
the `remove_grapple_block_path_to_itorash` / `hanubia_easier_path_to_itorash` options
are the only Itorash-area patches, and none repoint the escape).
Lower-confidence sibling risk (NOT excluded, no crash report yet):
the other cutscene-variant transports — `elevator_with_cutscene_aqua_000` (Artaria) —
could break similarly; the Flipper (`wagontrain_quarantine_with_cutscene`) is handled
by the dual-actor patch above. The reported "2 entrances to Hanubia" crashes are
consistent with the two capsule directions but not independently confirmed from logs;
100% confirmation of the full set would need the seed's `ap_patcher_input.json` or a
post-fix playthrough.

UPDATE (mutually-exclusive thermal-toggle fidelity gap — bounded + proven sound,
NOT modelled): Cataris "Thermal Device Room North" holds the game's ONLY pair of
physically-exclusive heat-redirect devices (`deviceheat_002` ↔
`deviceheat_camerafar_000`; stepping on one platform powers the other down). RDV
encodes the exclusion via polarity — one cross route needs `deviceheat_002` ON,
the other needs `NOT deviceheat_002 AND deviceheat_camerafar_000`. AP's sweep is
MONOTONIC (`event` → `state.can_reach_region`, true-forever) and
`translate_requirement` drops every `NOT <event>` to TRIVIAL, so AP believes both
polarities hold at once — strictly more permissive than the game, a latent
unmodelled-exclusion gap. Investigation (all 9 areas): exactly ONE co-located
device pair exists; the other 5 thermal devices are solo-per-room and the other
23 negated events are one-way directional transients (drop-the-transient is
faithful for those). We do NOT model the exclusion in the monotonic sweep (a real
departure, disproportionate for one room) and do NOT force `NOT <toggle>` →
IMPOSSIBLE (walls the room off → breaks generation, cf. the escape-rules saga).
Instead: (1) `MUTEX_TOGGLE_EVENTS` curates the pair and negated events route
through `_negated_event_requirement` (explicit, not a silent `return TRIVIAL`);
(2) `assert_toggle_model_soundness` (called from `extract_dread_rules.main`)
PROVES the drop sound for the shipped version — every node reachable via a
`NOT <toggle>` edge is ALSO reachable via a toggle-independent edge (a boundary
`dock`, or the `CatarisThermalMagnetPlatformLowered` route for the Energy Tank),
so the dropped negation is NEVER the sole gate on any node. Topology is fixed per
version (fill places items, never adds/removes edges), so this version-boundary
proof is also a per-seed proof. The assertion is a TRIPWIRE: a new co-located
device pair upstream, or a `NOT <toggle>` edge becoming a node's only entry, fails
extraction loudly. Runtime recovery from a player-induced stuck toggle is already
covered by `/warp` (it `Game.LoadScenario`s from the last save and relocates Samus
out of Cataris; the device is reversible in-game) — so no bespoke
device-poke client command was added (it would need unvalidated RE of the
`deviceheat` actor Lua API). Tests: `test_extract_dread_rules` (negated-toggle →
TRIVIAL, unknown-negated-event raises, soundness passes on the real graph, trips
on a synthetic new pair + on a synthetic sole-gate node).

UPDATE (one-way rotatable "flipper" false-positive — FIXED): a DIFFERENT toggle
class from the thermal mutex. Randovania models one-way flipper platforms
(stepping on them rotates 90° IRREVERSIBLY and seals two room halves; reconnect
only with Screw Attack) with an OPTIMISTIC "turn it and land on the useful side"
traversal gated on `NOT misc:HighDanger` (RDV's own comment: "assumes there are
no bad consequences"). With Highly Dangerous Logic off — our only config —
`NOT HighDanger` is always-true, so the monotonic sweep credited the useful
post-turn position as PERMANENTLY reachable. FALSE POSITIVE: once the flipper is
turned the wrong way (or you leave before capitalising) the useful side is gone,
and `/warp` CANNOT restore it (the flip persists in the save — unlike the
reversible thermal device). It also misled Universal Tracker (`/explain` showed
the pickup "reachable" off that very edge). Confirmed live in Artaria Screw Attack
Room: a Spin Boost + Screw Attack player (no Gravity/Space/Speed, all tricks
disabled) was told the Screw Attack pickup was reachable, then stranded in the
water. The compiled false path was `elevator-comp → flipper-event-node [TRIVIAL]
→ Total Recharge [TRIVIAL] → up with Spin`. ROOT CAUSE: the turn edge is
`OR[NOT HighDanger, NOT ArtariaSARotatable]` and we drop BOTH disjuncts to TRIVIAL
(`NOT HighDanger` via `MISC_RESOURCE_VALUES`, `NOT <event>` via
`_negated_event_requirement`) — so removing only the HighDanger atom (the first
hypothesis) leaves the co-guard trivial and the edge stays free. FIX:
`ONE_WAY_ROTATABLE_EVENTS` (`scripts/extract_dread_rules.py`, starts with
`ArtariaSARotatable`) curates the flipper events; `_rotatable_pessimistic_neg`
computes a per-room set and `translate_requirement(..., pessimistic_neg=...)`
resolves BOTH `NOT HighDanger` AND the room's rotatable `NOT <event>` to
IMPOSSIBLE (threaded through the emit loop + `build_graph`). This severs ONLY the
optimistic turn edge — every OTHER `NOT ArtariaSARotatable` use in the room is
additionally AND-ed with positive `HighDanger` (already impossible for us), so no
legitimate pre-rotate leniency is lost. The pickup then falls onto its DURABLE
post-rotation access (the Gravity/Space climb `Under-left-blocks → Total Recharge`,
or an alternate entrance). Verified: severed edge is IMPOSSIBLE in the graph; A/B
from the elevator side with the user's kit (reachable True→False); full-loadout
oracle `unreachable_pickup_locations` = 0 (no `accessibility: full` regression);
pickup still reachable at full loadout. `assert_rotatable_model_soundness`
tripwire (called from `main`) trips on stale curation or a silent no-op (upstream
restructuring the turn edge). STILL OPEN: Ghavoran Flipper Room
(`GhavoranSupersRotatable`) is the same class but its turn edges are gated on
`misc:Teleporters`/grapple-box events (not HighDanger) — RDV even warns "would
always lead to an incompletable game" and it interacts with transport rando — so
it needs its own analysis before curating. NOTE: `scripts/graph_to_compiled_rules.py`
is a STALE offline tool (expects graph schema 5; graph is now 7) — the RUNTIME
consumes `logic_graph.json` directly via `graph_logic.py` (schema 7), so this fix
lands through extraction + `graph_logic`, NOT `compiled_rules.json`. Needs
`python scripts/extract_dread_rules.py --all` to regen the gitignored graph.
Tests: `test_extract_dread_rules` (curation, turn-edge severed only with BOTH
atoms pessimistic, HighDanger-only insufficient, positive-event untouched,
soundness passes/stale/no-op). See [[dread-one-way-rotatable-flippers]].

UPDATE (generalized `/warp <region>` — the chosen runtime fix for the thermal
trap): the thermal toggle can still strand a player at RUNTIME (activate the
lower device → both `NOT deviceheat_002` exits from `Past Magnet Floor` close;
the only other exit needs the spider magnet floor lowered, unreachable from
there). Plain `/warp` (to start) relocates Samus out of Cataris but CANNOT
restore a lost traversal capability — if your route to a region went through the
now-severed thermal path, AP never reconsiders the dead edge. Decisive topology
fact: Dairon (the hub this trap most often cuts) has NINE transport entrances
from 5 regions incl. a non-rando Cataris↔Dairon teleporter, so a truly
*unwinnable* seed is very unlikely — the trap is almost always recoverable by
re-routing, just non-obvious. So instead of a logic change (edge removal / a
per-seed region-access guard — both considered, both heavier and the former
reverses the "no softlock prevention in logic" stance), `/warp` was generalized:
`/warp <region> [station]` reloads at a specific save station the player has
VISITED (`/warp dairon`, `/warp dairon west`); `/warp list` shows the recorded
targets; `/warp` (no arg) is unchanged (warp-to-start). It restores access you
ALREADY had — only save stations the client has SEEN the player stand in are
eligible. Tracking is per-STATION (not just per-region): the wire reports only
the scenario, but `warp_guard.lua` already tracks the live collision-camera
(`RL.CurrentScenario`/`CurrentSubArea`), so `_poll_once` reads it each tick
(`build_read_current_subarea_lua`) and records `(scenario, camera)` into
`DreadContext._visited_saves` when it matches `protocol.SAVE_STATION_BY_CAMERA` —
the 17 real save platforms (`protocol.SAVE_STATIONS`, a SUBSET of
`SAVE_STATION_CAMERAS`, which also lists access/tunnel rooms with no spawn
point), each carrying scenario + camera + spawn actor (functional ids from RDV's
logic; Hanubia omitted — no save station). NO warp_guard.lua change was needed
(it already tracked the camera), and the client OWNS the visited set so it
survives a Lua re-bootstrap on reconnect. Implementation:
`protocol.build_warp_src(scenario_lua, actor_lua)` parameterizes the old inline
warp Lua (the boss/Nav/save/interaction guards all inspect the CURRENT location,
so they apply to any destination); `_warp_to_start` and `_warp_to_region(region,
station)` both delegate to a shared `_warp(...)` carrying the
inventory/cursor/collected rewind. Same `Game.LoadScenario` primitive → no new
hardware-validation surface beyond the spawn-actor names. Residual gap: a save
station passed through faster than one 2 s poll tick may not be recorded (same
flavor as warp_guard's "fresh-connect subarea nil" gap). Tests: `test_warp`
(unknown region, never-visited vs visited-but-not-at-a-save refusals,
single-visited warp loads the right scenario/spawn, ambiguous-lists /
keyword-disambiguation, /warp list, visit recorded from a poll subarea read,
current-location guards still apply).

UPDATE (nav + map stations are now `/warp` targets too): the original `/warp
<region>` only offered the 17 save stations. But EVERY "save-ish" room in Dread —
save station, Navigation ("Adam") station, Map station — is a
`valid_starting_location` node in RDV's logic, so each already carries a real
`start_point_actor_name` (access-point / map-room weight plate) that
`Game.LoadScenario` can land Samus on — the spawn actor was the ONLY thing that
made save stations special. So nav (11) + map (6) stations were added with zero
RE work: the `SaveStation` dataclass is generalized to `WarpTarget` (adds a
`kind` ∈ {save,nav,map} + a kind-prefixed `label` — "Save East" / "Nav North" /
"Map"), `protocol.WARP_TARGETS{,_BY_CAMERA,_BY_REGION}` unify all 34 (an
import-time assert proves the per-scenario cameras are disjoint, so a camera key
maps to exactly one target), and `_visited_saves` / `_record_room_visit` /
`_warp_to_region` / `_warp_list` key off the unified tables. Hanubia (s080) gains
its ONLY warp target via its nav station (no save station there); map stations
cover the 6 regions with a map console. Visit detection was already half-built —
nav-room cameras already fire `_record_room_visit` for hint registration, so a
nav visit now ALSO records the warp target; map cameras were added to the same
poll signal. `/warp dairon nav north` / `/warp dairon map` disambiguate by
matching the kind-prefixed `label` (so Artaria's "Save North" vs "Nav North" are
separable); `_cmd_warp` now joins `args[1:]` so multi-word station names work.
Warp guards are destination-agnostic — warping TO a nav/map room is safe (you land
ON the platform; the Adam/map box only appears on activation) — and map rooms were
ALSO added to the warp-OUT guard (`RL.IsInMapRoom` / `RL.MapRooms` /
`MAP_STATION_CAMERAS` / `build_map_rooms_lua_table`, mirroring nav/save: a safe hub
you can walk out of, blocking costs nothing, and it prevents stranding the map
overlay). The `valid_starting_location` enumeration also surfaced 6 Map-room start
points, 1 Intro-Room start, and the Itorash→Hanubia elevator landing as spawnable;
the last two were DELIBERATELY left out (the Itorash escape-capsule landing is the
post-Raven-Beak corruption risk — see the capsule trap above; the Intro Room is
just the seed start). `SaveStation`/`SAVE_STATION_BY_CAMERA`/`SAVE_STATIONS_BY_REGION`
remain as back-compat aliases. Tests: `test_protocol` (map table shape/barewords,
4-way disjointness, in_map guard, nav/map targets + Hanubia-via-nav + label
distinctness), `test_warp` (nav/map spawn loads, save-vs-nav kind disambiguation,
Hanubia nav-only, nav/map visit recording, in_map block), `test_bootstrap`
(RL.MapRooms block + IsInMapRoom).

UPDATE (visited-warp set now PERSISTS across a client restart, via AP
DataStorage): `_visited_saves` was in-memory only — it survived a Switch
reconnect (client-owned, re-bootstrap-safe) but a full CLIENT restart started
empty, so a restarted client had to re-walk to each station before it could
`/warp` there. That's the wrong failure mode: the recovery use case is exactly
when you're STUCK and may be unable to re-reach a station. So the set is now
mirrored into AP server DataStorage (the client had NO prior DataStorage usage —
this introduces it). `_on_connected` binds `_warp_visited_key =
f"dread_warp_visited_{seed}_{team}_{slot}"` (seed-scoped so it never restores a
DIFFERENT seed's stations — and a seed change now also CLEARS the in-memory set,
fixing a latent pre-existing cross-seed leak) and `set_notify`s it; the resulting
`Retrieved` repopulates `_visited_saves` (`_absorb_visited_storage` merges
server→local, dropping any pair that isn't a current `WARP_TARGET_BY_CAMERA` key),
and `SetNotify` keeps multiple clients on the same slot in sync. Each new local
observation in `_record_room_visit` writes the full set back via
`Set`/`replace` (`_persist_visited`; ≤34 entries, tiny). Loop-safe: our own `Set`
uses `want_reply=False` and the `SetReply` handler MERGES ONLY (never re-persists);
`Retrieved` persists once iff local holds entries the server lacked (observed
while AP was disconnected → push-back). `send_msgs` self-guards when the AP socket
is down, so persistence is a silent no-op offline. Pure-client change — no
slot_data/world_version/wire impact. The conftest `CommonContext` stub was
extended (`team`, `stored_data`, `stored_data_notification_keys`, `set_notify`) to
match the surface now used. Tests: `test_warp` (persist-on-visit, restore-on-
Retrieved, SetReply merge, unrelated-key/garbage ignored, local-only push-back
flag, restart round-trip→warp), `test_context_e2e` (connect subscribes the
seed+slot key, seed change clears + rekeys). Residual: `replace` (not a CRDT
merge) means two clients writing in the same instant can clobber — self-heals on
the next reconnect's Retrieved; acceptable for a recovery feature.

UPDATE (Nav Station hints → free AP server hints on visit): the in-game Nav
Station ("Adam") plaques already showed REAL cross-world placement facts
(`World._generate_nav_hints` bakes them post-fill — "Your Grapple is at P2's Foo",
DNA hints, etc.), but they were never registered as AP SERVER hints because, as
the docstring said, "Nav Stations have no AP location check, so there is no
in-game event to gate the broadcast on." The save-station/Nav visit detection we
built (poll reads `RL.CurrentScenario`/`CurrentSubArea`) IS that event. So now:
each generated plaque also carries `loc = [owner_slot, location_id]`
(`_hint_loc`); slot_data's `nav_hints` ride it; the client indexes them by the
room that shows each (`protocol.NAV_HINT_STATIONS` — the 11 plaques' `(scenario,
camera)` in patcher-template order, resolved via the access-point actor→camera
map, all also in `NAV_ROOM_CAMERAS`) into `DreadContext._nav_hint_by_camera`; and
`_record_room_visit` (the renamed `_record_save_station_visit`, now doing BOTH
save-station recording and this) registers the revealed location as a FREE AP hint
the first time the player reaches that room. FREE is load-bearing and verified
against AP `MultiServer.py`: own locations → `LocationScouts` `create_as_hint=2`
(universal); a location in another world (an item hint, allowed only because the
item there is ours) → `CreateHints {player: owner, locations:[id]}` — neither
touches `get_hint_cost`. `_registered_nav_hints` dedupes re-visits; failures never
break the poll. NO start-time broadcast (the existing
`test_nav_hints_do_not_register_ap_server_hints` still holds — `start_hints`/
`start_location_hints` stay empty; registration is purely runtime-gated on
physically reaching the station). Residual gap: same as save tracking — a station
passed through faster than one 2 s poll tick isn't caught. Tests: `test_warp`
(map build + own→LocationScouts + cross-world→CreateHints + dedupe + non-nav
no-op), `test_nav_hints` (`loc` present + points at a real filled location).

The real-hardware end-to-end integration smoke is DONE: validated on real hardware
(and Ryujinx) — the bootstrap loads on the live 2.1.0 ROM, items pop, and checks
register. The manual validation gate is cleared; the counter/cutscene semantics
were already settled from source. Remaining items are flavor polish (e.g. RDV's
"highly dangerous logic" toggle is not exposed).

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

UPDATE (romfs version pre-flight + visible patcher status): two fixes for the
"Patcher : ready" → raw `ValueError: Not a valid version!` traceback a user hit
on AP-connect (open-dread-rando → mercury-engine-data-structures
`version_validation.identify_version` MD5s `system/files.toc` and matches it
against its `GameVersion` table; an unrecognized hash aborts the patch).
(1) `patcher_pipeline.verify_romfs_version` mirrors that check up-front
(`KNOWN_DREAD_TOC_HASHES` = the 4 shipped Dread TOC md5s: 1.0.0/1.0.1/2.0.0/
2.1.0) so `patch()` fails fast — BEFORE spawning the subprocess — with an
actionable message (missing TOC ⇒ "not a romfs"; unknown hash ⇒ "not a clean
retail dump: already-patched/mod-output/incomplete, or your MEDS predates this
version"). `_patcher_error_hint` also translates a raw "Not a valid version!" on
the subprocess stderr, covering the table-drift case where our pre-flight passed
but the installed MEDS still rejected the romfs. (2) Patch outcome is now
surfaced on an ALWAYS-VISIBLE top-bar "Patcher" pill (next to the Switch pill,
visible on every tab) that turns RED on failure; clicking it opens
`PatcherPopup` with the full error (`state.set_patch_status` + snapshot
`patch_status_{level,msg}`; `display.format_patcher_pill`/`patcher_pill_color`/
`format_patcher_detail`). `_run_patch` also mirrors a one-line failure pointer to
the `"Client"`→Archipelago tab (the default-visible log), so a failed auto-patch
is noticed without opening the Dread tab. Tests: `test_patcher_pipeline_output_path`
(version pre-flight + error-hint), `test_display` (pill colors/labels/detail).

UPDATE (Universal Tracker support): SHIPPED. UT recomputes a slot's reachable
set by re-running generation from slot_data in a single-player "fake" regen with
a DIFFERENT RNG stream. Our world rolls a CUSTOM per-seed region graph in
`World.generate_early` (door-lock rando `DoorRando.roll_assignments`, transport
rando `TransportRando.roll_connected_matching`, start-area `_start_comp`), so a
naive UT regen re-rolled a degenerate graph and reachability collapsed to a
near-empty set (only item-only-reachable spots survived — the reported "1 in-logic
location" bug). Fix (marioland2/tunic idiom): `World.interpret_slot_data` (static,
returns slot_data → triggers UT's passthrough regen); `fill_slot_data` exports a
new additive `ut_state` key = `{options, dock_assignments, transport_matching,
start_comp, start_patcher, start_extra_items}` (`_ut_state`; options = every
Dread-specific option value so the regen matches even with NO player YAML — common
options stay UT-owned); `generate_early` early-returns through `_restore_ut_state`
when `re_gen_passthrough["Metroid Dread"].ut_state` is present, restoring the
rolled decisions instead of re-rolling (`self.random` untouched). `graph_logic.
set_graph_rules` skips the random `prefer_bosses` locked-DNA placement when
`multiworld.generation_is_fake` (DNA never gates region access — only the
completion condition — so the reachable set is identical, and skipping avoids an
RNG-divergent placement colliding with UT's multidata overlay). Normal generation
is byte-identical (the restore branch only fires under `re_gen_passthrough`, never
set in real gen). The `ut_state` key is additive/slot_data-only (NOT in the CLI
placements JSON) and the client ignores it — client contract intact. Tests:
`tests/test_ut_tracker.py` (AP-gated) — round-trip byte-equality of all restored
fields + option restore from defaults, and a door-sensitive reachability check
(regen reproduces the seed's 94-location set, NOT a different seed's re-roll).
PopTracker map-tab (`tracker_world`) integration is NOT done (optional; not needed
for the in-logic-set fix).

UPDATE (UT `/explain` + `/get_logical_path` now human-readable): SHIPPED. UT's
default explain introspects the world's access rules — ours are opaque lambdas
(`Rules.compile_to_lambda`) on entrances named `e{i}` between regions named
`R{comp}`, so UT printed internal nodes (`R37 (True): e5 : True`). UT exposes
three optional world hooks (probed via `hasattr`, see the cloned
`worlds/tracker/TrackerClient.py` + `docs/apworld-integration.md`):
`explain_rule(target_name, state)` (→ `/explain`), `explain_path(entrance, state)`
(per-hop rendering inside UT's default `/get_logical_path` walk), and
`get_logical_path` (full override — NOT implemented; we let UT's `state.path`
BFS drive and only style each hop). We render our OWN symbolic rule ASTs (the
dicts `graph_logic` feeds to `compile_to_lambda`) into `JSONMessagePart` lists,
coloring each atom green/salmon by evaluating it via `compile_to_lambda` itself
(so the explanation can't drift from the logic). NO rule_builder migration —
that would force the real access rules to become `Rule.Resolved` objects and
change the proven generation path; the hooks avoid all of it. New
`apworld/dread/ut_explain.py` (`render_ast` + `ExplainCtx` + `build_comp_labels`,
AP-import-free, unit-tested with a fake state) holds the renderer; `DreadWorld`
gains `explain_path`/`explain_rule`/`_explain_ctx`/`_region_label`;
`graph_logic.build_regions` stashes `world._explain_asts` (name→resolved AST for
non-trivial `e{i}`) + `world._explain_labels` (comp→human label). Atom text:
item→name(`×N`), event→`reach <name>`, trick→`trick: <long> [level]` (green iff
enabled this seed), sum→`Missiles/Power Bombs ≥ N`, damage_threshold→`survive N
dmg (<suits> or energy)`, and/or→`(a & b)`/`(a | b)`. **Graph schema 6→7**: new
`comp_regions` (every component's RDV region name) so a hop labels by region
("Artaria") instead of the bare `R{comp}` when it has no pickup/event; pickup
names still win. `build_comp_labels` layers pickup > event > start > region.
Event names are humanized by `ut_explain.humanize_event`: CamelCase RDV events →
spaced words with a few expansions (`ArtariaEmmiMagnetPlatformLowered` → "Artaria
EMMI Magnet Platform Lowered", CU→Central Unit); RDV's unnamed
`scenario:subarea:actor` fallback ids → "<Region>: <cleaned actor>" via a baked
`_SCENARIO_REGION` map (s010_cave→Artaria … 9 scenarios, all functional Dread
ids, safe to commit). Residual: genuinely opaque door-barrier actor codes
(`PRP_DB_CV_008` → "Artaria: PRP DB CV") can't be decoded further without a
hand-curated 100+ actor dictionary — region-grouped is the ceiling; the
requirement text and region hops are fully readable. NOTE: needs
`python scripts/extract_dread_rules.py --all` to regen the gitignored
`logic_graph.json` at schema 7 (conftest auto-regens when the script is newer).
Tests: `tests/test_ut_explain.py` (AP-free renderer matrix + `build_comp_labels`
layering + gated real-world `explain_path`/`explain_rule` through
`setup_multiworld`); `test_graph_logic` schema pin 6→7 + `comp_regions` shape.

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
