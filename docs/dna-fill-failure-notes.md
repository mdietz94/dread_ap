# DNA-at-bosses fill failure — root cause & fix

**TL;DR.** All three reported symptoms are ONE bug: `Missile Tank` was
classified `useful` (non-advancement), but Archipelago's
`World.collect_item` *skips non-advancement items* — they never enter
`state.prog_items`. So `state.has("Missile Tank")` / `state.count("Missile
Tank")` were permanently `0`, including for the precollected starter copy.
Nearly every compiled location rule — and all four `victory_condition`
disjuncts — carries an `item Missile Tank>=1` atom in every disjunct, so 36
of 149 locations (several bosses among them) were unreachable *with a full
inventory*. Fix: `items.json` Missile Tank `useful` →
`progression_skip_balancing`. One line.

## How the one bug wore three masks

| Reported symptom | Why this root cause produces it |
|---|---|
| `minimal` + DNA + `prefer_bosses`: `Could not access required locations … Missing: [<boss>, <boss>]` | DNA is locked at random boss locations; the goal needs all N DNA. Several bosses' rules require Missile Tank → unreachable. When `random.sample` lands a DNA on a Missile-Tank-gated boss, the goal is unreachable. **Seed-dependent** (some seeds pick only the few reachable bosses and pass — that's why a 20-seed `distribute_items_restrictive` sweep can show 0 failures while the real generator fails). |
| `items` (any DNA/placement, incl. `include_boss_pickups:false`): `No more spots to place N items` | Under `items`/`full`, every location must be reachable. With Missile Tank invisible, 36 locations can never be reached, so `fill_restrictive` runs out of valid spots. **Deterministic, 100%.** |
| `required_artifacts:0`: `Game appears as unbeatable` | `victory_condition` is item-only (events inlined). All 4 disjuncts contain `item Missile Tank>=1`, so `base_victory(state)` is always False. **Same bug, NOT the `sum`/`damage_threshold` nodes.** |

The brief flagged the no-DNA "unbeatable" case as *likely a separate bug,
possibly the new `sum`/`damage_threshold` AST interacting with AP's
beatable-check.* It is **not separate** and the new AST nodes are **not** at
fault — they evaluate correctly. It is the same Missile Tank classification
defect, surfacing through the victory condition instead of through a boss
location.

## The exact item-chain

Take `Burenia: OnHydrogigaDead_CUSTOM` (32 disjuncts). Every disjunct is an
`and[...]` that includes `item Missile Tank>=1`. With a full inventory the
diagnostic prints `item Missile Tank>=1 (have 0)` for all 32 → rule False →
location unreachable. The brief's hand-check called the rule "satisfiable with
1 of each tank," but the live `CollectionState` never holds even 1 Missile
Tank because the classification keeps it out of `prog_items`.

## Why it regressed

Commit `7c35f01` ("Reclassify tank items to match actual logic role") changed
Missile Tank `progression_skip_balancing` → `useful` on this premise (quoted
from its message):

> "it is in BASE_STARTING_ITEMS (precollected), so the atom is satisfied from
> turn 0 … The atom IS still gated; the precollected copy gates it."

False. AP gates `collect_item` on `item.advancement`; a `useful` precollected
item is invisible to logic. Pre-`7c35f01`, Missile Tank was advancement and
`items`/`full` generated (CLAUDE.md's "verified 8/8"). The reclassify silently
broke it; no test exercised a full `items` generation reaching a
Missile-Tank-gated location, and `minimal` only fails on unlucky seeds.

## Where the fix belongs

In **rule/item classification** (`items.json`), not in `Rules.set_rules` DNA
ordering, not in `World.create_items` pool logic, not upstream in AP. AP is
behaving correctly: logic items must be advancement. Keeping all 60 copies
`progression_skip_balancing` (no `MIXED_CLASSIFICATION_FIRST_N` cap) is
deliberate — the `sum` ammo gates (`per_unit` Missile Tank = 2, thresholds up
to 153) need `state.count` to grow with collected tanks.

## The patch

- `apworld/dread/data/items.json`: Missile Tank `useful` →
  `progression_skip_balancing`.
- `apworld/dread/tests/test_item_pool.py`: replaced the
  `test_missile_tank_copies_all_useful` guard (which pinned the buggy state)
  with `test_missile_tank_copies_are_advancement`, documenting the regression.
- `apworld/dread/World.py`: corrected the misleading "precollected ⇒ useful is
  fine" comment.

## Verification

- Full-inventory reachability: 36/149 unreachable → **0/149**.
- In-process `distribute_items_restrictive` sweep, 20 seeds × 7 option combos
  (minimal/items × DNA 0/3 × prefer_bosses/anywhere × boss on/off): all 0
  failures.
- Real `scripts/ap_generate.py` (full `Main` pipeline): the previously-failing
  `minimal`+DNA3 seed, the no-DNA "unbeatable" seed, and the `items` smoke seed
  (3 seeds) all `Done. Enjoy.`
- Test suite: 276 passed, 11 skipped, 1 pre-existing vendor-fixture error
  (missing `vendor/open-dread-rando` checkout — unrelated).
