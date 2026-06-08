"""Translate our compiled Dread logic into a PopTracker pack.

Reads the canonical artifacts the apworld already produces

    apworld/dread/data/compiled_rules.json   per-location item-only rule AST
                                             + region_access + victory_condition
    apworld/dread/data/items.json            AP item table (names, pools, groups)
    apworld/dread/data/locations.json        149 pickups (region/scenario/type)
    apworld/dread/data/regions.json          8 areas

and emits a self-contained PopTracker pack (default ``poptracker_pack/``):

    manifest.json
    scripts/init.lua          loads everything (PopTracker auto-runs this)
    scripts/logic.lua         has()/cnt() helpers + one access fn per location
    items/items.json          toggles / consumables / progressives / trick dials
    locations/locations.json  every pickup, each gated by a $access_<id> rule
    maps/maps.json            one placeholder map; pins auto-arranged by region
    layouts/tracker.json      a minimal but valid default layout

WHAT IS AND ISN'T TRANSLATED
----------------------------
The logic is translated faithfully and completely. Each location's reachability
is ``region_access[region] AND per_location_rule`` (matching Regions.py +
Rules.py), compiled to a Lua boolean. The full AST vocabulary is handled:
``item`` (with amount), ``trick`` (symbolic level dials), ``sum`` (missile /
power-bomb capacity), ``damage_threshold`` (suit-or-E-tank-budget), ``and`` /
``or`` / ``trivial`` / ``impossible``. Events never appear — the forward
resolver already inlined them into item-only costs.

What is left to the pack author (art, not logic): the real Dread map image and
per-pin coordinates, and item icons. The generator lays pins out on a labelled
placeholder grid and points icons at ``images/items/<code>.png`` so the pack
loads and is navigable immediately; drop in art and reposition.

Run:  python scripts/export_poptracker.py [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "apworld" / "dread" / "data"

# Mirror of apworld/dread/Tricks.py — short_name -> human label. Kept inline so
# the exporter has no import dependency on the apworld package (which pulls AP).
TRICKS: list[tuple[str, str]] = [
    ("Knowledge", "Knowledge"),
    ("Movement", "Movement"),
    ("Combat", "Combat"),
    ("Pseudo", "Pseudo-Wave Beam"),
    ("IBJ", "Infinite Bomb Jump"),
    ("WBJ", "Water Bomb Jump"),
    ("WSJ", "Water Space Jump"),
    ("SWJ", "Single-wall Wall Jump"),
    ("Slide", "Slide Jump"),
    ("Speedbooster", "Speed Booster Conservation"),
    ("Walljump", "Wall Jump"),
    ("Suitless", "Heat/Cold Runs"),
    ("RGrapple", "Reverse Grapple Block"),
    ("DBoost", "Damage Boost"),
    ("FrozenEnemy", "Stand on Frozen Enemy"),
    ("GrappleMovement", "Grapple Movement"),
    ("CrossSkip", "Cross Bomb Skip"),
    ("TunnelSlope", "Climb Sloped Tunnels"),
    ("ShortBoost", "Short Boost"),
    ("DiffusionAbuse", "Diffusion Abuse"),
    ("FlashSkip", "Flash Shift Skip"),
    ("DBJ", "Diagonal Bomb Jump"),
    ("LedgeWarp", "Ledge Warp"),
    ("CBL", "Cross Bomb Launch"),
    ("FloorClip", "Floor Clip"),
    ("SlopeClimb", "Climb Sloped Surfaces"),
]
TRICK_LEVEL_NAMES = ["Disabled", "Beginner", "Intermediate",
                     "Advanced", "Expert", "Mastery"]
MAX_TRICK_LEVEL = 5
# Trick atoms in the logic resolve against the seed's per-trick level. AP's
# default global trick level is Beginner (1), so default each dial to stage 1 to
# reproduce a default seed's reachability out of the box.
DEFAULT_TRICK_STAGE = 1

# items.json names that the progressive widgets provide (so they get no
# standalone toggle); derived from each progressive entry's progression_tiers.
# Computed at runtime from items.json, but listed here for reference.


def slug(name: str) -> str:
    """AP item / trick name -> a stable PopTracker code (lua identifier safe)."""
    s = name.lower().replace("+", "_plus")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def trick_code(short_name: str, level: int) -> str:
    return f"trick_{slug(short_name)}_{level}"


# ---------------------------------------------------------------------------
# AST -> Lua boolean expression
# ---------------------------------------------------------------------------

class Translator:
    def __init__(self, name_to_code: dict[str, str]):
        self.name_to_code = name_to_code

    def _code(self, item_name: str) -> str:
        code = self.name_to_code.get(item_name)
        if code is None:
            raise KeyError(
                f"rule references item {item_name!r} absent from items.json"
            )
        return code

    def expr(self, ast: dict) -> str:
        """Return a Lua expression evaluating to a boolean."""
        t = ast["type"]

        if t == "trivial":
            return "true"
        if t == "impossible":
            return "false"

        if t == "item":
            amount = int(ast.get("amount", 1))
            code = self._code(ast["name"])
            if amount <= 1:
                return f'has("{code}")'
            return f'has("{code}", {amount})'

        if t == "trick":
            level = int(ast.get("level", 1))
            level = max(1, min(MAX_TRICK_LEVEL, level))
            return f'has("{trick_code(ast["name"], level)}")'

        if t == "sum":
            # base + Σ per_unit·count(code) >= threshold
            parts = [str(int(ast["base"]))]
            for term in ast["terms"]:
                code = self._code(term["name"])
                parts.append(f'{int(term["per_unit"])} * cnt("{code}")')
            total = " + ".join(parts)
            return f'(({total}) >= {int(ast["threshold"])})'

        if t == "damage_threshold":
            suits = ast.get("suit_options", [])
            ors = [f'has("{self._code(s)}")' for s in suits]
            budget = ('(99 + 100 * cnt("energy_tank") '
                      '+ 25 * cnt("energy_part"))')
            ors.append(f'({budget} >= {int(ast["hp_needed"])})')
            return "(" + " or ".join(ors) + ")"

        if t == "and":
            children = ast["items"]
            if not children:
                return "true"
            return "(" + " and ".join(self.expr(c) for c in children) + ")"

        if t == "or":
            children = ast["items"]
            if not children:
                return "false"
            return "(" + " or ".join(self.expr(c) for c in children) + ")"

        raise ValueError(f"unknown rule AST type: {t!r}")


# ---------------------------------------------------------------------------
# Pack pieces
# ---------------------------------------------------------------------------

def build_items_json(items: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Return (poptracker items list, name->code map covering every item)."""
    name_to_code: dict[str, str] = {}
    progressive_tiers: dict[str, list[str]] = {}
    for it in items:
        name_to_code[it["name"]] = slug(it["name"])
        tiers = it.get("progression_tiers") or []
        if tiers:
            progressive_tiers[it["name"]] = list(tiers)

    # Tier item names are provided by the progressive widget; no standalone item.
    covered_by_progressive: set[str] = set()
    for tiers in progressive_tiers.values():
        covered_by_progressive.update(tiers)

    # Items whose logic uses count(): make them consumable so the UI shows a
    # counter. Everything else with a pool count > 1 also reads better as a
    # consumable. Single-pickup unlocks become toggles.
    COUNTED = {
        "Energy Tank", "Energy Part", "Missile Tank", "Missile+ Tank",
        "Power Bomb Tank", "Flash Shift Upgrade", "Speed Booster Upgrade",
    }

    out: list[dict] = []

    # Metroid DNA: 12 distinct AP items, but logic only cares about the count
    # toward the goal — one consumable dial (0..12).
    dna_items = [it for it in items if it["name"].startswith("Metroid DNA")]
    other_items = [it for it in items if not it["name"].startswith("Metroid DNA")]

    for it in other_items:
        name = it["name"]
        code = slug(name)

        if name in progressive_tiers:
            # Progressive widget. Cumulative codes: stage k provides tiers 1..k,
            # so has("wave_beam") is only true once you've reached that stage
            # (which also implies the earlier tiers) — matches AP's mirror.
            tiers = progressive_tiers[name]
            stages = []
            acc: list[str] = []
            for tier in tiers:
                acc = acc + [slug(tier)]
                stages.append({
                    "name": tier,
                    "codes": ",".join(acc),
                    "img": f"images/items/{slug(tier)}.png",
                })
            out.append({
                "name": name,
                "type": "progressive",
                "allow_disabled": True,
                "initial_stage_idx": 0,
                "stages": stages,
            })
            continue

        if name in covered_by_progressive:
            # Provided by a progressive widget above; skip standalone.
            continue

        if name in COUNTED:
            out.append({
                "name": name,
                "type": "consumable",
                "codes": code,
                "img": f"images/items/{code}.png",
                "min_quantity": 0,
                "max_quantity": int(it.get("pool_count", 1)),
                "increment": 1,
            })
            continue

        # Plain unlock.
        out.append({
            "name": name,
            "type": "toggle",
            "codes": code,
            "img": f"images/items/{code}.png",
        })

    if dna_items:
        out.append({
            "name": "Metroid DNA",
            "type": "consumable",
            "codes": "metroid_dna",
            "img": "images/items/metroid_dna.png",
            "min_quantity": 0,
            "max_quantity": len(dna_items),
            "increment": 1,
        })

    # Trick dials — one progressive per trick, stages Disabled..Mastery, with
    # cumulative codes so has("trick_x_2") is true at Intermediate+.
    for short_name, label in TRICKS:
        stages = [{"name": f"{label}: Disabled", "codes": ""}]
        acc: list[str] = []
        for lvl in range(1, MAX_TRICK_LEVEL + 1):
            acc = acc + [trick_code(short_name, lvl)]
            stages.append({
                "name": f"{label}: {TRICK_LEVEL_NAMES[lvl]}",
                "codes": ",".join(acc),
            })
        out.append({
            "name": f"Trick: {label}",
            "type": "progressive",
            "allow_disabled": False,
            "initial_stage_idx": DEFAULT_TRICK_STAGE,
            "stages": stages,
        })

    return out, name_to_code


