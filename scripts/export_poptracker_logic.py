"""Inject Dread access logic into an *existing* PopTracker pack.

Unlike ``export_poptracker.py`` — which emits a fresh, self-contained
placeholder pack — this script bridges our compiled logic into a pack that
already has its own art, maps, item codes and location tree (e.g.
ZobeePlays/Metroid-Dread-AP-Poptracker). It is intentionally coupled to that
pack's conventions:

  * locations are addressed by **AP location id** (the pack's
    ``scripts/autotracking/location_mapping.lua`` maps id -> ``@Region/Room[/Section]``),
    and those ids match our ``apworld/dread/data/locations.json`` 1:1;
  * items are addressed by the pack's **own item codes** (``widebeam``,
    ``energytank``, ``missile+tank`` ...), so we carry an explicit
    AP-item-name -> pack-code map rather than slugging.

What it writes into ``--pack``:

  scripts/logic/ap_logic.lua   one ``access_<apid>()`` per location (+ goal),
                               translated from compiled_rules.json. Composed as
                               ``region_access[region] AND per-location rule`` to
                               match Regions.py + Rules.py. Returns an
                               AccessibilityLevel.
  items/items.json             APPENDS one trick dial per Randovania trick
                               (consumable 0..5; the count IS the difficulty
                               level, shown as a number overlay). Existing items
                               are left untouched.
  locations/*.json             sets every leaf section's ``access_rules`` to
                               ``["$access_<apid>"]`` (by id, via the pack's
                               location_mapping). Art / coords / names untouched.

Trick atoms resolve against ``dcnt("trick_<short>") >= level``; the dials are
pre-populated from slot_data by the pack's ``onClear`` (see the companion
archipelago.lua edit). Items the rules never reference (DNA, bosses) are left to
the pack.

Run:  python scripts/export_poptracker_logic.py --pack <path-to-pack>
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "apworld" / "dread" / "data"

# AP item name -> the pack's own item code. Every name that appears in
# compiled_rules.json (item / sum-term / damage-suit atoms) must be present;
# the script asserts this at runtime.
ITEM_CODE = {
    "Morph Ball": "morphball",
    "Bomb": "bomb",
    "Cross Bomb": "crossbomb",
    "Power Bomb": "powerbomb",
    "Power Bomb Tank": "powerbombtank",
    "Charge Beam": "chargebeam",
    "Wide Beam": "widebeam",
    "Plasma Beam": "plasmabeam",
    "Wave Beam": "wavebeam",
    "Diffusion Beam": "diffusionbeam",
    "Grapple Beam": "grapplebeam",
    "Super Missile": "supermissile",
    "Ice Missile": "icemissile",
    "Storm Missile": "stormmissile",
    "Missile Tank": "missiletank",
    "Missile+ Tank": "missile+tank",
    "Varia Suit": "variasuit",
    "Gravity Suit": "gravitysuit",
    "Phantom Cloak": "phantomcloak",
    "Flash Shift": "flashshift",
    "Flash Shift Upgrade": "flashshiftupgrade",
    "Speed Booster": "speedbooster",
    "Speed Booster Upgrade": "speedboosterupgrade",
    "Spider Magnet": "spidermagnet",
    "Spin Boost": "spinboost",
    "Space Jump": "spacejump",
    "Screw Attack": "screwattack",
    "Slide": "slide",
    "Energy Tank": "energytank",
    "Energy Part": "energypart",
}

# Randovania trick short_name -> (long label, pack trick code). The code is the
# consumable whose acquired count is the trick's difficulty level. Mirrors
# apworld/dread/Tricks.py (kept inline so the exporter doesn't import the
# AP-dependent apworld package).
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
TRICK_ICON = "images/items/Huh.png"  # placeholder; pack author can swap


def trick_code(short_name: str) -> str:
    return "trick_" + short_name.lower()


# ---------------------------------------------------------------------------
# AST -> Lua boolean expression (pack-code flavour)
# ---------------------------------------------------------------------------

class Translator:
    def _code(self, name: str) -> str:
        code = ITEM_CODE.get(name)
        if code is None:
            raise KeyError(f"rule references item {name!r} with no pack code "
                           f"(add it to ITEM_CODE)")
        return code

    def expr(self, ast: dict) -> str:
        t = ast["type"]
        if t == "trivial":
            return "true"
        if t == "impossible":
            return "false"
        if t == "item":
            amount = int(ast.get("amount", 1))
            code = self._code(ast["name"])
            if amount <= 1:
                return f'dhas("{code}")'
            return f'dhas("{code}", {amount})'
        if t == "trick":
            level = max(1, min(MAX_TRICK_LEVEL, int(ast.get("level", 1))))
            return f'(dcnt("{trick_code(ast["name"])}") >= {level})'
        if t == "sum":
            parts = [str(int(ast["base"]))]
            for term in ast["terms"]:
                parts.append(f'{int(term["per_unit"])} * dcnt("{self._code(term["name"])}")')
            return f'(({" + ".join(parts)}) >= {int(ast["threshold"])})'
        if t == "damage_threshold":
            ors = [f'dhas("{self._code(s)}")' for s in ast.get("suit_options", [])]
            budget = '(99 + 100 * dcnt("energytank") + 25 * dcnt("energypart"))'
            ors.append(f'({budget} >= {int(ast["hp_needed"])})')
            return "(" + " or ".join(ors) + ")"
        if t == "and":
            ch = ast["items"]
            return "(" + " and ".join(self.expr(c) for c in ch) + ")" if ch else "true"
        if t == "or":
            ch = ast["items"]
            return "(" + " or ".join(self.expr(c) for c in ch) + ")" if ch else "false"
        raise ValueError(f"unknown rule AST type: {t!r}")


# ---------------------------------------------------------------------------
# ap_logic.lua
# ---------------------------------------------------------------------------

def build_logic_lua(compiled: dict, locations: list[dict], tr: Translator) -> str:
    rules = compiled.get("rules", {})
    region_access = compiled.get("region_access", {})
    victory = compiled.get("victory_condition", {"type": "trivial"})
    loc_by_name = {l["name"]: l for l in locations}

    L: list[str] = [
        "-- AUTO-GENERATED by scripts/export_poptracker_logic.py (dread_ap).",
        "-- Dread access logic translated from compiled_rules.json; do not hand-edit.",
        "-- Composed as region_access[region] AND per-location rule (Regions.py + Rules.py).",
        "",
        "local NORMAL = AccessibilityLevel.Normal",
        "local NONE = AccessibilityLevel.None",
        "",
        "function dhas(code, n)",
        "  return Tracker:ProviderCountForCode(code) >= (n or 1)",
        "end",
        "",
        "function dcnt(code)",
        "  return Tracker:ProviderCountForCode(code)",
        "end",
        "",
        "-- Required Metroid DNA for the goal; set from slot_data by onClear.",
        "DREAD_REQUIRED_DNA = DREAD_REQUIRED_DNA or 0",
        "",
        "local function dna_count()",
        "  local n = 0",
        "  for i = 1, 12 do n = n + dcnt('metroiddna' .. i) end",
        "  return n",
        "end",
        "",
    ]

    def emit(fn: str, expr: str, comment: str) -> None:
        L.append(f"-- {comment}")
        L.append(f"function {fn}()")
        L.append(f"  if {expr} then return NORMAL end")
        L.append("  return NONE")
        L.append("end")
        L.append("")

    n = 0
    for loc in locations:
        if loc.get("pickup_type") == "event":
            continue
        name = loc["name"]
        region_ast = region_access.get(loc["region"], {"type": "trivial"})
        region_expr = tr.expr(region_ast)
        rule_ast = rules.get(name)
        if rule_ast is not None:
            rule_expr = tr.expr(rule_ast)
            expr = rule_expr if region_expr == "true" else f"({region_expr} and {rule_expr})"
            tag = name
        else:
            # boss / EMMI / cutscene with no per-pickup rule: region-gated only.
            expr = region_expr
            tag = f"{name} (region-gated only)"
        emit(f"access_{loc['ap_id']}", expr, tag)
        n += 1

    victory_expr = tr.expr(victory)
    emit("access_goal",
         f"({victory_expr} and dna_count() >= DREAD_REQUIRED_DNA)",
         "Goal: defeat Raven Beak (+ required Metroid DNA). Not wired to a pin "
         "by default — available if a goal location is added.")

    # short_name -> trick code, for slot_data pre-population in onClear.
    L.append("-- slot_data trick short_name -> dial code (consumed by onClear).")
    L.append("DREAD_TRICK_CODES = {")
    for short, _ in TRICKS:
        L.append(f'  ["{short}"] = "{trick_code(short)}",')
    L.append("}")
    L.append("")
    return "\n".join(L) + "\n", n


# ---------------------------------------------------------------------------
# items.json — append trick dials
# ---------------------------------------------------------------------------

def trick_items() -> list[dict]:
    out = []
    for short, label in TRICKS:
        out.append({
            "name": f"Trick: {label}",
            "type": "consumable",
            "img": TRICK_ICON,
            "codes": trick_code(short),
            "min_quantity": 0,
            "max_quantity": MAX_TRICK_LEVEL,
            "increment": 1,
            "overlay_align": "bottom",
        })
    return out


# ---------------------------------------------------------------------------
# locations/*.json — wire access_rules by AP id
# ---------------------------------------------------------------------------

def parse_location_mapping(pack: Path) -> dict[int, str]:
    txt = (pack / "scripts" / "autotracking" / "location_mapping.lua").read_text()
    pairs = re.findall(r'\[(\d+)\]\s*=\s*\{\"([^\"]+)\"', txt)
    return {int(i): p for i, p in pairs}


def find_section(tops: dict, path: str):
    """Resolve an ``@Region/Room[/Section]`` path to a section dict."""
    parts = path.lstrip("@").split("/")
    top = parts[0]
    for node in tops.get(top, []):
        cur = node
        ok = True
        for comp in parts[1:-1]:
            children = {c["name"]: c for c in cur.get("children", [])}
            if comp in children:
                cur = children[comp]
            else:
                ok = False
                break
        if not ok:
            continue
        last = parts[-1]
        secs = {s.get("name", ""): s for s in cur.get("sections", [])}
        if last in secs:
            return secs[last]
        # 2-component path whose leaf is the room itself -> its default section
        children = {c["name"]: c for c in cur.get("children", [])}
        if last in children and children[last].get("sections"):
            return children[last]["sections"][0]
    return None


def wire_locations(pack: Path, id_to_apfn: dict[int, str],
                   loc_mapping: dict[int, str]) -> tuple[int, list[str]]:
    loc_dir = pack / "locations"
    files = sorted(loc_dir.glob("*.json"))
    data_by_file = {f: json.loads(f.read_text()) for f in files}
    tops: dict[str, list] = {}
    for f, data in data_by_file.items():
        for node in data:
            tops.setdefault(node["name"], []).append(node)

    wired = 0
    misses: list[str] = []
    for ap_id, path in loc_mapping.items():
        sec = find_section(tops, path)
        if sec is None:
            misses.append(f"{ap_id} {path} (no section)")
            continue
        sec["access_rules"] = [f"$access_{ap_id}"]
        wired += 1

    for f, data in data_by_file.items():
        f.write_text(json.dumps(data, indent=4))
    return wired, misses


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", type=Path, required=True, help="target PopTracker pack dir")
    ap.add_argument("--data-dir", type=Path, default=DATA_DIR)
    args = ap.parse_args(argv)

    data = args.data_dir
    compiled = json.loads((data / "compiled_rules.json").read_text())
    sv = compiled.get("schema_version")
    if sv != 3:
        print(f"ERROR: compiled_rules.json schema_version={sv!r} (need 3). "
              f"Regenerate: python scripts/extract_dread_rules.py --all")
        return 2
    locations = json.loads((data / "locations.json").read_text())

    tr = Translator()
    logic_lua, n_access = build_logic_lua(compiled, locations, tr)

    pack = args.pack
    (pack / "scripts" / "logic").mkdir(parents=True, exist_ok=True)
    (pack / "scripts" / "logic" / "ap_logic.lua").write_text(logic_lua)

    # Append trick dials, skipping any already present (idempotent re-runs).
    items_path = pack / "items" / "items.json"
    items = json.loads(items_path.read_text())
    have = {it.get("codes") for it in items}
    added = [it for it in trick_items() if it["codes"] not in have]
    items.extend(added)
    items_path.write_text(json.dumps(items, indent=4))

    loc_mapping = parse_location_mapping(pack)
    apfns = {loc["ap_id"]: f"access_{loc['ap_id']}" for loc in locations}
    wired, misses = wire_locations(pack, apfns, loc_mapping)

    print(f"ap_logic.lua: {n_access} access functions (+ goal)")
    print(f"items.json:   +{len(added)} trick dials")
    print(f"locations:    {wired}/{len(loc_mapping)} sections wired")
    if misses:
        print("UNWIRED (investigate):")
        for m in misses:
            print("  ", m)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
