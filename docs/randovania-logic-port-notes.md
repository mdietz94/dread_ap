# Randovania logic port — Milestone 1 notes

Companion to [randovania-logic-port.md](randovania-logic-port.md) (the
plan). This is the post-milestone retrospective: what shipped, what
didn't, and what Milestone 2 needs to budget for.

## Update — all 9 areas compiled (post-M1)

After Elun shipped, ran `--all`. Two extensions needed; both small.

1. **New `node` requirement type.** Artaria's Melee Tutorial Room uses
   `{"type": "node", "data": {...}}` for "have you previously visited
   this pickup node?" — a back-path device for the Morph Ball cul-de-
   sac. Treated as `IMPOSSIBLE` so disjuncts like `Morph OR node(...)`
   correctly collapse to `Morph`. Real visit-history modeling is M2.

2. **Algorithm swap to DNF-frozenset fixed-point.** The bounded-DFS
   was fine for Elun (68 nodes) but timed out on Artaria (~280 nodes).
   Replaced with a Bellman-Ford fixed-point over DNF (frozenset of
   frozensets of `(item, name, amount)` atoms). Frozensets are
   hashable + comparable in O(1), so the simplification + dedupe
   loops are orders of magnitude faster than re-canonicalizing nested
   ASTs. All 9 areas now compile in <10s total.

3. **Actor-name matching fix.** Initial `lower().replace("_", "")`
   normalization collapsed `Item_MissileTank001` and
   `item_missiletank_001` (genuinely different actors) into one key
   — dropped 6 rules. Switched to exact (case-sensitive,
   underscore-preserving) matching after verifying that all 137
   actor names match exactly between our `locations.json` and
   Randovania's `extra.actor_name`.

Results:
- **137/137 actor pickups have rules** (was 5/137 for Elun-only).
- **0 impossible**, 3 trivially reachable (early Artaria pickups).
- **115 events** referenced across all rules (was 3 for Elun).
- Generation smoke test still succeeds under `accessibility: minimal`.
  Under `accessibility: items` it now blocks on 22 pickups whose
  area-isolated rules require items the AP solver can't reach without
  cross-region traversal — that's exactly the M2 cross-region-access
  signal.
- 134 tests pass (was 111). New ones in
  [test_all_regions_rules.py](../apworld/dread/tests/test_all_regions_rules.py):
  per-region coverage + non-impossible + late-game-reaches-all + 4
  hand-vetted per-region assertions.

## What shipped (M1 deliverables)

| Artifact | Status |
|---|---|
| `scripts/cache_randovania_logic.py` | ✓ pulls 10 files (header + 9 areas) from upstream `3559136dc44`, pins commit |
| `scripts/extract_dread_rules.py` | ✓ compiles Randovania req-tree → serializable AST |
| `apworld/dread/data/compiled_rules.json` | ✓ Elun (5 pickups) + victory + event list |
| `apworld/dread/data/events.json` | ✓ 3 events used (ElunSoldier, Ship, grapplepulloff1x2_000) |
| `apworld/dread/Rules.py` | ✓ AST → `state.has(...)` lambda → `add_rule` |
| `apworld/dread/tests/test_rule_compiler.py` | ✓ 18 tests on lambda compiler |
| `apworld/dread/tests/test_elun_rules.py` | ✓ 10 wiki-vetted Elun assertions |
| `scripts/tests/test_extract_dread_rules.py` | ✓ 20 tests on AST builder + simplifier |
| `scripts/install_apworld.py` | ✓ installs into sibling AP checkout (folder or .apworld mode) |
| `scripts/ap_generate.py` | ✓ wraps `Archipelago/Generate.py` |
| Generation smoke test | ✓ Dread-only seed (149 items / 149 locations) generates clean with `accessibility: items` |
| All tests | ✓ 111 pass (63 pre-existing + 48 new) |

## Pickup-to-rule sample (Elun)

| Pickup | Compiled rule (simplified) | Source — Elun.txt |
|---|---|---|
| Energy Tank | `Plasma Beam AND Morph Ball AND (Bomb \| Cross Bomb \| Power Bomb)` | Only entry to Ammo Recharge Station from canonical-route side is the Plasma Beam Door from Purple Drapes |
| Plasma Beam pickup | OR: Lower path (`Morph + Bomb + Missile Tank + (Morph \| Slide)`) ∪ Upper path (`Morph + Bomb + Plasma`) | Missile Door from AR Station Lower lets you self-loop and get Plasma without needing it |
| Power Bomb Tank | `Plasma + Morph + bombs + ElunSoldier + grapplepulloff_event + (Bomb \| Cross \| PB)` | Sits in Vertical Bomb Maze, gated by Grapple Block event |
| Missile Tank (Fan Room) | `Plasma + Morph + bombs + Speed Booster + (Morph \| Slide)` | Pickup requires "Speed Booster and Speed Booster Conservation (Beginner) and Can Slide" |
| Missile Tank (Horizontal Bomb Maze) | `Plasma + Morph + bombs + Power Bomb + (Cross Bomb \| Spin Boost \| Space Jump)` | Reached via Power-Bomb-gated upper tunnel into Vertical Bomb Maze |