def build_logic_lua(
    rules: dict[str, dict],
    region_access: dict[str, dict],
    victory: dict,
    locations: list[dict],
    translator: Translator,
) -> str:
    """One access function per location + helpers, as a Lua source string."""
    loc_by_name = {l["name"]: l for l in locations}

    lines: list[str] = []
    lines.append("-- AUTO-GENERATED by scripts/export_poptracker.py")
    lines.append("-- Dread access logic translated from compiled_rules.json.")
    lines.append("-- Do not edit by hand; re-run the exporter instead.")
    lines.append("")
    lines.append("function has(code, n)")
    lines.append("  n = n or 1")
    lines.append("  return Tracker:ProviderCountForCode(code) >= n")
    lines.append("end")
    lines.append("")
    lines.append("function cnt(code)")
    lines.append("  return Tracker:ProviderCountForCode(code)")
    lines.append("end")
    lines.append("")
    lines.append("-- Required Metroid DNA for the goal (seed option, 0..12).")
    lines.append("-- Edit to match your YAML's required_artifacts.")
    lines.append("DREAD_REQUIRED_DNA = 0")
    lines.append("")

    def emit(fn_name: str, expr: str, comment: str) -> None:
        lines.append(f"-- {comment}")
        lines.append(f"function {fn_name}()")
        lines.append(f"  if {expr} then return 1 end")
        lines.append("  return 0")
        lines.append("end")
        lines.append("")

    for loc_name, rule_ast in rules.items():
        loc = loc_by_name.get(loc_name)
        if loc is None:
            continue
        fn = f"access_{loc['ap_id']}"
        rule_expr = translator.expr(rule_ast)
        region_ast = region_access.get(loc["region"], {"type": "trivial"})
        region_expr = translator.expr(region_ast)
        if region_expr == "true":
            expr = rule_expr
        else:
            expr = f"({region_expr} and {rule_expr})"
        emit(fn, expr, loc_name)

    # Locations with no compiled rule (boss / EMMI / cutscene / corex) are gated
    # only by their region's access (Rules.py adds no per-pickup rule for them).
    for loc in locations:
        if loc["name"] in rules:
            continue
        if loc.get("pickup_type") == "event":
            continue
        fn = f"access_{loc['ap_id']}"
        region_ast = region_access.get(loc["region"], {"type": "trivial"})
        expr = translator.expr(region_ast)
        emit(fn, expr, f"{loc['name']} (region-gated only)")

    # Goal.
    victory_expr = translator.expr(victory)
    emit("access_goal",
         f"({victory_expr} and cnt(\"metroid_dna\") >= DREAD_REQUIRED_DNA)",
         "Goal: defeat Raven Beak (+ required Metroid DNA)")

    return "\n".join(lines) + "\n"


