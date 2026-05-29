# M2 plumbing — wire compiled rules into the AP solver

## Context (read this first)

The `dread_ap` project ships a working Metroid Dread Archipelago client
(see [CLAUDE.md](../CLAUDE.md)). Phases 1–5 are done; the [Randovania
logic compiler](randovania-logic-port.md) covers all 9 Dread areas and
emits 137 rules — 100% of actor-pickup locations.

The compiler half of [Milestone 2](randovania-logic-port-notes.md) shipped.
The **consumer half didn't**. Your job is to wire the compiler's output
into the AP-side machinery so the solver actually honors what the
compiler produced.

### The headline problem

Of 137 compiled rules, **85 (62%) reference at least one event node**.
Today the lambda compiler in [Rules.py](../apworld/dread_archipelago/Rules.py)
short-circuits events to `True`:

```python
if t == "event":
    # M1: events treated as trivially satisfied. Real event-item
    # gating arrives in M2.
    return _const_true
```

So a rule that *should* read "needs Plasma Beam AND grapplepulloff event"
currently reads "needs Plasma Beam AND True" — losing the gating on
whatever the player needs to reach the grapplepulloff node (typically
Grapple Beam). 62% of rules are quietly under-constrained.

Three other plumbing gaps follow from the same M1-era simplification:
the completion_condition is hardcoded `True`, `Regions.py` uses a star
topology, and the trick-level knob is hardcoded.

## Scope

You're completing **M2 plumbing**: wire the existing compiler output
into AP generation. **You are not extending the compiler**, except where
clearly noted (it needs to emit per-event rules and per-area cross-region
exits — neither is done today).

### Done when

The acceptance criteria are split into two gates so you can ship part 1
even if part 2 runs long.

**Gate A — playable seeds (the high-impact bar)**. Required.

1. Event-item plumbing landed: events ARE items in the AP pool, placed
   at synthetic event locations whose access rules are the per-event
   reach requirements compiled from Randovania. Rules.py's event branch
   returns `state.has(event_item_name, player)`.
2. Real victory condition: `Rules.py` reads
   `compiled["victory_condition"]` (currently `{type: event, name: Ship}`)
   and sets the completion_condition accordingly.
3. A fresh generation smoke run (`scripts/ap_generate.py` against
   `apworld/dread_archipelago/tests/seeds/dread_smoke.yaml`) produces a
   solvable seed.
4. A regression test asserts: a rule that depends on an event becomes
   un-satisfiable in a State that lacks the event item.
5. All existing 134 tests still pass.

**Gate B — full M2 plumbing**. Try to land; descope cleanly if budget runs out.

6. Cross-region access in `Regions.py`: replace star topology with the
   real graph derived from cross-region docks.
7. Trick-level Choice option in `Options.py` (Beginner / Intermediate /
   Advanced), threaded through the rule selection so the solver honors
   the user's pick at world-creation time.
8. Stale docstrings refreshed in Rules.py / Regions.py / World.py to
   reflect M2 reality.

### Explicitly NOT in scope