The Plan's pre-milestone assertion "Energy Tank requires only Morph
Ball" was incorrect — Elun's geometry forces Plasma Beam through Purple
Drapes. The compiler caught this; the wiki confirms it.

## Decisions reaffirmed in flight

- **Pragmatic DFS path enumeration**, not Randovania's fixed-point.
  First attempt was an iterative Bellman-Ford-style fixed-point with an
  OR-width cap. Didn't converge for Elun in 60s — the symbolic AST grew
  faster than the cap could absorb. Swapped in bounded simple-path
  enumeration (max_depth=40, max_paths_per_target=24, then `absorb_or`
  to drop dominated paths). Elun compiles in <1s; should scale linearly
  to other areas since per-area node counts are 60-200 range.

- **Tricks at level 1 = trivial; level ≥ 2 = impossible.** First cut
  treated all tricks as impossible, which made Elun's Fan Room missile
  unreachable. Randovania's level-1 tricks are "Beginner" tier (basic
  shinesparking, simple bomb jumps) that any Dread player knows. The
  user-facing trick-level option is M2.

- **Ammo counts collapse to amount=1.** Randovania models
  `MissileAmmo amount=75` (raw missile count needed); our pool has
  Missile Tanks worth +2 each, with no per-tank tracking. Collapsing
  to amount=1 means "any Missile Tank in inventory" satisfies the
  requirement. Over-permissive but a coherent direction; real ammo
  counting is M2.

- **Damage requirements collapse to suit ownership.** Heat/Lava →
  Varia OR Gravity; Cold → Gravity OR Varia; raw Damage → True
  (player has enough HP assumed); OOB → False (don't encourage
  unintended routes). No E-Tank counting yet.

- **Events evaluate as Trivial in the lambda compiler.** The compiler
  preserves event nodes in the AST so M2 can switch them to real
  `event` items without recompiling. For M1, this means a rule that
  depends on grapplepulloff doesn't actually gate on Grapple Beam in
  AP's reachability check. Documented in Rules.py. The Plan's
  event-items-in-pool approach lands in M2.

- **Cross-region access is M2.** For M1, each area is compiled in
  isolation with entries = "cross-region docks pointing INTO this
  area" (Elun's `Shuttle to Ghavoran`) + any `valid_starting_location`
  node. The per-pickup rule captures what's needed to reach the pickup
  from that entry. Whether the player can ACTUALLY reach Elun from the
  Artaria start is a separate question. With Regions.py still using
  star topology (every region trivially reachable from Menu), this is
  fine for M1 generation; M2 needs to wire real region-to-region
  requirements through cross-region dock weaknesses.

## Known approximations (over-permissive in v0.1)

These all make the AP solver think more placements are valid than they
actually are. Better to have an honest under-approximation, but the
gaps below are documented so M2 can pick them up.

1. **Events evaluate as Trivial.** Solver may place items that need
   Grapple Beam (via grapplepulloff event) without ensuring Grapple
   Beam is logically reachable first.
2. **Cross-region access is unmodeled.** Solver thinks every region is
   reachable from Menu, so it doesn't worry about chaining.
3. **Ammo counts collapsed.** A rule that needs 75 missiles passes
   with any 1 Missile Tank.