def build_locations_json(
    locations: list[dict], regions: list[dict],
) -> list[dict]:
    """Region-grouped location tree with $access rules + placeholder pin coords.

    Pins are auto-arranged on the placeholder map (one column band per region)
    so nothing overlaps before real map art exists.
    """
    region_order = [r["name"] for r in regions]
    by_region: dict[str, list[dict]] = {r: [] for r in region_order}
    for loc in locations:
        if loc.get("pickup_type") == "event":
            continue
        by_region.setdefault(loc["region"], []).append(loc)

    # Arrange pins on the placeholder map: one column band per region, pins
    # stacked vertically. Pure layout convenience so nothing overlaps before
    # real art exists.
    MAP_W, COL_PAD, ROW_PAD, TOP = 1600, 200, 34, 60
    out: list[dict] = []
    for col, region in enumerate(region_order):
        locs = by_region.get(region, [])
        children = []
        x = COL_PAD // 2 + col * COL_PAD
        for row, loc in enumerate(locs):
            short = loc["name"].split(": ", 1)[-1]
            y = TOP + row * ROW_PAD
            children.append({
                "name": short,
                "access_rules": [f"$access_{loc['ap_id']}"],
                "map_locations": [
                    {"map": "dread", "x": x, "y": y}
                ],
                "sections": [{"name": short}],
            })
        out.append({"name": region, "children": children})

    # Goal as its own top-level check.
    out.append({
        "name": "Goal",
        "children": [{
            "name": "Defeat Raven Beak",
            "access_rules": ["$access_goal"],
            "map_locations": [{"map": "dread", "x": MAP_W - 80, "y": 60}],
            "sections": [{"name": "Raven Beak"}],
        }],
    })
    return out