- Ammo counting (per the retro, defer to v0.3).
- Damage thresholds beyond the existing "suit ownership" collapse.
- Rebuilding the compiler from scratch. Only extend where called out.
- Touching the wire layer (`apworld/dread_archipelago/client/*`).
- Touching the patcher adapter (`scripts/build_patcher_json.py`).
- Renumbering AP IDs for existing items / locations. APPEND ONLY.
- Boss/EMMI/cutscene non-actor pickups (the 12 entries in
  `locations.json` that don't have compiled rules). Those gate via
  `pickup_lua_callback` in the patcher, not via the logic graph.

## Gate A — the high-impact work

### A1. Extend the compiler to emit per-event reach rules

Currently `events.json` is just a name list. To make events into real AP
items we need to know, per event, **which node it sits at and how the
player reaches that node**. The compiler already walks the graph; it
just doesn't emit the per-event reachability.

**Modify [scripts/extract_dread_rules.py](../scripts/extract_dread_rules.py)** to:

- For each area, in addition to per-pickup-node reachability, compute
  per-event-node reachability the same way (BFS from area entry,
  accumulate AND of edge requirements).
- Output structure for `compiled_rules.json` grows a new key:

  ```jsonc
  {
    "pinned_commit": "...",
    "victory_condition": {"type": "event", "name": "Ship"},
    "events": [
      {
        "name": "ArtariaCU",
        "region": "Artaria",
        "rule": {"type": "and", "items": [...]}   // reach rule for the event node
      },
      ...
    ],
    "rules": { ... }                              // unchanged
  }
  ```

  Keep the existing `events.json` (the bare name list) OR fold its
  contents into `compiled_rules.json` — your call. Whichever, document
  the schema bump in `docs/randovania-logic-port-notes.md`.

- AP ID assignment for event items + locations: append-only and stable.
  The simplest convention:

  ```python
  # In a new scripts/extract_event_ap_ids.py (or fold into extract_dread_rules.py)
  EVENT_ITEM_BASE = max(existing_item_ap_ids) + 1
  EVENT_LOCATION_BASE = max(existing_location_ap_ids) + 1
  for i, event_name in enumerate(sorted(event_names)):
      event_item_ap_id[event_name] = EVENT_ITEM_BASE + i
      event_location_ap_id[event_name] = EVENT_LOCATION_BASE + i
  ```

  Sorted-by-name keeps the ordering stable across recompiles. The IDs
  must be disjoint from existing `items.json` and `locations.json`
  ranges (the existing extractor seeds those from hash-derived bases —
  see [scripts/extract_dread_data.py](../scripts/extract_dread_data.py)).
  Emit the AP IDs alongside the event rules so the consumer reads them
  rather than recomputing.

### A2. Append event items to `data/items.json` and event locations to `data/locations.json`

Run the extended compiler once, then capture its output into the data
tables. The IDs are stable so this is a one-shot append per recompile.

Event items shape:
```json
{
  "name": "Event: ArtariaCU",
  "ap_id": <stable id>,
  "patcher_item_id": "",
  "quantity": 0,
  "classification": "progression"
}
```

Event locations shape:
```json
{
  "name": "Event: ArtariaCU",
  "ap_id": <stable id>,
  "region": "Artaria",
  "scenario": "s010_cave",
  "actor": "",
  "pickup_type": "event",
  "vanilla_item": "Event: ArtariaCU"
}
```

Naming convention: `"Event: <name>"` for both the item and the location.
AP allows the item and its locked location to share a name; this makes
debugging easier.

**Update [Items.py](../apworld/dread_archipelago/Items.py)** if needed —
the `DreadItemData` dataclass already handles `quantity=0`; the
`_CLASSIFICATION_MAP` already has `progression`. Probably zero changes
here. Same for Locations.py — the `pickup_type` is a free-form string,
"event" is a new value but the validator is loose.

**Update the existing data-integrity tests** in
[tests/test_data_tables.py](../apworld/dread_archipelago/tests/test_data_tables.py):
- `test_location_count_is_149` → bump to `149 + N_events` and rename to
  something forward-looking like `test_location_count_matches_table_sum`.
- `test_item_count_at_least_30` already permissive; nothing to change.
- The `test_vanilla_items_resolve_to_known_item` will need event items
  to be valid `vanilla_item` references — extending the lookup with
  event item names fixes this.

### A3. Wire World.create_items + set_rules

**[World.py](../apworld/dread_archipelago/World.py) — `create_items`**:

```python
def create_items(self):
    # ... existing pool fill ...
    # Append one event item per event in the compiled set.
    # Event items don't consume location slots in the regular pool — they
    # only exist to be placed at their corresponding event location.
    # AP convention: precollect them via self.multiworld.push_precollected
    # OR add to itempool with skip_balancing=True (you decide; the latter is
    # more visible in the spoiler log).
    compiled = load_compiled_rules()  # share the helper from Rules.py
    for event in compiled["events"]:
        evt_item_name = f"Event: {event['name']}"
        item = self.create_item(evt_item_name)
        # Mark as skip_balancing so the multiworld solver doesn't try to
        # balance these into other players' pools.
        item.classification = ItemClassification.progression_skip_balancing
        self.multiworld.itempool.append(item)
```

**[Rules.py](../apworld/dread_archipelago/Rules.py) — `set_rules`**:

```python
def set_rules(world):
    from worlds.generic.Rules import add_rule
    multiworld = world.multiworld
    player = world.player

    compiled = load_compiled_rules_or_fallback()

    # 1. Per-pickup rules (existing behavior).
    for loc_name, rule_ast in compiled.get("rules", {}).items():
        try:
            location = multiworld.get_location(loc_name, player)
        except KeyError:
            continue
        predicate = compile_to_lambda(rule_ast, player)
        add_rule(location, predicate)

    # 2. Per-event-location reach rules + lock the event item to the
    #    event location.
    for event in compiled.get("events", []):
        loc_name = f"Event: {event['name']}"
        item_name = f"Event: {event['name']}"
        try:
            location = multiworld.get_location(loc_name, player)
        except KeyError:
            continue
        # The location's reach rule = the event's reach rule.
        predicate = compile_to_lambda(event["rule"], player)
        add_rule(location, predicate)
        # Pin the event item to the event location so the solver can't
        # move it. This is how events become collectible state.
        item = next((i for i in multiworld.itempool
                     if i.player == player and i.name == item_name), None)
        if item is not None:
            multiworld.itempool.remove(item)
            location.place_locked_item(item)

    # 3. Switch the event branch of the lambda compiler from _const_true
    #    to state.has — done by updating compile_to_lambda below.

    # 4. Real victory condition.
    victory = compiled.get("victory_condition", {"type": "trivial"})
    if victory.get("type") == "event":
        ship_item = f"Event: {victory['name']}"
        multiworld.completion_condition[player] = (
            lambda state, n=ship_item: state.has(n, player)
        )
    else:
        multiworld.completion_condition[player] = lambda state: True
```

**[Rules.py](../apworld/dread_archipelago/Rules.py) — `compile_to_lambda`**,
update the event branch:

```python
if t == "event":
    name = ast["name"]
    return lambda state, n=f"Event: {name}": state.has(n, player)
```

**Mind the closure-capture trap.** Default-argument binding is the safe
pattern; don't refactor it into a loop variable without re-applying
defaults.

### A4. Tests for Gate A

Required new tests in
[tests/test_rule_compiler.py](../apworld/dread_archipelago/tests/test_rule_compiler.py):

```python
def test_event_branch_consults_state():
    """After M2 plumbing, events resolve via state.has(<event item>, player).
    M1 returned _const_true; that's now wrong."""
    pred = compile_to_lambda({"type": "event", "name": "ShipPickup"}, player=1)
    assert pred(StubState({})) is False
    assert pred(StubState({"Event: ShipPickup": 1})) is True
```

Plus a new
[tests/test_event_plumbing.py](../apworld/dread_archipelago/tests/test_event_plumbing.py):

- Asserts the compiled `events` list is non-empty.
- Asserts every event has both a `region` and a `rule`.
- Asserts every event's AP ID is in a range disjoint from
  `items.json` and `locations.json`.
- Asserts the events are sorted by name (stable ordering invariant).

Pick one or two specific event-gated rules from `compiled_rules.json`
(e.g. a Burenia pickup that references `BureniaPrepareSpeedSave`) and
assert that with the event item present and `Speed Booster` absent, the
rule still evaluates the speed condition — i.e. confirm the
under-constrained behavior is gone.

**Update the smoke seed** at
[tests/seeds/dread_smoke.yaml](../apworld/dread_archipelago/tests/seeds/dread_smoke.yaml)
only if needed. `accessibility: items` is already correct.

## Gate B — the medium-impact follow-up

### B1. Cross-region access in Regions.py

The compiler already enumerates cross-region exits per area (per the
retro doc, in an internal `cross_region_exits` structure). Extend
`scripts/extract_dread_rules.py` to emit them to compiled_rules.json:

```jsonc
{
  ...,
  "cross_region_edges": [
    {"from": "Artaria", "to": "Cataris", "dock": "Elevator to Cataris",
     "rule": {"type": "trivial"}},
    {"from": "Burenia", "to": "Ghavoran", "dock": "Shuttle to Ghavoran",
     "rule": {"type": "and", "items": [...]}},
    ...
  ]
}
```

Then rewrite [Regions.py](../apworld/dread_archipelago/Regions.py):

- Keep the Menu region.
- Menu connects ONLY to the starting region (Artaria — derived from the
  starting_area option). Other regions are NOT connected from Menu.
- For each `cross_region_edges` entry, call `from_region.connect(
  to_region, dock_name, rule=compile_to_lambda(edge.rule, player))`.

Edges are usually bidirectional in Dread. The compiler should emit them
as separate one-way edges (one per direction); if it doesn't today,
you'll need to emit both. Cross-region docks have weakness requirements
in `header.json` under `dock_weakness`; transport docks (Shuttles,
Elevators) are typically Trivial.

### B2. Trick-level option

Two parts:

**Compiler side**: `scripts/extract_dread_rules.py` accepts
`--trick-level=1|2|3` (it already accepts `trick_level` as a parameter
per the retro). Output three artifacts:

```
apworld/dread_archipelago/data/compiled_rules_l1.json   (Beginner — current behavior)
apworld/dread_archipelago/data/compiled_rules_l2.json   (Intermediate)
apworld/dread_archipelago/data/compiled_rules_l3.json   (Advanced)
```

Bake all three so the apworld can swap based on the user option without
re-running the compiler at generation time.

**Options.py**:

```python
class TrickLevel(Choice):
    """How permissive the access logic is. Beginner = no tricks.
    Intermediate = basic shinesparking and bomb jumps. Advanced =
    everything Randovania classifies as Advanced. Higher levels generate
    seeds that ASSUME the player knows the trick, not seeds that
    REQUIRE it."""
    display_name = "Trick Level"
    option_beginner = 1
    option_intermediate = 2
    option_advanced = 3
    default = 1


@dataclass
class DreadOptions(PerGameCommonOptions):
    starting_area: StartingArea
    include_boss_pickups: IncludeBossPickups
    trick_level: TrickLevel
```

**Rules.py**: `load_compiled_rules` chooses the file based on
`world.options.trick_level.value`.

### B3. Stale docstrings refresh

Refresh top-of-file comments in:
- `Rules.py` (currently "Milestone 1: only Elun (5 pickups) has compiled rules")
- `Regions.py` (currently "v0.1 we use a star topology")
- `World.py` (currently "Milestone 1 shipped: Elun (5 pickups) has real compiled rules")

Update the "Logic status" section of [CLAUDE.md](../CLAUDE.md) and
bump `__version__` in
[apworld/dread_archipelago/__init__.py](../apworld/dread_archipelago/__init__.py)
to `0.0.1-phase4-logic-m2`.

### B4. Tests for Gate B

- `test_regions.py`: assert Menu connects to exactly one region (the
  starting one); assert at least N cross-region edges exist.
- `test_trick_level_files_exist.py`: assert all three `compiled_rules_lN.json`
  files are present, valid JSON, and reference the same pinned commit.

## Files you'll touch

### Modify

| Path | Why |
|---|---|
| `scripts/extract_dread_rules.py` | Emit per-event rules + cross-region edges + trick-level outputs |
| `apworld/dread_archipelago/data/compiled_rules.json` | Regenerate with new schema |
| `apworld/dread_archipelago/data/events.json` | Either restructure or remove (folded into compiled_rules.json) |
| `apworld/dread_archipelago/data/items.json` | Append event items (DO NOT renumber existing) |
| `apworld/dread_archipelago/data/locations.json` | Append event locations (DO NOT renumber existing) |
| `apworld/dread_archipelago/Rules.py` | Event branch → state.has; victory_condition wiring; per-event place_locked_item |
| `apworld/dread_archipelago/Regions.py` | Real graph from cross_region_edges (Gate B) |
| `apworld/dread_archipelago/Options.py` | TrickLevel option (Gate B) |
| `apworld/dread_archipelago/World.py` | Event item creation in `create_items`; docstring refresh |
| `apworld/dread_archipelago/__init__.py` | Version bump |
| `apworld/dread_archipelago/tests/test_data_tables.py` | Bump expected counts |
| `docs/randovania-logic-port-notes.md` | M2 plumbing retro; what worked / didn't |
| `CLAUDE.md` | Refresh Status section |

### Create

| Path | Why |
|---|---|
| `apworld/dread_archipelago/tests/test_event_plumbing.py` | Gate A regression coverage |
| `apworld/dread_archipelago/tests/test_regions.py` | Gate B cross-region coverage |
| `apworld/dread_archipelago/tests/test_trick_level_files_exist.py` | Gate B trick-level coverage |
| `apworld/dread_archipelago/data/compiled_rules_l1.json` etc. | Gate B trick-level outputs |

## Pitfalls

1. **AP-ID stability**. The existing item / location IDs are hash-derived
   from `"Metroid Dread maxdietz items"` etc. — see
   [scripts/extract_dread_data.py](../scripts/extract_dread_data.py).
   Appended event IDs must start at `max(existing_ids) + 1` and order by
   sorted event name so they don't drift. Adding or renaming events
   shifts everything after that event — try not to.

2. **`compile_to_lambda` closures and late binding**. The existing code
   uses default-argument capture (`lambda state, n=name: ...`); preserve
   that pattern when you add the event branch.

3. **`place_locked_item` and itempool ordering**. After you
   `place_locked_item`, remove the item from the itempool (or never put
   it there in the first place — AP idioms vary). Pick one path; double-
   placement crashes generation.

4. **`progression_skip_balancing` vs plain `progression`**. Event items
   are progression (they unlock other locations) but they shouldn't
   participate in inter-player balancing (they're locked to a specific
   location). `progression_skip_balancing` is the right classification.

5. **Cross-region edges must be one-way pairs**. Most Dread connections
   are bidirectional, but the compiler models each direction separately
   so requirements can differ. Emit both directions; don't assume
   symmetry.

6. **Starting region**. Today everything connects from Menu, so the
   solver doesn't care where Samus starts. Once you remove that, the
   starting region matters — derive it from `world.options.starting_area`
   (currently only Artaria is valid).

7. **Pre-existing victory-condition redundancy**. World.py used to set
   `completion_condition` AFTER calling `set_rules` (silently masking
   anything Rules.py set). That's already been fixed; don't reintroduce
   it. Rules.py owns completion_condition.

8. **Pinned commit drift**. The compiler reads from
   `.dread-cache/randovania-logic/` which is pinned to commit
   `3559136dc44`. If you re-run the cache fetch, make sure the pin
   doesn't move underneath you. `PINNED_COMMIT.txt` is the source of
   truth.

9. **Itorash isn't in locations.json**. The compiler emits rules for
   Itorash, but `locations.json` was extracted from
   `starter_preset_patcher.json` which doesn't include s090_skybase.
   So Itorash events are real but have no pickup locations to gate.
   Don't add Itorash pickups unless you also update the patcher
   template — that's out of scope here.

10. **Goal detection lives on the Switch, not in AP**. The client
    already reports `StatusUpdate{CLIENT_GOAL}` based on
    `Init.bBeatenSinceLastReboot` (see
    [client/context.py](../apworld/dread_archipelago/client/context.py)).
    The completion_condition change is for the AP **generator** to know
    the seed is solvable — it doesn't change the runtime goal signal.

## Verification

Run, in order:

```pwsh
# All existing tests still pass after each change
python -m pytest apworld/dread_archipelago/tests/ scripts/tests/ -q

# Compiler still produces a parseable artifact
python scripts/extract_dread_rules.py --all
python -c "import json; print(len(json.load(open('apworld/dread_archipelago/data/compiled_rules.json'))['events']))"

# Generation smoke test produces a fresh seed
python scripts/ap_generate.py apworld/dread_archipelago/tests/seeds/dread_smoke.yaml
ls apworld/dread_archipelago/tests/seeds/out/
```

Per the retro doc, watch for:
- "Late-game loadout reaches everything" — `test_all_regions_rules.py`
  should still pass post-event-wiring (full loadout includes all event
  items implicitly via `place_locked_item`).
- "No pickup is impossible" — same; tighter event gating shouldn't
  introduce impossible pickups.
- An event-gated rule should now be MORE restrictive than before. Spot-
  check a Burenia pickup that references `BureniaPrepareSpeedSave` and
  confirm it now requires Speed Booster (transitively).

## Doc deliverable

Append a new section to
[docs/randovania-logic-port-notes.md](randovania-logic-port-notes.md)
titled `## M2 plumbing retrospective` with:
- What shipped (Gate A and/or Gate B)
- What didn't (and why — descope is fine, silent skip is not)
- Approximations that remain (carry the existing list forward, strike
  items you eliminated)
- What v0.3 needs to budget for

If the schema of `compiled_rules.json` changes, document the new
schema in this file too.

## Success criteria checklist

- [ ] All 134 pre-existing tests still pass
- [ ] New test asserts event branch resolves via state.has
- [ ] Event item count appended to items.json (~115 new entries)
- [ ] Event location count appended to locations.json
- [ ] Event items' AP IDs disjoint from existing ranges
- [ ] Generation smoke test produces a fresh seed
- [ ] `compiled_rules.json` includes per-event `rule` field
- [ ] `Rules.py`'s `completion_condition` reads `victory_condition` from compiled output
- [ ] (Gate B) Regions.py wires cross-region edges instead of star topology
- [ ] (Gate B) Options.py exposes TrickLevel Choice
- [ ] (Gate B) Three precompiled trick-level files exist
- [ ] Stale docstrings refreshed
- [ ] `__version__` bumped to `0.0.1-phase4-logic-m2`
- [ ] M2 plumbing retro appended to `docs/randovania-logic-port-notes.md`
