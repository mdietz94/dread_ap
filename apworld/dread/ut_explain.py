"""Human-readable logic explanation for Universal Tracker (UT).

UT lets a world override how its logic is explained by defining three optional
methods that UT probes for with ``hasattr`` (see UT's ``TrackerClient.py`` and
``worlds/tracker/docs/apworld-integration.md``):

    explain_rule(self, target_name, state)  -> list[JSONMessagePart] | None
        Overrides the ``/explain <location>`` command.
    explain_path(self, entrance, state)     -> list[JSONMessagePart] | None
        Overrides how ONE hop is rendered inside UT's default
        ``/get_logical_path`` walk. Return None to skip the hop, a non-None
        falsy to fall back to UT's default rendering, or a list to print.

Without these, UT falls back to introspecting the raw access-rule object. Our
access rules are opaque lambdas (``Rules.compile_to_lambda``) attached to
entrances named ``e{i}`` between regions named ``R{comp}``, so UT's default
prints things like ``R37 (True): e5 : True`` — the "internal nodes" problem.

This module renders our *symbolic* rule ASTs (the same dicts ``graph_logic``
feeds to ``compile_to_lambda``) into ``JSONMessagePart`` lists, coloring each
atom by evaluating it against the tracker's ``CollectionState``. Truth is
evaluated by reusing ``compile_to_lambda`` itself, so the explanation can never
drift from the logic it describes.

Deliberately Kivy-free and AP-import-light: ``compile_to_lambda`` is duck-typed
on ``state`` and ``Tricks`` is pure data, so the renderer is unit-testable with a
tiny fake state and no Archipelago install. The ``DreadWorld`` glue that wires
these into UT lives in ``World.py``; the per-entrance AST + component-label maps
are stashed on the world by ``graph_logic.build_regions``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .Rules import compile_to_lambda
from .Tricks import DREAD_TRICKS, TRICK_LEVEL_NAMES

# ---------------------------------------------------------------------------
# Event-name humanizing.
#
# Randovania names most Dread events in readable CamelCase
# ("ArtariaEmmiMagnetPlatformLowered"); unnamed events fall back to a
# ``scenario:subarea:actor`` id ("s010_cave:default:PRP_DB_CV_006"). We render
# both into something a player can read. Functional Dread ids (scenario codes,
# actor names) are safe to commit (CLAUDE.md).
# ---------------------------------------------------------------------------
_SCENARIO_REGION = {
    "s010_cave": "Artaria", "s020_magma": "Cataris", "s030_baselab": "Dairon",
    "s040_aqua": "Burenia", "s050_forest": "Ghavoran", "s060_quarantine": "Elun",
    "s070_basesanc": "Ferenia", "s080_shipyard": "Hanubia", "s090_skybase": "Itorash",
}

# Substring expansions for common glued lowercase actor tails, applied before
# word-splitting. Order matters (longest / most-specific first).
_EVENT_TOKEN_SUBS = [
    ("grapplepulloff", "grapple pull-off "),
    ("chainreaction", "chain reaction "),
    ("deviceheat", "heat device "),
    ("breakable", "breakable "),
]

# Whole-word expansions applied after splitting.
_EVENT_WORD_SUBS = {"CU": "Central Unit", "Emmi": "EMMI", "PB": "Power Bomb"}


def _split_words(s: str) -> str:
    """Split a CamelCase / snake_case identifier into spaced words, dropping a
    trailing numeric suffix (``_006``, ``1x2``, ``017``)."""
    for needle, repl in _EVENT_TOKEN_SUBS:
        s = s.replace(needle, repl)
    s = re.sub(r"[_\-]+", " ", s)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)      # camel boundary
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)    # ACRONYMWord boundary
    words = []
    for w in s.split():
        w = re.sub(r"\d+$", "", w)                     # strip trailing digits
        if w and not re.fullmatch(r"[0-9x]+", w):      # drop pure "1x2" tokens
            words.append(_EVENT_WORD_SUBS.get(w, w))
    return " ".join(words)


def humanize_event(name: str) -> str:
    """Render a raw event name into a human label.

    ``scenario:subarea:actor`` -> ``"<Region>: <cleaned actor>"`` (region from
    the scenario code); CamelCase RDV events -> spaced words (the region is
    already embedded, e.g. "Artaria Emmi ..."). Opaque actor codes survive as a
    cleaned, region-prefixed remnant rather than a raw colon id."""
    if ":" in name:
        parts = name.split(":")
        region = _SCENARIO_REGION.get(parts[0], parts[0])
        actor = _split_words(parts[-1])
        return f"{region}: {actor}" if actor else region
    return _split_words(name) or name

# JSONMessagePart colors. The satisfied/unsatisfied pair mirrors AP's own
# rule-explanation convention (green / salmon); cyan marks "state unknown".
_GREEN = "green"
_SALMON = "salmon"
_UNKNOWN = "cyan"
_LABEL = "blue"

_TRICK_LONG = {t.short_name: t.long_name for t in DREAD_TRICKS}

# JSONMessagePart = dict; kept as plain dicts so this module never imports
# NetUtils (which would pull the AP runtime into unit tests).
JSONMessagePart = dict


def _text(s: str) -> JSONMessagePart:
    return {"type": "text", "text": s}


def _colored(s: str, color: str) -> JSONMessagePart:
    return {"type": "color", "color": color, "text": s}


@dataclass
class ExplainCtx:
    """Everything the renderer needs to evaluate + label an AST for one slot.

    Mirrors the arguments ``graph_logic`` passes to ``compile_to_lambda`` so a
    rendered atom's color matches its real reachability contribution.
    """
    player: int
    trick_levels: dict[str, int]
    energy_per_tank: int
    ammo_amounts: dict[str, int]
    door_lock_rando: bool
    # comp id -> human label ("Artaria: Charge Beam Room"); default "R{comp}".
    comp_label: Callable[[int], str] = lambda c: f"R{c}"
    # raw event name -> human label; humanizes RDV's functional ids by default.
    event_label: Callable[[str], str] = field(default=lambda n: humanize_event(n))

    def truth(self, ast: dict, state: Any) -> bool:
        pred = compile_to_lambda(
            ast, self.player, self.trick_levels, graph_mode=True,
            energy_per_tank=self.energy_per_tank,
            ammo_amounts=self.ammo_amounts,
            door_lock_rando=self.door_lock_rando)
        return bool(pred(state))


def _resource_label(terms: list[dict]) -> str:
    names = " ".join(t.get("name", "") for t in terms)
    if "Power Bomb" in names:
        return "Power Bombs"
    if "Missile" in names:
        return "Missiles"
    return "capacity"


def _atom_color(ctx: ExplainCtx, ast: dict, state: Any) -> str:
    if state is None:
        return _UNKNOWN
    return _GREEN if ctx.truth(ast, state) else _SALMON


def render_ast(ast: dict, ctx: ExplainCtx, state: Any) -> list[JSONMessagePart]:
    """Render a symbolic rule AST into a ``JSONMessagePart`` list.

    ``state`` may be None (structure only, atoms colored ``cyan``); otherwise
    each leaf is colored green/salmon by evaluating it against ``state``.
    """
    t = ast.get("type")

    if t == "trivial":
        return [_colored("always", _GREEN)]
    if t == "impossible":
        return [_colored("never", _SALMON)]

    if t == "item":
        amount = int(ast.get("amount", 1))
        label = ast["name"] if amount <= 1 else f"{ast['name']} x{amount}"
        return [_colored(label, _atom_color(ctx, ast, state))]

    if t == "event":
        return [_text("reach "),
                _colored(ctx.event_label(ast["name"]),
                         _atom_color(ctx, ast, state))]

    if t == "trick":
        short = ast["name"]
        level = int(ast.get("level", 1))
        long = _TRICK_LONG.get(short, short)
        level_name = TRICK_LEVEL_NAMES.get(level, str(level))
        enabled = level <= ctx.trick_levels.get(short, 0)
        return [_colored(f"trick: {long} [{level_name}]",
                         _GREEN if enabled else _SALMON)]

    if t == "misc":
        # DoorLocks-family flag; state-independent (resolves to a constant).
        name = ("not " if ast.get("negate") else "") + str(ast.get("name"))
        return [_colored(name, _atom_color(ctx, ast, state))]

    if t == "sum":
        label = f"{_resource_label(ast['terms'])} ≥ {int(ast['threshold'])}"
        return [_colored(label, _atom_color(ctx, ast, state))]

    if t == "damage_threshold":
        suits = list(ast.get("suit_options", []))
        alt = (" / ".join(suits) + " or energy") if suits else "energy budget"
        label = f"survive {int(ast['hp_needed'])} dmg ({alt})"
        return [_colored(label, _atom_color(ctx, ast, state))]

    if t in ("and", "or"):
        items = ast.get("items", [])
        if not items:
            return ([_colored("always", _GREEN)] if t == "and"
                    else [_colored("never", _SALMON)])
        sep = " & " if t == "and" else " | "
        parts: list[JSONMessagePart] = [_text("(")]
        for i, child in enumerate(items):
            if i:
                parts.append(_text(sep))
            parts.extend(render_ast(child, ctx, state))
        parts.append(_text(")"))
        return parts

    # Unknown atom type: don't crash an interactive command — show it raw.
    return [_text(str(ast))]


def build_comp_labels(graph: dict) -> dict[int, str]:
    """Best-effort human label per graph component, from the data already in
    ``logic_graph.json``. A component that holds a pickup is labeled by that
    pickup's AP location name ("Artaria: Charge Beam Room"); otherwise by an
    event it hosts, else a valid-start region name. Components with none of
    these fall back to ``R{comp}`` at lookup time."""
    labels: dict[int, str] = {}
    for comp, ap_name in graph.get("pickups", []):
        labels.setdefault(comp, ap_name)
    for comp, event_name in graph.get("events", []):
        labels.setdefault(comp, f"{humanize_event(event_name)} (event)")
    for key, meta in graph.get("start_comps", {}).items():
        labels.setdefault(meta["comp"], key.split("::")[0])
    # Region-level fallback for every remaining component (graph schema 7+), so
    # a hop never degrades to the bare "R{comp}" machine name.
    for comp, region in enumerate(graph.get("comp_regions", [])):
        if region is not None:
            labels.setdefault(comp, region)
    return labels