def build_maps_json() -> list[dict]:
    return [{
        "name": "dread",
        "location_size": 18,
        "location_border_thickness": 2,
        "img": "images/maps/dread.png",
    }]


def build_layout_json(item_grid: list[str], trick_grid: list[str]) -> dict:
    """A minimal but valid default layout: tabbed Map / Items / Tricks.

    ``item_grid`` and ``trick_grid`` are lists of item CODES (PopTracker matches
    a grid cell to whichever item provides that code — for a progressive that is
    its first-stage code). itemgrid takes ``rows`` (a 2-D array), so we chunk.
    """
    def rows(codes: list[str], per_row: int = 6) -> list[list[str]]:
        return [codes[i:i + per_row] for i in range(0, len(codes), per_row)] or [[]]

    return {
        "tracker_default": {
            "type": "container",
            "background": "#222222",
            "content": {
                "type": "tabbed",
                "tabs": [
                    {
                        "title": "Map",
                        "content": {"type": "map", "maps": ["dread"]},
                    },
                    {
                        "title": "Items",
                        "content": {
                            "type": "itemgrid",
                            "item_size": 32,
                            "rows": rows(item_grid),
                        },
                    },
                    {
                        "title": "Tricks",
                        "content": {
                            "type": "itemgrid",
                            "item_size": 32,
                            "rows": rows(trick_grid),
                        },
                    },
                ]
            }
        }
    }


def build_init_lua() -> str:
    return (
        "-- AUTO-GENERATED by scripts/export_poptracker.py\n"
        "ScriptHost:LoadScript(\"scripts/logic.lua\")\n"
        "Tracker:AddItems(\"items/items.json\")\n"
        "Tracker:AddMaps(\"maps/maps.json\")\n"
        "Tracker:AddLocations(\"locations/locations.json\")\n"
        "Tracker:AddLayouts(\"layouts/tracker.json\")\n"
    )


PACK_README = """\
# Metroid Dread (Archipelago) — PopTracker pack

**Auto-generated** by `scripts/export_poptracker.py` in the dread_ap repo.
Re-run that exporter to regenerate after the logic changes; do not hand-edit
`scripts/logic.lua`, `items/items.json`, or `locations/locations.json`.

## What's done (the logic)
- Every pickup's reachability is translated from the apworld's
  `compiled_rules.json` into a Lua access function (`scripts/logic.lua`),
  composed as `region_access[region] AND per-location rule` to match the
  generator. Items, item counts, missile/power-bomb capacity sums, suit /
  E-tank damage budgets, and per-trick difficulty dials are all handled.
- Items, progressives (suit/beam/missile/etc.), counted expansions, the
  Metroid DNA goal counter, and a difficulty dial per trick are defined.

## What's left to you (art + tuning)
- **Map image + pin coordinates.** Drop the real Dread map at
  `images/maps/dread.png` and reposition pins (they're auto-arranged on a
  labelled placeholder grid by region so nothing overlaps).
- **Item icons** under `images/items/<code>.png`.
- **Goal DNA count.** Edit `DREAD_REQUIRED_DNA` at the top of
  `scripts/logic.lua` to match your YAML's `required_artifacts`.
- **Trick defaults.** Each trick dial defaults to Beginner (AP's default
  global trick level). Adjust per seed.

## Note on progressive vs. non-progressive seeds
Beams/suits/etc. are modeled as PopTracker *progressive* widgets with
cumulative codes (so having Wave Beam implies Plasma + Wide), matching AP's
default opt-out progressives. If your seed disables a progressive group, the
individual tiers can be picked up out of order — represent that by clicking the
widget to the highest tier you hold.
"""