4. **Damage thresholds ignored.** A 30-damage cold room with no suit
   passes (we model only "do you have a suit?", not "do you have
   enough HP to take the hit").
5. **Tricks at level 2+ are impossible.** Conservative — some seeds
   might end up unsolvable if the only path involves a level-2 trick.
   Symptom: a pickup compiles to `{"type": "impossible"}`. None of
   Elun's 5 pickups hit this in M1.

## What Milestone 2 needs

Estimated 2 weeks of focused work, in rough order:

### 1. Compile the other 8 areas (1-3 days)

Run `python scripts/extract_dread_rules.py --all`. The DFS is bounded
so it should finish in <60s total. Failure modes to expect:

- Unmapped item names: Artifact1-12 (DNA) currently map to None →
  Impossible. Either add them to items.json as M2 progression items or
  exclude them from the compile target list.
- Larger areas (Artaria, Cataris ~1.4 MB JSON) have many more nodes;
  may need to raise `max_depth` (40 → 80) or `max_paths_per_target`
  (24 → 64).
- Hand-validate ≥3 assertions per area against
  https://metroid.wiki.gg/wiki/Metroid_Dread maps.

### 2. Real event-item plumbing (3-4 days)

Per the Plan §"Step 4: event items":

- Add event item entries to `data/items.json` (append-only — preserve
  AP IDs!) as `progression` class with `quantity: 0`.
- Identify event locations: nodes of `node_type: "event"` in the area
  JSON. Their reach requirement is the event's own rule.
- Append synthetic event locations to `data/locations.json`.
- `World.create_items` adds one event item per event name (count = 1).
- `Rules.set_rules` `place_locked_item`s each event item at its event
  location.
- Switch the lambda compiler's `event` case from `_const_true` to
  `state.has(event_item_name, player)`.

### 3. Cross-region access graph (2-3 days)

The compiler already emits `cross_region_exits` per area (just not
written to output yet). Build a region-to-region adjacency in
`Regions.py`:

- For each area's cross-region exits, look up the dock's open
  requirement (Shuttle docks are always Trivial; doors may have
  weaknesses).
- Wire `Region.connect(other_region, dock_name, rule=predicate)`
  instead of the current `menu.connect(region, name)` star.
- Per-pickup rule still captures what's needed within the area; the
  region edge captures what's needed to enter the area.

### 4. Trick-level UI option (1 day)

Currently hardcoded to "level 1 on, level 2+ off". Add a
`TrickLevel` Choice option to `Options.py` (Beginner/Intermediate/
Advanced), thread it into the compiler as a configuration knob (it
already accepts a `trick_level` parameter — wire it from the World
class at world-creation time). Re-compiling per-seed is fast (<1s).

### 5. Real victory condition (½ day)

Currently `completion_condition = lambda state: True` because the goal
is fully detected on the Switch side. Once events are real, switch to
`state.has("Event: Ship", player)`. Per the Plan, the in-game goal
detection still drives the AP `StatusUpdate{CLIENT_GOAL}`, so this is
purely about making the solver insist on a path to the credits.

### 6. Ammo counting (optional — defer to v0.3?)

The current `_AMMO_OR_TANK_ITEMS = {... amount=1}` collapse is the
cheap option. Real counting means changing the lambda compiler to:

- Sum the per-Missile-Tank ammo count (each Missile Tank in our pool
  = +2 missiles; the item table's `quantity` field already encodes
  this).
- For `MissileAmmo amount=N`, emit `state.count("Missile Tank", player)
  * 2 >= N`.

The benefit is small for v0.2 (no real Dread rooms gate on more
missiles than you'd get from picking up other things), the bug surface
is large. Reasonable to push to v0.3.

## What worked well

- **Pinning the upstream commit.** Future-us reading `compiled_rules.
  json` can grep `pinned_commit` and know exactly which Randovania
  state generated it. No more "wait did this work against the version
  before they renamed Z?".
- **Serializable AST as the artifact.** Decoupling extraction from
  lambda construction kept Rules.py at 100 LOC and made unit-testing
  the primitives trivial (StubState pattern).
- **Bounded DFS over fixed-point.** Convergence-free, deterministic,
  fast on Elun. The plan flagged this as a possible swap; doing it up
  front saved 2 hours of debugging.
- **Reading Elun.txt as ground truth.** Randovania exports a
  human-readable form of every logic file. Way easier to audit
  compiler output against than the JSON.

## What didn't work

- **First fixed-point implementation.** Ran for 60s on Elun without
  finishing. Replaced.
- **Initial trick handling (all tricks off).** Made Fan Room missile
  unreachable; would have failed `test_no_elun_pickup_is_impossible`.
  Fixed by enabling level-1 tricks.
- **First Elun.txt hand-derived rule for Energy Tank.** The Plan
  asserted "Energy Tank requires only Morph Ball" — wrong. The wiki
  confirms Plasma Beam Door is the canonical entry. Compiler caught it.
- **First apworld zip name (`dread.apworld`).** Archipelago imports
  the world as `worlds.<zip-stem>`, but our package is
  `dread/`. Renamed the zip to `dread.apworld`.
- **Loading JSON from inside the zipped apworld.** `Path(__file__).
  parent / "data" / "items.json"` doesn't work for a zip-loaded
  package. M1 sidesteps it by installing the apworld as a folder
  under `worlds/` instead of as a zip under `custom_worlds/`. Switching
  data loads to `importlib.resources` is a small M2 cleanup that
  unblocks `.apworld` distribution.

## Files touched

Created:
- `scripts/cache_randovania_logic.py`
- `scripts/extract_dread_rules.py`
- `scripts/install_apworld.py`
- `scripts/ap_generate.py`
- `scripts/tests/test_extract_dread_rules.py`
- `apworld/dread/data/compiled_rules.json` (generated)
- `apworld/dread/data/events.json` (generated)
- `apworld/dread/tests/test_rule_compiler.py`
- `apworld/dread/tests/test_elun_rules.py`
- `apworld/dread/tests/seeds/dread_smoke.yaml`
- `docs/randovania-logic-port-notes.md` (this file)
- `.dread-cache/randovania-logic/{header,9 areas}.json` + `PINNED_COMMIT.txt` (gitignored)

Modified:
- `apworld/dread/Rules.py` — compile AST → lambda, apply add_rule
- `CLAUDE.md` — bump Status line per the Plan's success documentation

Not touched (per scope constraints):
- `apworld/dread/client/*` — wire layer
- `apworld/dread/data/items.json` — AP-ID stability
- `apworld/dread/data/locations.json` — AP-ID stability
- `scripts/build_patcher_json.py` — patcher adapter
- `apworld/dread/Regions.py` — star topology unchanged
  (cross-region access is M2)
- `apworld/dread/World.py` — no event items yet (M2)
- `apworld/dread/Options.py` — trick-level option is M2


## M2 plumbing retrospective

Gate A landed: events become real AP items, completion_condition reads
`victory_condition` from the compiled artifact, and the smoke seed
generates under `accessibility: minimal`. Gate B (cross-region edges,
trick-level UI, three pre-baked trick-level outputs, fully refreshed
docstrings) is deferred — Gate A on its own takes the project from
"62% of compiled rules silently under-constrained" to "the AP solver
honors the per-pickup logic the compiler produced," which is the
high-impact half. Plan for Gate B: see
[randovania-logic-port-m2plumbing.md](randovania-logic-port-m2plumbing.md).

### What shipped (Gate A)

| Artifact | Status |
|---|---|
| `scripts/extract_dread_rules.py` — per-event reach rules + AP-ID assignment | ✓ emits 184 events with `{name, region, rule, item_ap_id, location_ap_id}` |
| `scripts/append_event_data.py` (new, idempotent) | ✓ appends event items / locations to data tables |
| `apworld/dread/data/compiled_rules.json` schema bump | ✓ `events` is now a list of dicts (was a flat name list); see schema below |
| `apworld/dread/data/items.json` | ✓ +184 event items appended (AP IDs 21554..21737) |
| `apworld/dread/data/locations.json` | ✓ +184 event locations appended (AP IDs 31303..31486) |
| `apworld/dread/Rules.py` event branch | ✓ `state.has("Event: <name>", player)` (was `_const_true`) |
| `apworld/dread/Rules.py::set_rules` | ✓ adds per-event reach rules + `place_locked_item`s the event onto its location |
| `apworld/dread/Rules.py` completion_condition | ✓ now reads `compiled["victory_condition"]` — currently `state.has("Event: Ship", player)` |
| `apworld/dread/World.py::create_items` | ✓ appends one event item per compiled event with classification `progression`; refactored filler math to only count non-event slots |
| `apworld/dread/tests/test_event_plumbing.py` (new) | ✓ 10 tests: structural invariants, AP-ID disjointness, sorted-by-name, victory condition, Burenia event-gated regression |
| `apworld/dread/tests/test_rule_compiler.py` | ✓ `test_event_branch_consults_state` replaces the M1 `_const_true` pin |
| `apworld/dread/tests/test_data_tables.py` | ✓ `test_location_count_is_149` → `test_location_count_matches_table_sum`; per-item/location asserts now skip event entries |
| `apworld/dread/tests/test_all_regions_rules.py` / `test_elun_rules.py` | ✓ LATE_GAME / VANILLA_LATE_GAME dicts now include every event item so the "fully equipped reaches everything" invariant still holds |
| Smoke seed `tests/seeds/dread_smoke.yaml` | ✓ regenerates under `accessibility: minimal` with the M2 pool (149 non-event item slots + 184 locked event items) |
| All tests | ✓ 144 pass (134 pre-M2 + 10 new) |

### compiled_rules.json schema (post-M2)

```jsonc
{
  "pinned_commit": "3559136dc44...",
  "areas_compiled": ["Artaria", ...],
  "victory_condition": {"type": "event", "name": "Ship"},
  "events": [
    {
      "name": "ArtariaCU",
      "region": "Artaria",
      "rule": { /* AST */ },
      "item_ap_id": 21554,
      "location_ap_id": 31303
    },
    ...
  ],
  "rules": { /* loc_name -> AST, unchanged */ }
}
```

`events.json` is kept as a back-compat flat name list. Future
consumers should prefer `compiled_rules.json::events` since it carries
the full per-event metadata.

### AP-ID assignment

Append-only, stable across recompiles:

- `event_item_ap_id[i]   = max(existing_item_ap_ids) + 1 + i`
- `event_location_ap_id[i] = max(existing_location_ap_ids) + 1 + i`

`i` is the position of the event in a sorted-by-name list. Adding a new
event in the middle of the alphabet shifts every event after it by one
AP ID — try not to. Renaming an event has the same effect; treat
event renames as a deliberate seed-bumping event.

### What didn't (Gate B + descope)

| Item | Why deferred |
|---|---|
| Cross-region access in `Regions.py` | Gate B. The compiler already collects `cross_region_exits` per area; emitting them to compiled_rules.json + rewiring `create_regions()` to consume them is mechanical but out of Gate A's "high-impact bar" framing. Without it `accessibility: items` mode fails. |
| `TrickLevel` Choice option in `Options.py` | Gate B. Requires three pre-baked rule files (`compiled_rules_l{1,2,3}.json`) plus the per-trick-level compile pass. |
| Three trick-level rule files | Gate B. The compiler already accepts trick configuration, but the apworld currently only loads `compiled_rules.json` — adding the dispatch layer is small but unblocks nothing for v0.1 single-player. |
| Stale docstrings in some test files | Cosmetic. Touched the ones in Rules.py / Regions.py / World.py; test files' historical M1 comments are mostly self-correcting through the LATE_GAME-with-events refactor. |

### Approximations that remain (carrying forward from M1)

Strikethrough = eliminated by M2.

1. ~~Events evaluate as Trivial.~~ Events are now real items with
   real reach rules.
2. **Cross-region access is unmodeled.** Solver thinks every region is
   reachable from Menu, so it doesn't worry about chaining. *Gate B.*
3. **Ammo counts collapsed.** A rule that needs 75 missiles still
   passes with any 1 Missile Tank. Defer to v0.3.
4. **Damage thresholds ignored.** A 30-damage cold room with no suit
   passes (we model only suit ownership, not HP). Defer to v0.3.
5. **Tricks at level 2+ are impossible.** Gate B (TrickLevel UI option)
   will unlock the higher tiers.

### Approximations introduced by M2

1. **Per-event reach rules are OR'd across areas without cross-region
   cost.** If event `Foo` lives in region A (requires Plasma) and also
   in region B (requires Storm), the rule reads "Plasma OR Storm" —
   ignoring the cost of reaching A or B. With Regions.py as a star
   topology this is the same approximation as M1's per-pickup rules
   (both assume you can be in any region for free), so the wire is
   consistent. Gate B's cross-region edges fix both at once.

2. **Itorash events folded into Hanubia.** The starter preset patcher
   has no Itorash pickup locations, so `regions.json` has 8 regions.
   Events whose home region is Itorash (most notably `Ship`) live at
   synthetic event locations whose `region` field reads `Hanubia`.
   The compiled reach rule is still the per-area Itorash rule (OR'd
   if present in other regions too) — only the AP-side location
   metadata is folded. Adding Itorash as a 9th region would clean this
   up; out of scope for Gate A.

3. **Events with no compiled event node default to IMPOSSIBLE.** If an
   event is referenced from a rule but no `node_type: "event"` node
   carries that `event_name` in any compiled area, the per-event rule
   is `IMPOSSIBLE` and the compiler prints a WARN. Today this hits
   zero events (the `--all` compile covers every area that contains
   referenced events); leaving the default conservative makes a
   compile-set narrowing (e.g. dropping `--all`) fail loud.

### What v0.3 needs to budget for

Inherited from M1 (still true):
- Real ammo counting (Missile Tank count ≥ N rather than ≥ 1).
- Damage / E-Tank thresholding for non-suit-gated rooms.

Newly visible after M2:
- The cross-region star is now the dominant remaining over-permissive
  approximation. Gate B will close it; v0.3 should already plan
  around the assumption that cross-region edges land before then.
- The `accessibility: items` failure mode (cross-region-induced) is a
  clear next-task signal. The smoke yaml flips to `items` the moment
  Gate B lands.
