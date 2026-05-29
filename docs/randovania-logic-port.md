# Randovania logic → Archipelago rules — port plan

## Context (read this first)

The `dread_ap` project is a Metroid Dread Archipelago integration for
modded Switch. The wire layer is shipped and tested ([CLAUDE.md](../CLAUDE.md)
covers the architecture; [PLAN.md](../PLAN.md) is the original
implementation plan). 63 unit tests pass; phases 1–5 are complete.

The one load-bearing gap is **logic**. The current
[apworld/dread/Rules.py](../apworld/dread/Rules.py)
is empty and [Regions.py](../apworld/dread/Regions.py) uses
a star topology — every region connects directly from Menu with no
requirements. A generated seed will trivially soft-lock (Power Bomb
behind a Power Bomb door, Morph Ball behind a Morph Ball gate, etc.).

Your job is to port Randovania's authoritative Dread logic database into
AP-native `add_rule` form. There is no Dread Manual AP world to crib from
(I checked — the community hasn't built one).

## Scope: this milestone vs. full coverage

You are implementing **Milestone 1**: the compiler infrastructure plus a
proof-of-concept port of the smallest area (Elun, 4 pickups). Do not
attempt to port all 9 areas in this session — that's Milestone 2 and is
~2 weeks of focused work. The Milestone 1 deliverable proves the path
scales linearly so Milestone 2 is mechanical.

**Done when**:
1. The compiler can ingest Randovania's logic JSON for any area and emit
   AP-style `add_rule` calls.
2. Elun's 4 pickups have non-trivial rules and ≥3 hand-verified test
   assertions pass (e.g. "Pickup (Plasma Beam) requires Morph Ball").
3. The remaining 8 areas can be added by re-running the compiler — the
   bottleneck for Milestone 2 should be data validation, not new code.

**Explicitly NOT in scope**:
- Other 8 areas (Artaria, Burenia, Cataris, Dairon, Ferenia, Ghavoran,
  Hanubia, Itorash). Compile them as a sanity test of the pipeline if
  you have time, but ship them only if their rules-test fixtures pass.
- The Switch wire layer (`apworld/dread/client/*`). Do not
  touch it.
- The patcher adapter (`scripts/build_patcher_json.py`). Do not touch it.
- AP-ID renumbering. The hash-derived IDs in `data/items.json` and
  `data/locations.json` are stable contracts. Adding new event items at
  the end is OK; reordering existing ones is not.
- Trick-level UI option. Compile with all tricks OFF for v0.1; add the
  Options-driven knob in Milestone 2.

## Randovania logic format (what you're parsing)

Source files live at [randovania/games/dread/logic_database](https://github.com/randovania/randovania/tree/main/randovania/games/dread/logic_database)
in the upstream Randovania repo. Already vendored at
`vendor/open-dread-rando/` does NOT contain these — you need to fetch
them. There are 10 files:

```
header.json     481 KB  Global resource database + templates
Artaria.json   1448 KB
Burenia.json    875 KB
Cataris.json   1456 KB
Dairon.json     849 KB
Elun.json       141 KB  ← smallest, your Milestone 1 target
Ferenia.json    728 KB
Ghavoran.json   835 KB
Hanubia.json    186 KB
Itorash.json     79 KB
```

Cache them under `.dread-cache/randovania-logic/` (already in
`.gitignore` via the `.dread-cache/` rule). Pin a specific upstream
commit hash and record it in the cache directory.

### Top-level shape (per-area file)

```json
{
  "name": "Elun",
  "extra": {"asset_id": ..., "scenario_id": "s060_quarantine"},
  "areas": {
    "Save Station South": {
      "default_node": "...",
      "nodes": {
        "Pickup (Plasma Beam)": {
          "node_type": "pickup",
          "pickup_index": 115,
          "extra": {
            "actor_name": "itemsphere_plasmabeam_000",
            "actor_def": "actordef:actors/items/..."
          },
          "location_category": "major",
          "connections": {
            "Door to Ammo Recharge Station": { ... requirement tree ... }
          }
        },
        "Door to Ammo Recharge Station": {
          "node_type": "dock",
          "dock_type": "door",
          "default_connection": {...},
          "connections": { ... }
        }
      }
    }
  }
}
```

### Requirement tree node types

```jsonc
// AND of requirements
{"type": "and", "data": {"items": [<req>, <req>, ...]}}

// OR of requirements
{"type": "or", "data": {"items": [<req>, <req>, ...]}}

// Reference to a single resource (item / trick / event)
{"type": "resource", "data": {
  "type": "items" | "tricks" | "events" | "damage",
  "name": "Morph Ball",       // resource name in the global database
  "amount": 1,                 // 1 for items, level for tricks
  "negate": false              // rare; means "requires NOT having this"
}}

// Named template — expand recursively against header.json
{"type": "template", "data": "Lay Bomb"}
```

### header.json (global)

- `resource_database` — the canonical names for items, tricks, events,
  damage. Use this to validate your name mapping.
- `requirement_template` — `{template_name: requirement_tree}`. Templates
  can reference other templates; expand recursively at compile time.
- `dock_weakness` — door type definitions (PowerBeamDoor, MissileDoor,
  etc.) with their requirements. Most pickup-reachability rules end up
  being chains of dock crossings, so this matters.

## Translation pipeline

```
.dread-cache/randovania-logic/*.json
    │
    │ scripts/cache_randovania_logic.py   (fetch + pin)
    ▼
apworld/dread/data/compiled_rules.json   (intermediate)
    │
    │ scripts/extract_dread_rules.py   (the compiler)
    │   - parse header.json → resource db + templates
    │   - per area, parse nodes + connections graph
    │   - per pickup, compute reachability requirement from Start
    │   - emit per-location rule as a serializable Lua-table-style record
    ▼
apworld/dread/Rules.py    (loads compiled_rules.json, applies add_rule)
apworld/dread/Regions.py  (uses node graph for region-to-region access)
apworld/dread/data/events.json  (event-item pool additions)
```

### Step 1: cache

`scripts/cache_randovania_logic.py` — straightforward fetch + write.
Pin to a specific commit hash; record it in
`.dread-cache/randovania-logic/PINNED_COMMIT.txt`. Idempotent (skip
files whose hash already matches).

### Step 2: compile

`scripts/extract_dread_rules.py` is the meat. Structure:

```python
def compile_area(area_json: dict, header: HeaderDb) -> AreaRules:
    nodes = parse_nodes(area_json)
    graph = build_node_graph(nodes)             # node_id -> (target_id, requirement)
    start_nodes = find_starting_nodes(nodes)    # in Artaria; Elun has none
    rules = {}
    for pickup_node in nodes_of_type("pickup", nodes):
        rules[pickup_node.actor_name] = reachability_requirement(
            graph, start_nodes, pickup_node)
    return rules
```

#### Reachability strategy

This is the conceptually hard step. Randovania uses node-to-node
connections; Archipelago wants per-location predicates over the player's
inventory `state`. The translation:

> **A pickup is reachable IFF there exists a path from Start to the
> pickup node where every edge's requirement is satisfied by `state`.**

Pragmatic v0.1 algorithm (good enough for Elun):

1. BFS from Start with an initially-empty resource set
2. At each node, accumulate the AND of requirements along the edge taken
3. When you reach the pickup node, emit the path's requirement
4. Across multiple paths, take the OR
5. Simplify the resulting AST (drop redundant ANDs, hoist common terms)

A complete algorithm is the **fixed-point reachability** Randovania
itself uses: repeatedly expand reachable nodes given current resources
until convergence. Implement the pragmatic version first; document where
it overestimates so Milestone 2 can swap in the fixed-point if needed.

For event nodes (e.g. "Defeat Corpius"): treat them as **items the
player gains by reaching the event**. Add them to the AP pool as
`progression` event items (zero-quantity, can only be obtained by
clearing the event location). The pickup-reachability for nodes past the
event then requires `state.has("Defeated Corpius")`.

#### Requirement AST → Python lambda

```python
def compile_requirement(req: dict, ctx: CompileContext) -> Predicate:
    t = req["type"]
    if t == "and":
        children = [compile_requirement(c, ctx) for c in req["data"]["items"]]
        return lambda state: all(c(state) for c in children)
    if t == "or":
        children = [compile_requirement(c, ctx) for c in req["data"]["items"]]
        return lambda state: any(c(state) for c in children)
    if t == "resource":
        d = req["data"]
        if d["type"] == "items":
            return lambda state: state.has(map_item_name(d["name"]), ctx.player)
        if d["type"] == "tricks":
            # v0.1: tricks default to OFF
            return lambda state: False
        if d["type"] == "events":
            return lambda state: state.has(event_item_name(d["name"]), ctx.player)
        if d["type"] == "damage":
            # damage requirements collapse to suit ownership in v0.1
            return compile_damage(d, ctx)
    if t == "template":
        return compile_requirement(ctx.templates[req["data"]], ctx)
    raise ValueError(f"unknown requirement type {t}")
```

Closure-capture gotcha: don't iterate over a list and reuse `c` in a
lambda — Python's late binding will bite you. Use list comprehensions or
default-argument trick.

#### Item name mapping

Randovania's resource names (e.g. `"Morph Ball"`, `"Plasma Beam"`,
`"Missiles"`) mostly match our `data/items.json` names. Differences:

| RDV name | Our AP name |
|---|---|
| Missiles | Missile Tank (capacity comes from `quantity=2`) |
| Energy Tanks | Energy Tank |
| Energy Parts | Energy Part |
| Lock-On Missile | Storm Missile |
| Power Beam | (no AP item — Samus has it from start) |

Build a mapping table in `scripts/extract_dread_rules.py` as a literal
dict; assert at compile time that every RDV name resolves to either an
AP item or a known-no-op skip (`Power Beam`, etc.). Unknown name = fail
loud with the area + node so the gap surfaces immediately.

### Step 3: emit + load

The compiler emits `apworld/dread/data/compiled_rules.json`:

```json
{
  "pinned_commit": "<sha>",
  "events": ["Defeated Corpius", "Defeated Kraid", ...],
  "rules": {
    "Elun: Plasma Beam": {
      "type": "and",
      "items": [
        {"type": "item", "name": "Morph Ball"},
        {"type": "item", "name": "Bomb"}
      ]
    },
    ...
  }
}
```

Keep it as serialized AST, not pre-compiled lambdas, so `Rules.py` can
re-compile against the live `player` index at world-creation time.

`Rules.py` then becomes:

```python
def set_rules(world):
    rules = load_compiled_rules(DATA_DIR / "compiled_rules.json")
    for location_name, rule_ast in rules["rules"].items():
        predicate = compile_to_lambda(rule_ast, world.player)
        location = world.multiworld.get_location(location_name, world.player)
        add_rule(location, predicate)
    world.multiworld.completion_condition[world.player] = lambda state: state.has(
        "Defeated Raven Beak", world.player)
```

`Regions.py` switches from star topology to a real graph: each area-to-
area connection comes from the cross-region docks in the logic JSON
(specifically `node_type: "dock"` with a target in a different area).

### Step 4: event items

Add the event names to the AP item pool as `progression` items with
zero copies in the natural pool (they're location-events, not collectible
items). The compiler emits the event list to
`apworld/dread/data/events.json`; `World.create_items` adds
one event item per event name; `set_rules` wires each event location's
`place_locked_item` to its event item.

## Files to create

| Path | Purpose |
|---|---|
| `scripts/cache_randovania_logic.py` | Fetch + pin |
| `scripts/extract_dread_rules.py` | The compiler |
| `apworld/dread/data/compiled_rules.json` | Compiler output |
| `apworld/dread/data/events.json` | Event item pool |
| `apworld/dread/tests/test_rule_compiler.py` | Unit tests on the compiler |
| `apworld/dread/tests/test_elun_rules.py` | Hand-verified Elun assertions |
| `docs/randovania-logic-port-notes.md` | Where you ended up: what worked, what didn't, what Milestone 2 needs |

## Files to modify

| Path | Change |
|---|---|
| `apworld/dread/Rules.py` | Load compiled_rules.json, apply add_rule, set completion_condition |
| `apworld/dread/Regions.py` | Real graph instead of star topology (cross-area docks only — within-area is handled by per-location rules) |
| `apworld/dread/World.py` | Add event items in `create_items`; place_locked_item for events |
| `apworld/dread/data/items.json` | Append event items (DON'T renumber existing AP IDs — append only) |
| `CLAUDE.md` | Update Phase 4 status to reflect real rules; bump version slug |

## Hand-verified assertions for Elun (Milestone 1 acceptance)

Elun has 4 pickups (per the format analysis):
- Energy Tank (114) — should require **Morph Ball** (most rooms in Elun gated by morph tunnels)
- Plasma Beam (115) — should require **Morph Ball** plus probably a beam upgrade
- Power Bomb Tank (116) — should require **Power Bomb**
- Missile Tank (117) — should require **some access weapon**, exact requirement TBD

Verify these by hand against [the Metroid Dread Wiki Elun map](https://metroid.wiki.gg/wiki/Quiet_Robe)
before treating "the compiler said X" as ground truth. The compiler is
software; the wiki is closer to player experience. Hand-vet ≥3 rules.

If your compiler produces `lambda state: False` for any pickup, the
algorithm is wrong (every pickup must be reachable from Start in vanilla
Dread). Likewise `lambda state: True` for Plasma Beam is wrong — it sits
behind ≥1 unique item.

## Pitfalls

1. **Late binding in Python lambdas** — `for x in xs: rules.append(lambda: x)` captures `x` by reference. Use `lambda x=x:` or list-comp construction.
2. **Template recursion** — expand all templates **before** lambda compilation, otherwise expansion happens once per lambda invocation. Cache aggressively.
3. **Bidirectional doors** — connection requirements are usually symmetric but not always (one-way drops, climb-walls). Read both directions before assuming.
4. **Trick gating** — default ALL tricks to False. Don't accidentally include "advanced glitched" paths as required. The wire-up test "Plasma Beam requires Morph Ball" must not fail because the compiler picked up a ledge-warp trick path that's hidden behind tricks=False.
5. **Damage resources** — `{"type": "damage", "name": "Lava", "amount": 30}` etc. For v0.1, map to suit ownership: any heat/lava resource → `state.has("Varia Suit") or state.has("Gravity Suit")`. Water → Gravity Suit. Damage amount is ignored.
6. **Events as items** — they need to enter the AP item pool to be holdable in `state.has`. `World.create_items` adds them; `set_rules` calls `place_locked_item` on the event location.
7. **Cross-area travel** — Dread's areas connect via teleporters/transports. Find them in each area's node graph (`node_type: "dock"` with `default_connection.area` ≠ current area). Wire them as Regions connections in `Regions.py`.
8. **Starting location** — Samus starts in Artaria at `StartPoint0` in `s010_cave`. The `valid_starting_location: true` field in nodes is your hint. Other areas have no native starting points; reachability for Burenia must traverse via Artaria → cross-area dock → Burenia.
9. **`actor_name` mapping** — Randovania's `extra.actor_name` (snake_case) matches our `locations.json` `actor` field (CamelCase or snake_case). Some don't match exactly. Build a lookup with normalization (lowercase, ignore underscores) and assert 100% coverage of our 137 actor-pickups at compile time. Boss/EMMI/cutscene pickups don't have actors in our table — they're handled via the `pickup_lua_callback` field; treat them as special cases.

## Verification

Three layers of verification, in order of value:

1. **Unit tests on the compiler primitives** — AST → lambda over a mocked `state`. Fast, mechanical, run on every change.
2. **Hand-vetted Elun assertions** — proves the compiler gets a real area right. Run after compiler changes.
3. **Generation smoke test** — generate a Dread-only seed with `python vendor/Archipelago/Generate.py`. Confirm it produces a solvable seed (Archipelago's solver runs as part of generation). If it doesn't, the rules are wrong somewhere.

The Phase 1 wire test (`scripts/phase1_validate.py`) is unrelated to this
work and need not be re-run.

## What success documentation looks like

Append to `CLAUDE.md` under "Status":

> Logic: Milestone 1 shipped. Elun (4 pickups) compiled and hand-verified. Compiler infrastructure (cache, AST parser, reachability, lambda compiler, event-item pipeline) is reusable for the other 8 areas — that's Milestone 2.

Also write `docs/randovania-logic-port-notes.md` with what worked, what
didn't, and what Milestone 2 needs to budget for. Future-you reading
that doc 4 weeks from now should know exactly where to pick up.