def build_manifest() -> dict:
    return {
        "name": "Metroid Dread (Archipelago)",
        "game_name": "Metroid Dread",
        "package_uid": "metroid_dread_ap",
        "package_version": "0.1.0",
        # Manual tracker — no console autotracker exists for Switch/Dread.
        "platform": "",
        "variants": {
            "standard": {"display_name": "Standard"}
        },
    }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR,
                    help="dir holding compiled_rules.json + items/locations/regions")
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "poptracker_pack",
                    help="output pack directory")
    args = ap.parse_args(argv)

    data = args.data_dir
    compiled_path = data / "compiled_rules.json"
    if not compiled_path.exists():
        print(f"missing {compiled_path}. Generate it first:\n"
              f"  python scripts/extract_dread_rules.py --all", flush=True)
        return 2

    compiled = json.loads(compiled_path.read_text())
    sv = compiled.get("schema_version")
    if sv != 3:
        print(f"warning: compiled_rules.json schema_version={sv!r} (expected 3); "
              f"trick/sum/damage atoms may differ.", flush=True)

    items = json.loads((data / "items.json").read_text())
    locations = json.loads((data / "locations.json").read_text())
    regions = json.loads((data / "regions.json").read_text())

    items_out, name_to_code = build_items_json(items)
    translator = Translator(name_to_code)

    rules = compiled.get("rules", {})
    region_access = compiled.get("region_access", {})
    victory = compiled.get("victory_condition", {"type": "trivial"})

    logic_lua = build_logic_lua(rules, region_access, victory, locations,
                                translator)
    locations_out = build_locations_json(locations, regions)
    maps_out = build_maps_json()

    # Item / trick grids reference items by CODE. For a progressive, use its
    # first non-empty stage's first code (PopTracker resolves the widget from any
    # code it provides).
    def first_code(it: dict) -> str | None:
        if it.get("type") in ("toggle", "consumable"):
            return it.get("codes") or None
        if it.get("type") == "progressive":
            for stage in it.get("stages", []):
                if stage.get("codes"):
                    return stage["codes"].split(",")[0]
        return None

    item_grid, trick_grid = [], []
    for it in items_out:
        code = first_code(it)
        if not code:
            continue
        (trick_grid if it["name"].startswith("Trick: ") else item_grid).append(code)
    layout_out = build_layout_json(item_grid, trick_grid)

    out = args.out
    (out / "scripts").mkdir(parents=True, exist_ok=True)
    (out / "items").mkdir(parents=True, exist_ok=True)
    (out / "locations").mkdir(parents=True, exist_ok=True)
    (out / "maps").mkdir(parents=True, exist_ok=True)
    (out / "layouts").mkdir(parents=True, exist_ok=True)
    (out / "images" / "items").mkdir(parents=True, exist_ok=True)
    (out / "images" / "maps").mkdir(parents=True, exist_ok=True)

    (out / "README.md").write_text(PACK_README)
    (out / "manifest.json").write_text(json.dumps(build_manifest(), indent=2))
    (out / "scripts" / "init.lua").write_text(build_init_lua())
    (out / "scripts" / "logic.lua").write_text(logic_lua)
    (out / "items" / "items.json").write_text(json.dumps(items_out, indent=2))
    (out / "locations" / "locations.json").write_text(
        json.dumps(locations_out, indent=2))
    (out / "maps" / "maps.json").write_text(json.dumps(maps_out, indent=2))
    (out / "layouts" / "tracker.json").write_text(
        json.dumps(layout_out, indent=2))

    n_access = logic_lua.count("\nfunction access_")
    print(f"wrote pack -> {out}")
    print(f"  items:     {len(items_out)} widgets "
          f"({sum(1 for i in items_out if i['type']=='progressive')} progressive)")
    print(f"  locations: {sum(len(r.get('children', [])) for r in locations_out)} "
          f"checks across {len(locations_out)} groups")
    print(f"  logic.lua: {n_access} access functions")
    print("  TODO (art, not logic): drop a real map at images/maps/dread.png, "
          "reposition pins, add item icons under images/items/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
