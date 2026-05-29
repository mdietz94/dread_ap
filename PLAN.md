# Metroid Dread Archipelago (modded Switch) — Implementation Plan

## Context

You want a Metroid Dread Archipelago integration that runs on modded Switch hardware, following the pattern proven by [smo_archipelago](../../Documents/smo_archipelago) (CLAUDE.md, README.md). That project required ~6 months of work largely because the SMO side had to author its own subsdk9 module in C++, hook ~47 mangled symbols via LibHakkun + sail, and re-derive offsets in Ghidra against `main.nso` for cutscene patches.

**The headline finding of this plan: Dread is in a fundamentally different starting position.** Randovania has already shipped the runtime hook layer for live multiworld communication (the `open-dread-rando-exlaunch` sysmodule, written on top of [Emufartz/dread-hook](https://github.com/Emufartz/dread-hook)). That sysmodule exposes a TCP socket on port **6969** that accepts arbitrary Lua, executes it inside Dread's own Lua runtime via a bootstrapped `RL` (Randovania Lua) namespace, and returns the result. `RL.ReceivePickup`, `RandomizerPowerup.GetItemAmount`, blackboard reads for collected-location bitfields, and `Game.GetCurrentGameModeID` are all already callable from over the wire. **The "ideal scenario" you described — existing rando has the hooks, we just write networking — is the actual situation.** Treat the C++ / Ghidra column of the SMO project budget as essentially zero for Dread.

The work, then, is a Python translator that bridges Archipelago's protocol to that existing Lua-eval socket, plus a patcher-side seed format that open-dread-rando can consume. The MuratDev41 fork [open-dread-rando-ap](https://github.com/MuratDev41/open-dread-rando-ap) attempted exactly this; per the discord excerpts it stalled on `starting_location` patching and exefs naming, but the architectural intent was right. We continue that direction with a soft fork that credits upstream and targets Dread 2.1.0.

## Architecture

Three tiers, mirroring the SMO project but with **no Switch-side C++ code of our own**:

```
[ Switch / Dread 2.1.0 ]  <--TCP/binary LAN-->  [ PC Client (Python, inside apworld) ]  <--ws-->  [ AP server ]
   exlaunch sysmodule                              DreadContext(CommonContext)                      archipelago.gg
   (UPSTREAM — Randovania)                         Kivy GUI (Tracker + Connections)
   - bootstraps RL.* Lua namespace                 LuaProtocol on port 6969
   - opens TCP :6969                               Forked apworld machinery
   - executes arbitrary Lua, returns result        Translates AP items ↔ RL.ReceivePickup
   romfs/                                          Translates RL collected-bitfield ↔ AP CheckLocations
   (open-dread-rando patcher output)
   - per-seed item placements (bmsad/bmsld)
   - starting inventory / teleporter shuffle
```

Versus SMO, the **bottom-left tier collapses** (no LibHakkun, no Ghidra-derived symbol DB, no NSP-targeted addresses, no subsdk build). The **top-right tier expands slightly** because the Lua-eval protocol is more verbose than SMO's hand-rolled JSON — every `gotItem` event is a Lua poll, not a synchronous push.

## Strategy

**Decisions locked in via clarifying questions:**
- Target version: **Dread 2.1.0** (newest, already dumped per discord 4/11)
- Community posture: **soft fork with upstream credit** — fork [open-dread-rando](https://github.com/randovania/open-dread-rando) and [open-dread-rando-exlaunch](https://github.com/randovania/open-dread-rando-exlaunch) into this repo, keep LICENSE + attribution headers, document the AP-specific diffs in a CHANGES.md, plan to file occasional upstreamable bug-fix PRs back

**Build order** (each phase is shippable on its own):

1. **Wire-up validation (no apworld yet)** — Install upstream exlaunch sysmodule on Switch, write a 50-line Python TCP client that handshakes on :6969 and runs `RL.GetInventoryAndSend()`. Goal: confirm we can read Dread state from PC end-to-end on real hardware. This is pure de-risking — we touch nothing of our own code first.

2. **Patcher fork — open-dread-rando-ap** — Soft-fork open-dread-rando. Replace its Randovania-shaped seed-input format with an AP-shaped one (item_pool, location_pool, starting_items, starting_location from AP slot_data). Fix the `starting_location` regression Murat hit (likely a schema change in Randovania's `PatcherData` between versions — diff against upstream's 2.1.0-pinned commit). Output: a romfs/ tree the existing exlaunch sysmodule can serve from `/atmosphere/contents/<title-id>/romfs/`.

3. **PC client — apworld + Python bridge** — Lift the entire [apworld/smo_archipelago/client/](../../Documents/smo_archipelago/apworld/smo_archipelago/client/) skeleton (CommonContext subclass, Kivy GUI, asyncio TCP, replay-on-reconnect, scout cache). Replace SMO's line-delimited JSON protocol with a `LuaProtocol` class that speaks the exlaunch binary framing: `[1-byte type] [1-byte req#] [4-byte LE length] [Lua source]`. Reuse: `context.py`, `gui.py`, `state.py`, `datapackage.py`, `scout_cache.py`, `discovery.py`, `commands.py`, `setup_state.py`, `net_util.py`. Replace: `switch_server.py` → `lua_executor.py`, `protocol.py` → `lua_packets.py`.

4. **Apworld content — items, locations, regions, rules** — Hardest content task. Dread has ~100 pickup locations across 6 areas. Mine [randovania/games/dread](https://github.com/randovania/randovania/tree/main/randovania/games/dread) for the canonical item/location list (their `data/json_data/` is the authoritative source). Translate to AP `items.json` / `locations.json` / `regions.json` / `Rules.py`. Reuse SMO's hash-based ID derivation pattern (`game_table` + `creator` → polynomial hash).

5. **Goal detection** — Hook `RL.UpdateRDVClient`'s periodic poll to read `Init.bBeatenSinceLastReboot` (already exposed by exlaunch). When it flips true, send AP `StatusUpdate{ClientGoal}`. Mirrors SMO's `CreditsStartHook` but trivially simpler since it's pure Lua state.

6. **Tracker** — PopTracker pack, port the SMO pack's layout + Lua-rules-port pattern. Punt to v0.2 if needed.

## Components to build vs reuse

| Component | Source | Effort |
|---|---|---|
| Switch sysmodule | UPSTREAM open-dread-rando-exlaunch, install as-is | 0 |
| Lua hook namespace (`RL.*`) | UPSTREAM bootstrap_part_{0..3}.lua, install as-is | 0 |
| Ghidra/symbol work | NONE — exlaunch already did it | 0 |
| RomFS patcher | FORK open-dread-rando, change input schema | 2-3 weeks |
| Mercury Engine file IO | UPSTREAM [mercury-engine-data-structures](https://github.com/randovania/mercury-engine-data-structures) pip dep | 0 |
| PC client framework | LIFT from smo_archipelago/apworld/smo_archipelago/client/ | 1 week |
| LuaProtocol (binary framing + req-resp matching) | NEW, ~200 LOC | 3 days |
| Apworld data (items/locations/regions/rules) | DERIVE from randovania/games/dread JSON | 2 weeks |
| Goal detection wiring | NEW, ~30 LOC | 1 day |
| PopTracker pack | PORT pattern from smo_archipelago/poptracker | 1 week |

**Total estimate: ~6-8 weeks of focused work for v0.1-alpha.** Compare to SMO's ~6 months. The compression is entirely due to exlaunch eliminating the Switch-side C++ tier.

## Critical files to read/reference

- [Documents/smo_archipelago/CLAUDE.md](../../Documents/smo_archipelago/CLAUDE.md) — the brief for the parent pattern; everything in the "Apworld component", "Bridge component", and "Decisions already made" sections transfers
- [Documents/smo_archipelago/apworld/smo_archipelago/client/](../../Documents/smo_archipelago/apworld/smo_archipelago/client/) — the entire reusable PC-client skeleton
- [Documents/smo_archipelago/apworld/smo_archipelago/__init__.py](../../Documents/smo_archipelago/apworld/smo_archipelago/__init__.py) — Component registration + SuffixIdentifier pattern for the launcher hookup
- [Documents/smo_archipelago/docs/wire-protocol.md](../../Documents/smo_archipelago/docs/wire-protocol.md) — pattern for documenting our LuaProtocol
- Upstream [randovania/open-dread-rando](https://github.com/randovania/open-dread-rando) — the patcher to fork
- Upstream [randovania/open-dread-rando-exlaunch](https://github.com/randovania/open-dread-rando-exlaunch) — the sysmodule to install (no fork needed unless we hit gaps)
- Upstream [randovania/randovania/game_connection/connector/dread_remote_connector.py](https://github.com/randovania/randovania/blob/main/randovania/game_connection/connector/dread_remote_connector.py) — reference implementation of the LuaProtocol; **lift the wire format directly from this file**
- Upstream [randovania/randovania/game_connection/executor/dread_executor.py](https://github.com/randovania/randovania/blob/main/randovania/game_connection/executor/dread_executor.py) — the binary framing reference (port 6969, packet types, handshake)
- Upstream [randovania/randovania/games/dread/](https://github.com/randovania/randovania/tree/main/randovania/games/dread) — canonical item/location/region data for the apworld

## Risks (ranked by severity)

1. **`open-dread-rando-ap` was abandoned for a reason — the patcher integration is the actual hard part.** Murat got the wire layer running but hit `starting_location` and exefs naming bugs. Most likely root cause: open-dread-rando's `PatcherData` schema is tightly coupled to Randovania's seed shape (PickupTarget/PickupModel objects, dread-specific elevator data, etc.) — flatly translating AP item_pool to it loses required fields. **Mitigation**: in Phase 2, write an adapter layer that constructs a full `PatcherData` from AP slot_data + a hardcoded "AP filler" template for the Randovania-specific fields we don't care about. Don't try to rewrite open-dread-rando from scratch.

2. **Lua-eval poll latency.** Unlike SMO's push protocol where the Switch sends `{"t":"check"}` the moment a moon collects, the exlaunch model is poll-driven: client sends `RL.GetCollectedIndicesAndSend()` periodically (Randovania's `UpdateRDVClient` runs every 2s via `Game.AddSF`). For 100-location seeds this is fine; for tight async scenarios (deathlink across worlds) the 2s window is the floor. **Mitigation**: accept the 2s p99 for v0.1. Push-style optimization is a v0.2 if needed (would require a sysmodule patch).

3. **Cutscene-blocked item delivery.** Randovania's `RL.GivePendingPickup` documents a retry loop because `Scenario.QueueAsyncPopup` no-ops during cinematics / user-input-disabled states. AP items received during a cutscene queue but don't deliver until the player regains control. **Mitigation**: same as Randovania — implement the pending-queue + post-HELLO replay pattern (the smo_archipelago client already has this pattern in `state.py`; lift it).

4. **Upstream version churn.** exlaunch's `RL.*` API surface is undocumented and may change. The 2.1.0 Lua bootstrap files (bootstrap_part_{0..3}.lua) are essentially our API contract. **Mitigation**: pin to a specific exlaunch commit hash in our docs, ship our own copy of the bootstrap files under romfs (we can override Randovania's), and run a smoke-test ping/pong on connect that asserts `RL.Version == X` before declaring the link healthy. This is what `dread_executor.py`'s handshake does — copy it.

5. **Community friction (the discord LuigiPollen / ToS-Infringing Muffin thread).** Soft-forking after that thread is borderline — people are watching. **Mitigation** (per your chosen posture): credit upstream prominently in README, license headers, and the apworld metadata; file bug-fix PRs back when we find genuine bugs; do not solicit playtesters in the Randovania discord until v0.1 is public.

6. **`starting_location` regression specifically.** Per the chat this is what bricked Murat's branch. Likely a Randovania 2.x schema change — `starting_location` went from a string id to a `NodeIdentifier` tuple. **Mitigation**: in Phase 2, before any AP work, reproduce upstream open-dread-rando's tests against a vanilla Randovania seed to confirm the patcher even works standalone; only then layer on the AP adapter.

7. **Switch keys / IP exposure.** Same risk as smo_archipelago. Gitignore RomFS extractions, BMSAD/BMSLD dumps, prod.keys. Reuse smo's CLAUDE.md "Never commit Nintendo IP" section verbatim — the list applies identically to Dread asset files (BFRES, BCSKLA, BWAV, etc.).

## What we are explicitly NOT doing

- **No subsdk module of our own.** If exlaunch turns out to lack a hook we need, the right move is a patch upstream, not a parallel module. Only revisit if a true blocker emerges.
- **No Ghidra work.** If we end up reaching for it the plan needs revisiting — that signals exlaunch is insufficient for this purpose.
- **No deathlink / hints / traps for v0.1.** Land item flow + goal detection first, ship, then iterate. Same MVP discipline that worked for SMO M0-M7.
- **No emulator-only target.** Like SMO, we test on Ryujinx for dev iteration but the production target is real hardware (Atmosphere CFW).

## Verification plan

End-to-end smoke test on real hardware (mirrors smo_archipelago/.claude/skills/smo-loopback-test):

1. **Phase 1 done** — Boot Dread 2.1.0 with upstream exlaunch installed. Run a Python REPL that connects to `<switch-ip>:6969`, completes the handshake, runs `RL.GetInventoryAndSend()`, prints the returned JSON inventory. PASS if non-zero Missile count appears after picking up a Missile Expansion.

2. **Phase 2 done** — Generate an AP seed, run our forked patcher to produce a romfs/ tree, install to `/atmosphere/contents/010093801237C000/romfs/`, boot Dread, confirm starting_location matches the AP slot_data and the first item pickup grants the AP-assigned item (not the vanilla Missile).

3. **Phase 3 done** — Run the apworld client + AP server + a 2-player seed (Dread + any other game). Confirm: (a) collecting a location in Dread sends `LocationChecks` to AP server, (b) AP item arriving from the other player triggers `RL.ReceivePickup` and the inventory increments, (c) crash-restart Dread mid-session, reconnect, missed items replay correctly.

4. **Phase 5 done** — Beat the final boss, confirm `Init.bBeatenSinceLastReboot` flips and AP server records `Goal Completed`.

5. **Regression** — `pytest apworld/dread_archipelago/tests/` mirrors the smo test layout (host-side, no Switch needed). Bridge wire-protocol tests, datapackage tests, Rules.py logic tests.

## Open question to revisit after Phase 1

If Phase 1 ping/pong fails on real hardware (vs. Ryujinx), the entire plan needs reassessment — we'd be looking at either a real hardware regression in exlaunch (file an upstream issue, stall here) or a deeper compatibility issue with FW version or Atmosphere version. Don't go past Phase 1 without a green light on real hardware.
