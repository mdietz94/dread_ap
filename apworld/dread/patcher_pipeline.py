"""Patcher pipeline glue — bundled inside the apworld so /patch works
from a deployed .apworld zip (no scripts/ needed).

Two pure functions that mirror the historic CLI scripts:

  * :func:`placements_to_overrides` — was ``scripts/seed_to_patcher_overrides.py``.
  * :func:`merge_overrides` — was ``scripts/build_patcher_json.py``.

The :func:`patch` orchestration runs both in sequence and invokes the
upstream ``open-dread-rando`` CLI to write the modded romfs. It's the
implementation behind the in-client ``/patch`` command.

The CLI scripts under ``scripts/`` are thin wrappers around this module
— single source of truth for the conversion logic.

The Switch→PC collected-checks path needs no init.lc patching: it is
handled entirely by the client-sent Randovania bootstrap
(``client/bootstrap.py`` → ``RL.GetCollectedIndicesAndSend``), which
reads the authoritative Blackboard ``Location_Collected_*`` props and
pushes the COLLECTED_INDICES bitfield. (An earlier design injected an
equivalent Lua block into ``init.lc``; that was removed as redundant
once the bootstrap shipped — see docs/wire-wiring-notes.md.)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ._data_loader import data_resource, load_json
from ._vendor import (
    PATCHER_RUNTIME_DEPS,
    vendor_unavailable_diagnostic,
    vendored_open_dread_rando_src,
)
from .client.protocol import pickup_resource_stage


def _patcher_subprocess_env(env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Copy of ``env`` (or ``os.environ``) with ``PYTHONPATH`` prepended to
    include the vendored ``open_dread_rando`` source. The submodule path is
    only prepended when it actually exists — otherwise the env is unchanged
    so callers can still fall back to a pip-installed copy."""
    out = dict(env) if env is not None else os.environ.copy()
    src = vendored_open_dread_rando_src()
    if src is not None:
        existing = out.get("PYTHONPATH", "")
        out["PYTHONPATH"] = (
            f"{src}{os.pathsep}{existing}" if existing else str(src)
        )
    return out


# A neutral placeholder item used when the AP placement is for ANOTHER
# slot. The player sees the cross-slot caption instead of the resource
# icon, and — crucially — collecting it must grant the local Dread player
# NOTHING (the real item is sent to the recipient over the wire).
#
# We use ``quantity: 0``: this is exactly Randovania's own coop/multiworld
# convention. open-dread-rando's ``lua_editor.get_parent_for`` special-cases
# quantity 0 ("coop uses the correct item_id ... but with quantity of 0"),
# routing it through the generic ``RandomizerPowerup`` whose additive grant
# of +0 adds nothing. The item_id is kept as a valid resource name only so
# the patcher has something well-formed to write; its value is otherwise
# inert at quantity 0.
#
# Previously this granted a real Missile Tank (``quantity: 2``), so picking
# up a foreign item visibly handed the local player +2 missile capacity —
# the reported bug.
CROSS_SLOT_PLACEHOLDER = {"item_id": "ITEM_WEAPON_MISSILE_MAX", "quantity": 0}

# Sprite to render for in-world pickups that hold ANOTHER slot's item.
# "itemsphere" is the patcher's own neutral-orb model (also its fallback in
# vendor/open-dread-rando/.../model_data.py), so no new asset has to ship.
# A future polish would register a real AP-branded model and switch this
# constant. The model list shape matches the template (single-model entries
# use a one-element list; multi-element lists are the progressive-item case).
CROSS_SLOT_MODEL: list[str] = ["itemsphere"]

# Base map-icon for an in-world pickup that holds ANOTHER slot's item. "unknown"
# is open-dread-rando's "?" minimap glyph (open_dread_rando/pickups/map_icons.py
# ALL_ICONS["unknown"] -> ItemUnknown). It's the icon Randovania itself uses for
# off-world items (see _map_icon_override), and it pairs with CROSS_SLOT_MODEL's
# neutral orb so the map legend and the in-world sphere agree.
CROSS_SLOT_MAP_BASE_ICON = "unknown"

# Starting-area option index → (scenario, actor). v0.1 only supports
# Artaria (option 0 == vanilla start). Future versions extend this.
STARTING_AREA_INDEX_TO_LOCATION: dict[int, dict[str, str]] = {
    0: {"scenario": "s010_cave", "actor": "StartPoint0"},
}

# Cosmetic / combat passthrough: payload field name → json-path of the leaf to
# overwrite in the patcher template. World.py resolves each value to its final
# patcher form, so this layer only relocates leaves. Adding a new passthrough
# setting is one line here + one line in World._build_placements_payload.
COSMETIC_COMBAT_PATHS: dict[str, tuple[str, ...]] = {
    "bShowBossLifebar": ("cosmetic_patches", "config", "AIManager", "bShowBossLifebar"),
    "bShowEnemyLife": ("cosmetic_patches", "config", "AIManager", "bShowEnemyLife"),
    "bShowEnemyDamage": ("cosmetic_patches", "config", "AIManager", "bShowEnemyDamage"),
    "bShowPlayerDamage": ("cosmetic_patches", "config", "AIManager", "bShowPlayerDamage"),
    "enable_death_counter": ("cosmetic_patches", "lua", "custom_init", "enable_death_counter"),
    "enable_room_name_display": ("cosmetic_patches", "lua", "custom_init", "enable_room_name_display"),
    "raven_beak_damage_table_handling": ("game_patches", "raven_beak_damage_table_handling"),
    "nerf_power_bombs": ("game_patches", "nerf_power_bombs"),
    "default_x_released": ("game_patches", "default_x_released"),
    # Top-level template field; controls Samus's base HP and per-tank grant.
    "energy_per_tank": ("energy_per_tank",),
}


def _set_in(root: dict, path: tuple[str, ...], value: Any) -> None:
    """Overwrite a leaf in an existing nested dict. Parent keys must already
    exist (the starter preset is complete); a missing parent raises so
    template/schema drift surfaces loudly rather than silently no-op'ing."""
    node = root
    for key in path[:-1]:
        if key not in node or not isinstance(node[key], dict):
            raise KeyError(
                f"cosmetic/combat path {'.'.join(path)} missing parent {key!r} "
                f"in template — template/schema drift?"
            )
        node = node[key]
    node[path[-1]] = value


# ---------------------------------------------------------------------
# Pure conversions
# ---------------------------------------------------------------------


def layout_uuid_from_seed(seed_id: str, slot_name: str) -> str:
    """Derive a UUID in the schema-required format from seed + slot.

    The schema regex is ``^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-...{12}$``.
    We hash (seed, slot), slice into 8-4-4-4-12 hex, and force a valid
    version/variant nibble. Stable across runs."""
    digest = hashlib.sha256(f"{seed_id}:{slot_name}".encode("utf-8")).hexdigest()
    h = list(digest[:32])
    h[12] = "4"   # version 4
    h[16] = "8"   # valid variant
    chars = "".join(h)
    return f"{chars[0:8]}-{chars[8:12]}-{chars[12:16]}-{chars[16:20]}-{chars[20:32]}"


def _map_icon_override(is_own: bool, model: str, ap_item_name: str) -> Optional[dict]:
    """Map-screen icon override for one placement.

    Mirrors Randovania's own Dread exporter (``patch_data_factory.
    _pickup_detail_for_target``) so the minimap legend matches what we already
    do for the in-world model + caption:

      * own item with a concrete model    → ``{"icon_id": <model>}``
      * own item rendered as the orb      → ``{"custom_icon": {"label": NAME}}``
        (e.g. Metroid DNA, whose ``model_name`` is ``itemsphere``)
      * cross-slot item (always the orb)  → ``{"custom_icon": {"label": NAME,
        "base_icon": "unknown"}}`` — the "?" glyph, matching Randovania's
        off-world treatment.

    The starter preset bakes Randovania's OWN placement icon at every map spot,
    so after AP shuffling each minimap icon lies: a relocated Missile Tank still
    shows the icon of whatever vanilla item used to sit there, and a foreign
    item advertises the Dread item it replaced. Rewriting the icon keeps the map
    honest. Returns ``None`` when there's nothing to override (own item whose
    model we don't know — older payloads / the offline CLI flow) so the
    template's icon is left untouched, exactly like the model path."""
    label = (ap_item_name or "").upper()
    if not is_own:
        return {"custom_icon": {"label": label, "base_icon": CROSS_SLOT_MAP_BASE_ICON}}
    if not model:
        return None
    if model == CROSS_SLOT_MODEL[0]:  # the neutral orb ("itemsphere")
        return {"custom_icon": {"label": label}}
    return {"icon_id": model}


def placements_to_overrides(
    placements: dict[str, Any],
    *,
    layout_uuid: Optional[str] = None,
) -> dict[str, Any]:
    """Convert a per-slot placements dict (from ``DreadWorld._build_placements_payload``)
    to the overrides shape that :func:`merge_overrides` consumes."""
    slot_name = placements["slot_name"]
    seed_id = placements.get("seed_id", "")
    starting_area_idx = placements.get("starting_area", 0)
    starting_items = placements.get("starting_items", {})

    # A graph-resolved spawn (more-starting-areas) overrides the index table.
    start_override = placements.get("start_location_override")
    if start_override:
        starting_location = {"scenario": start_override["scenario"],
                             "actor": start_override["actor"]}
    else:
        starting_location = STARTING_AREA_INDEX_TO_LOCATION.get(
            int(starting_area_idx),
            STARTING_AREA_INDEX_TO_LOCATION[0],
        )

    pickup_resources: dict[str, list] = {}
    pickup_captions: dict[str, str] = {}
    pickup_models: dict[str, list[str]] = {}
    pickup_map_icons: dict[str, dict] = {}

    for p in placements.get("placements", []):
        scenario = p.get("scenario")
        actor = p.get("actor")
        if not scenario or not actor:
            continue
        # Events are AP-synthetic — no patcher counterpart.
        if p.get("pickup_type") == "event":
            continue
        # Non-actor pickups (EMMI / corex / corpius / cutscene) ARE overridden
        # now: their location's (scenario, actor) equals the template's
        # pickup_lua_callback (scenario, function), so the key below matches a
        # template pickup via _pickup_key. This is what lets Metroid DNA (and
        # any AP item) land on a boss/EMMI location and grant the right
        # resource, instead of the boss keeping its vanilla drop.

        key = f"{scenario}/{actor}"
        recipient = p.get("recipient_slot_name") or slot_name
        is_own = bool(p.get("is_own_player", recipient == slot_name))

        if is_own:
            # Progressive item: the placement carries the FULL multi-stage
            # resources + per-tier model list + progressive map-icon id. Upstream
            # open_dread_rando builds the RandomizerProgressive class + animated
            # models from these arrays (resources is already list-of-stages,
            # model is already a list), so they flow through merge_overrides
            # verbatim. See DreadWorld._build_placements_payload.
            prog_stages = p.get("progression_stages")
            if prog_stages:
                pickup_resources[key] = prog_stages
                ap_item_name = p.get("ap_item_name", "")
                if ap_item_name:
                    pickup_captions[key] = f"{ap_item_name} acquired."
                models = p.get("models") or []
                if models:
                    pickup_models[key] = models
                map_icon_id = p.get("map_icon_id")
                if map_icon_id:
                    pickup_map_icons[key] = {"icon_id": map_icon_id}
                continue
            patcher_item_id = p.get("patcher_item_id") or ""
            quantity = int(p.get("quantity", 1))
            if not patcher_item_id:
                continue  # defensive — events were already filtered
            # Expand to the full resource stage — single resource for most
            # items, but the Main Power Bomb grants the unlock flag + capacity
            # pair (see pickup_resource_stage), without which the player gets a
            # 0/0-ammo power bomb that shows as "?" in the menu.
            pickup_resources[key] = [pickup_resource_stage(patcher_item_id, quantity)]
            # Overwrite the template's stale caption so the in-game popup names
            # the AP-placed item, not the starter-preset's vanilla one (e.g. a
            # pedestal now holding a Missile Tank shouldn't still say "Flash
            # Shift acquired."). Matches the template's "<item> acquired." form.
            ap_item_name = p.get("ap_item_name", "")
            if ap_item_name:
                pickup_captions[key] = f"{ap_item_name} acquired."
            # Re-skin the in-world sphere to THIS item's model for the same
            # reason as the caption: the starter preset baked Randovania's own
            # placement model (often a progressive multi-model) at each location,
            # so after AP shuffling the vanilla model rarely matches what the
            # pickup actually grants. Only own-slot items have a concrete Dread
            # model; cross-slot items are handled by CROSS_SLOT_MODEL below.
            patcher_model = p.get("patcher_model", "")
            if patcher_model:
                pickup_models[key] = [patcher_model]
            # Re-skin the map-screen icon the same way as the model: the baked
            # icon names Randovania's placement, stale after AP shuffling.
            map_icon = _map_icon_override(True, patcher_model, ap_item_name)
            if map_icon is not None:
                pickup_map_icons[key] = map_icon
        else:
            ap_item_name = p.get("ap_item_name", "Item")
            pickup_resources[key] = [[dict(CROSS_SLOT_PLACEHOLDER)]]
            pickup_captions[key] = f"Sent {ap_item_name} to {recipient}"
            pickup_models[key] = list(CROSS_SLOT_MODEL)
            pickup_map_icons[key] = _map_icon_override(False, "", ap_item_name)

    if layout_uuid is None:
        layout_uuid = layout_uuid_from_seed(str(seed_id), slot_name)

    cfg_id = f"AP-{str(seed_id)[:8]}"

    return {
        "layout_uuid": layout_uuid,
        "configuration_identifier": cfg_id,
        "starting_location": starting_location,
        "starting_items": starting_items,
        "cosmetic_combat": placements.get("cosmetic_combat", {}),
        "required_artifacts": placements.get("required_artifacts"),
        "nav_hints": placements.get("nav_hints", []),
        "pickup_resources": pickup_resources,
        "pickup_captions": pickup_captions,
        "pickup_models": pickup_models,
        "pickup_map_icons": pickup_map_icons,
        # Door-lock rando: passed straight to open-dread-rando's configuration.
        "door_patches": placements.get("door_patches", []),
    }


def _pickup_key(pickup: dict[str, Any]) -> Optional[str]:
    """Return a stable ``"<scenario>/<name>"`` key for a template pickup.

    Actor pickups key off ``pickup_actor`` (scenario/actor). Non-actor
    pickups (EMMI / corex / corpius / cutscene) have ``pickup_actor: null``
    but carry a ``pickup_lua_callback`` whose ``(scenario, function)`` pair
    matches the ``(scenario, actor)`` our locations.json stores for those
    boss/EMMI locations — so both shapes share one key space (verified
    unique across the template)."""
    actor = pickup.get("pickup_actor")
    if actor:
        return f"{actor.get('scenario')}/{actor.get('actor')}"
    cb = pickup.get("pickup_lua_callback")
    if cb:
        return f"{cb.get('scenario')}/{cb.get('function')}"
    return None


def _merge_map_icon(existing: Any, override: dict) -> dict:
    """Rewrite the icon branch of a template ``map_icon`` in place-safe fashion.

    The schema models ``map_icon`` as a ``oneOf`` of {empty, ``icon_id``,
    ``custom_icon``} alongside an optional ``original_actor`` — so exactly one
    icon branch may be present. We carry over the template's ``original_actor``
    (it points the icon at the correct map prop) and drop whichever icon branch
    the template had, replacing it with ours (``override`` holds exactly one of
    ``icon_id`` / ``custom_icon``)."""
    merged: dict[str, Any] = {}
    if isinstance(existing, dict) and "original_actor" in existing:
        merged["original_actor"] = existing["original_actor"]
    merged.update(override)
    return merged


NAV_HINT_AP_TEXT = "You're playing Archipelago! There's already a hint system!"


def _apply_nav_hints(
    hints: list[dict[str, Any]],
    generated: list[Any],
) -> list[dict[str, Any]]:
    """Fill each Nav Station hint plaque's text from the AP-generated hint
    list, keeping ``accesspoint_actor`` / ``hint_id`` intact so the patcher's
    ``patch_hints`` still resolves the actor and applies the door-unlock
    side-effects (``vDoorsToChange=[]``, ``wpThermalDevice=""``).

    The starter preset bakes ~11 entries pointing at Randovania's own
    placement (e.g. "A Progressive Beam can be found in Cataris"); those are
    false under AP shuffling. ``DreadWorld`` instead generates real cross-world
    placement hints at generation time (see ``DreadWorld._generate_nav_hints``,
    which also registers each as a real AP server hint). Slots beyond the
    generated count — or every slot when ``generated`` is empty, e.g. the
    offline / template-passthrough flows — fall back to the AP-aware filler so
    nothing keeps leaking the stale Randovania placement."""
    out = []
    for i, hint in enumerate(hints):
        if i < len(generated):
            entry = generated[i]
            text = entry["text"] if isinstance(entry, dict) else entry
        else:
            text = NAV_HINT_AP_TEXT
        out.append({**hint, "text": [text]})
    return out


def _objective_hints_for(required_artifacts: int) -> list[str]:
    """Neutral, non-spoiler text for the in-game objective screen, replacing
    the starter preset's stale per-guardian hints.

    The template hard-codes hints like ``"Metroid DNA 1 is guarded by
    Corpius"``, baked for Randovania's own DNA placement. Under AP those are
    simply WRONG whenever DNA is shuffled anywhere, lands on a different random
    boss subset, or isn't required at all (``required_artifacts: 0``). Faithful
    regeneration would need a guardian-name map plus Randovania's per-location
    hint logic — out of scope for v0.1 — so we state only the count and leak
    nothing. ``{c1}``/``{c0}`` are the game's colour escapes (kept so the line
    renders like the originals). Always returns exactly one string, matching the
    template's shape."""
    n = int(required_artifacts)
    if n <= 0:
        return ["Return to your ship to escape ZDR."]
    return [f"Recover {{c1}}{n} Metroid DNA{{c0}} to complete your mission."]


def merge_overrides(template: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Apply the AP override dict on top of a vanilla template.

    Returns a new dict (deep-copied); inputs are not mutated."""
    out = json.loads(json.dumps(template))

    # Hard requirement for AP: init.lc gates RL.Init() on this flag. Without
    # it the exlaunch socket never binds — see the comment in the script-
    # mode wrapper for the full back-story.
    out["enable_remote_lua"] = True

    for key in ("layout_uuid", "configuration_identifier", "starting_location"):
        if key in overrides:
            out[key] = overrides[key]

    if "starting_items" in overrides:
        out["starting_items"] = overrides["starting_items"]

    # Door-lock rando: replace the template's door_patches with the AP assignment
    # (one entry per physical door). Absent/empty ⇒ keep the template's vanilla
    # door_patches so non-door-rando seeds stay byte-identical.
    door_patches = overrides.get("door_patches")
    if door_patches:
        out["door_patches"] = door_patches

    # Cosmetic / combat leaves. Only fields actually supplied are written;
    # an absent key leaves the template default untouched (so a pre-this-change
    # seed payload yields byte-identical output).
    cosmetic_combat = overrides.get("cosmetic_combat", {})
    for field_name, path in COSMETIC_COMBAT_PATHS.items():
        if field_name in cosmetic_combat:
            _set_in(out, path, cosmetic_combat[field_name])

    # Goal: how many Metroid DNA must be collected. Overwrite the count AND
    # replace objective.hints — the template's per-guardian hints are baked for
    # Randovania's placement and are false under AP shuffling (see
    # _objective_hints_for). None ⇒ leave the template untouched so offline /
    # template-passthrough flows stay byte-identical.
    required_artifacts = overrides.get("required_artifacts")
    if required_artifacts is not None:
        obj = out.setdefault("objective", {})
        obj["required_artifacts"] = int(required_artifacts)
        obj["hints"] = _objective_hints_for(int(required_artifacts))

    # Nav Station hints: the starter preset bakes ~11 entries pointing at
    # Randovania's own placement, false under AP shuffling. Fill them with the
    # AP-generated cross-world hints (falling back to neutral filler for any
    # plaque past the generated count) while preserving entries so patch_hints
    # still unlocks the doors.
    if out.get("hints"):
        out["hints"] = _apply_nav_hints(out["hints"], overrides.get("nav_hints") or [])

    pickup_resources = overrides.get("pickup_resources", {})
    pickup_captions = overrides.get("pickup_captions", {})
    pickup_models = overrides.get("pickup_models", {})
    pickup_map_icons = overrides.get("pickup_map_icons", {})

    unmatched = set(pickup_resources.keys())
    for pickup in out.get("pickups", []):
        key = _pickup_key(pickup)
        if key is None:
            continue
        if key in pickup_resources:
            pickup["resources"] = pickup_resources[key]
            unmatched.discard(key)
        if key in pickup_captions:
            pickup["caption"] = pickup_captions[key]
        # Only rewrite an existing model field — non-actor pickups (boss /
        # EMMI / cutscene drops) carry no in-world sphere to re-skin, so
        # leaving them untouched preserves the template's vanilla shape.
        if key in pickup_models and "model" in pickup:
            pickup["model"] = pickup_models[key]
        # Same rule for the map-screen icon: only rewrite an existing map_icon
        # (every actor pickup has one; non-actor drops don't appear on the
        # item map). Preserve the template's original_actor so the icon still
        # anchors to the right map spot — see _merge_map_icon.
        if key in pickup_map_icons and "map_icon" in pickup:
            pickup["map_icon"] = _merge_map_icon(pickup["map_icon"], pickup_map_icons[key])

    if unmatched:
        raise ValueError(
            "pickup keys in overrides that don't exist in the template:\n  "
            + "\n  ".join(sorted(unmatched))
        )

    return out


def load_starter_template() -> dict[str, Any]:
    """Load the Randovania starter-preset patcher template bundled with
    the apworld. (open-dread-rando's pip wheel doesn't ship its test
    fixtures, so we carry our own copy.)"""
    return load_json("starter_preset_patcher.json")


def build_patcher_input_from_placements(
    placements: dict[str, Any],
    *,
    layout_uuid: Optional[str] = None,
    mod_compatibility: Optional[str] = None,
) -> dict[str, Any]:
    """End-to-end placements → patcher_input.json conversion using the
    bundled starter template.

    The bundled template hard-codes ``mod_compatibility: "ryujinx"`` (its
    origin was a Ryujinx dev export). That value decides the patcher's
    on-disk layout — Ryujinx nests the mod under a ``DreadRandovania``
    folder, Atmosphere writes flat into ``contents/<tid>/``. A real Switch
    (Atmosphere) does NOT read the nested folder, so an SD/custom deploy
    must override this to ``"atmosphere"`` or the seed lands one level too
    deep and the console ignores it. Pass ``mod_compatibility`` to override
    the template; leave it ``None`` to keep the template's value.
    """
    overrides = placements_to_overrides(placements, layout_uuid=layout_uuid)
    result = merge_overrides(load_starter_template(), overrides)
    if mod_compatibility is not None:
        result["mod_compatibility"] = mod_compatibility
    return result


# ---------------------------------------------------------------------
# Orchestration (the /patch command's implementation)
# ---------------------------------------------------------------------


@dataclass
class PatchResult:
    ok: bool
    message: str
    patcher_input_path: Optional[Path] = None
    cli_returncode: Optional[int] = None
    cli_stderr_tail: str = ""
    notes: list[str] = field(default_factory=list)


# Sentinel printed by the external-Python dep probe to distinguish
# "deps imported cleanly" from "Python ran but had nothing to say"
# (e.g. site-customize banners on stdout).
_PROBE_OK_TOKEN = "DREAD_AP_DEPS_OK"


def describe_python(python_executable: Optional[str] = None) -> str:
    """Human-readable description of the Python that would be used for
    the patcher subprocess. Flags the frozen Archipelago launcher because
    that case is the #1 reason ``check_dependencies()`` reports a missing
    install — the launcher's bundled Python doesn't see a user's
    pip-installed patcher deps."""
    py = python_executable or sys.executable
    base = Path(py).name.lower()
    if "archipelagolauncher" in base or base in {"archipelago.exe", "archipelago"}:
        return f"{py}  (frozen Archipelago launcher — won't have patcher deps)"
    if python_executable:
        return f"{py}  (auto-detected by the setup wizard)"
    return f"{py}  (sys.executable)"


def check_dependencies(python_executable: Optional[str] = None) -> Optional[str]:
    """Return None if the patcher's Python deps are importable from the
    target interpreter; else a user-readable message naming what's
    missing and how to fix it.

    When ``python_executable`` is provided (and isn't the current
    process), probe by subprocess so the answer reflects what the
    patcher CLI will actually see. The in-process import path is wrong
    inside the frozen Archipelago launcher — that Python ships its own
    bundled site-packages and never sees a user's ``pip install``."""
    deps_pip = " ".join(PATCHER_RUNTIME_DEPS)
    if python_executable and python_executable != sys.executable:
        try:
            proc = subprocess.run(
                [python_executable, "-c",
                 f"import open_dread_rando, mercury_engine_data_structures; "
                 f"print('{_PROBE_OK_TOKEN}')"],
                capture_output=True, text=True, timeout=30,
                env=_patcher_subprocess_env(),
            )
        except FileNotFoundError:
            return (
                f"configured Python not found: {python_executable}\n"
                "Re-run /setup to re-detect a usable interpreter."
            )
        except subprocess.TimeoutExpired:
            return f"dep probe timed out launching {python_executable}"
        if proc.returncode == 0 and _PROBE_OK_TOKEN in (proc.stdout or ""):
            return None
        # Surface the actual ImportError for actionable diagnostics.
        err = (proc.stderr or proc.stdout or "").strip()
        last = err.splitlines()[-1] if err else f"exit {proc.returncode}"
        hint = (
            vendor_unavailable_diagnostic()
            if vendored_open_dread_rando_src() is None
            else f"install patcher deps with:  {python_executable} -m pip install {deps_pip}"
        )
        return (
            f"open_dread_rando / mercury_engine_data_structures not importable "
            f"from {python_executable}\n"
            f"    {last}\n"
            f"{hint}"
        )

    # In-process probe path. Inject the vendored source onto sys.path so the
    # import matches what the patcher subprocess would see at runtime.
    vendored_src = vendored_open_dread_rando_src()
    if vendored_src is not None and str(vendored_src) not in sys.path:
        sys.path.insert(0, str(vendored_src))
    try:
        import open_dread_rando  # noqa: F401
    except ImportError:
        if vendored_src is None:
            return (
                f"open_dread_rando isn't available in {describe_python()}: "
                f"{vendor_unavailable_diagnostic()}."
            )
        return (
            f"open_dread_rando is vendored at {vendored_src} but import still "
            f"failed in {describe_python()} — likely a missing runtime dep.\n"
            f"Install with:  pip install {deps_pip}"
        )
    try:
        import mercury_engine_data_structures  # noqa: F401
    except ImportError:
        return (
            "mercury_engine_data_structures is not installed (open_dread_rando dep).\n"
            f"Install with:  pip install {deps_pip}"
        )
    return None


def _candidate_pythons() -> list[str]:
    """Best-effort, ordered list of real Python interpreters to probe for the
    patcher deps. Deduped by literal absolute path (NOT symlink-resolved — a
    venv's ``bin/python`` is typically a symlink to the base interpreter, but
    invoking the resolved base path skips venv activation and misses the
    venv's ``site-packages``). The frozen Archipelago launcher is excluded —
    its bundled site-packages never sees a user's ``pip install``, so it
    can never be the answer."""
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(path: Optional[str]) -> None:
        if not path:
            return
        try:
            abspath = str(Path(path).absolute())
        except OSError:
            return
        if not Path(abspath).is_file():
            return
        # describe_python tags the frozen launcher; never offer it.
        if "frozen Archipelago launcher" in describe_python(abspath):
            return
        key = abspath.lower() if os.name == "nt" else abspath
        if key in seen:
            return
        seen.add(key)
        candidates.append(abspath)

    # The current interpreter first (correct in dev / a real venv).
    # SKIPPED when this process is a frozen bundle (PyInstaller / py2exe
    # set `sys.frozen`) — in that case `sys.executable` points at the
    # frozen wrapper, which has its own bundled site-packages and can
    # never satisfy this prereq regardless of what the user has pip-
    # installed. The `describe_python` name check below is a backstop;
    # `sys.frozen` is the canonical signal.
    if not getattr(sys, "frozen", False):
        _add(sys.executable)
    # Explicit venv fallback: if our host process isn't itself the venv's
    # python (frozen launcher, wrapper script), the activated venv still
    # shows up here.
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        venv_root = Path(venv)
        for rel in ("bin/python", "bin/python3", "Scripts/python.exe"):
            _add(str(venv_root / rel))
    # PATH lookups.
    for name in ("py", "python", "python3"):
        _add(shutil.which(name))
    # The Windows `py -3` launcher resolves to the default Python 3.
    if sys.platform == "win32":
        py = shutil.which("py")
        if py:
            try:
                proc = subprocess.run(
                    [py, "-3", "-c", "import sys; print(sys.executable)"],
                    capture_output=True, text=True, timeout=10,
                )
                if proc.returncode == 0:
                    _add(proc.stdout.strip())
            except (OSError, subprocess.SubprocessError):
                pass
        # Common per-user CPython install location.
        local = os.environ.get("LOCALAPPDATA")
        if local:
            base = Path(local) / "Programs" / "Python"
            if base.is_dir():
                for child in sorted(base.glob("Python*")):
                    _add(str(child / "python.exe"))
    return candidates


def autodetect_patcher_python() -> tuple[Optional[str], str]:
    """Find a Python that can run the vendored ``open-dread-rando`` patcher.

    Returns ``(path, message)``: ``path`` is the first detected interpreter
    whose deps import cleanly (``None`` if none qualifies), and ``message`` is
    user-facing — an OK line naming the interpreter, the exact ``pip install``
    command when a real Python exists but lacks the runtime deps, or an
    install-Python hint when no interpreter was found at all. Reuses
    :func:`check_dependencies` so the probe matches exactly what ``/patch``
    will see."""
    deps_pip = " ".join(PATCHER_RUNTIME_DEPS)
    candidates = _candidate_pythons()
    if not candidates:
        return None, (
            "No Python interpreter found on PATH. Install Python 3 from "
            f"python.org, then run:  python -m pip install {deps_pip}"
        )
    for cand in candidates:
        if check_dependencies(cand) is None:
            return cand, f"patcher Python auto-detected: {cand}"
    best = candidates[0]
    if vendored_open_dread_rando_src() is None:
        return None, f"Vendored open-dread-rando not available: {vendor_unavailable_diagnostic()}."
    return None, (
        "open-dread-rando runtime deps aren't installed in any detected "
        "Python. Run this, then re-run /setup so the wizard re-detects the "
        "interpreter:\n"
        f"    {best} -m pip install {deps_pip}"
    )


def _install_exefs_ips(exefs_dir: Path) -> list[str]:
    """Copy open-dread-rando's build-id-keyed exefs IPS patches (bundled under
    our ``data/exefs_patches/``) into the mod's ``exefs`` dir, returning the
    filenames copied.

    These IPS patches add ``Game.HasRandomizerPatches`` (the "version
    sentinel") to the ``main`` NSO. open-dread-rando's ``custom_scenario.lua``
    rejects the save at ``InitScenario`` (new-save start) with "Unsupported
    Metroid Dread version" if that function is missing — even on a correct
    2.1.0 ROM. The patches are named after the target NSO build id, so Ryujinx
    /Atmosphere apply only the one matching the running game.

    Why we re-assert them here: upstream ships these prebuilt ``.ips`` inside
    its pip wheel (gitignored in the repo, generated by
    ``tools/create_exefs_patches.py``). Our patcher runs the *vendored*
    open-dread-rando submodule, whose checkout has none, and open-dread-rando
    ``rmtree``s + refills the exefs dir on every run from its (empty) package
    copy — so without this the deployed mod has no version sentinel. Our
    bundled copies are byte-identical to what the vendored tool generates.
    When the patcher falls back to a pip install (no vendored src), it already
    wrote these same bytes; copying ours over is a harmless no-op-equivalent.
    """
    copied: list[str] = []
    try:
        entries = list(data_resource("exefs_patches").iterdir())
    except (FileNotFoundError, NotADirectoryError, OSError):
        return copied
    for entry in entries:
        if not entry.name.endswith(".ips"):
            continue
        (exefs_dir / entry.name).write_bytes(entry.read_bytes())
        copied.append(entry.name)
    return copied


# Dread's Atmosphere title id (lowercase, as the patcher emits it). Kept in
# sync with _setup.deploy.DREAD_TITLE_ID; duplicated here to avoid importing
# the GUI/deploy module into the patcher pipeline.
_DREAD_TITLE_ID = "010093801237c000"


def _resolve_output_layout(
    install_dir: Path, compatibility: str
) -> tuple[Path, Path, Path]:
    """Map the desired FINAL mod dir + compatibility mode to the three paths
    we need: (cli_output_path, exefs_dir, exefs_patches_dir).

    This inverts ``open_dread_rando.output_config.OutputCompatibility.paths()``
    so the patcher's own append lands the mod exactly at ``install_dir`` (no
    double-nesting) and so our re-asserted version-sentinel IPS land where the
    target platform actually reads exefs patches:

      RYUJINX:    mod = out/DreadRandovania
                  exefs == exefs_patches == mod/exefs   (Ryujinx reads IPS
                  from the mod's own exefs folder)
      ATMOSPHERE: mod = out/contents/<tid>
                  exefs       = mod/exefs               (LayeredFS replacement)
                  exefs_patches = out/exefs_patches/DreadRandovania
                  (Atmosphere reads IPS from the GLOBAL exefs_patches tree,
                  a sibling of contents/ — NOT from inside the title folder)
    """
    if compatibility == "ryujinx":
        if install_dir.name == "DreadRandovania":
            out_path = install_dir.parent
            mod_dir = install_dir
        else:
            out_path = install_dir
            mod_dir = install_dir / "DreadRandovania"
        exefs = mod_dir / "exefs"
        return out_path, exefs, exefs  # Ryujinx: IPS live alongside exefs.
    if compatibility == "atmosphere":
        # The SD/custom install dir is the LayeredFS mod dir:
        #   <root>/atmosphere/contents/<tid>
        # so out_path is the atmosphere/ dir (two segments up). If the caller
        # passed something shallower, let the patcher re-create contents/<tid>.
        if (install_dir.parent.name == "contents"
                and install_dir.name.lower() == _DREAD_TITLE_ID):
            out_path = install_dir.parent.parent
            mod_dir = install_dir
        else:
            out_path = install_dir
            mod_dir = install_dir / "contents" / _DREAD_TITLE_ID
        exefs = mod_dir / "exefs"
        exefs_patches = out_path / "exefs_patches" / "DreadRandovania"
        return out_path, exefs, exefs_patches
    raise ValueError(f"unknown mod_compatibility: {compatibility!r}")


def patch(
    placements: dict[str, Any],
    dreadvania_install_dir: Path,
    vanilla_romfs_dir: Path,
    *,
    layout_uuid: Optional[str] = None,
    patcher_input_path: Optional[Path] = None,
    python_executable: Optional[str] = None,
    exefs_overlay: Optional[dict[str, Path]] = None,
    mod_compatibility: Optional[str] = None,
) -> PatchResult:
    """End-to-end /patch implementation. Pure-ish (writes to disk, runs
    a subprocess) — returns a PatchResult that the caller surfaces as
    log output.

    Steps:
      1. dependency check
      2. build patcher_input.json from `placements`
      3. invoke `python -m open_dread_rando` against the vanilla romfs,
         writing the mod into `dreadvania_install_dir` (overwriting in place)
      4. re-assert ``exefs_overlay`` (our patched sysmodule files) over the
         exefs the patcher just wrote — see below.

    ``dreadvania_install_dir`` is the FINAL mod folder (the dir whose
    ``romfs``/``exefs`` the game loads). ``mod_compatibility`` selects the
    patcher's on-disk layout (``open_dread_rando/output_config.py``):

      - ``"ryujinx"`` appends a ``DreadRandovania`` segment to
        ``--output-path``; the install dir already ends in that segment, so
        we hand the patcher the PARENT and let it re-create the leaf. (Passing
        the dir directly produced ``.../DreadRandovania/DreadRandovania`` and,
        worse, the nested ``exefs`` got the patcher's bundled UPSTREAM
        (server-mode, port 6969) ``subsdk9`` — "Multiple replacements to
        subsdk9".)
      - ``"atmosphere"`` appends ``contents/<tid>`` instead, and a real Switch
        only reads ``exefs``/``romfs`` directly under that title folder — NOT
        a ``DreadRandovania`` subfolder. So an SD/custom deploy MUST run in
        this mode; running it in ``"ryujinx"`` mode strands the seed in
        ``contents/<tid>/DreadRandovania/`` where the console ignores it (the
        bug this parameter fixes). The caller derives the mode from the deploy
        target (Ryujinx → ``"ryujinx"``; SD/custom → ``"atmosphere"``).

    :func:`_resolve_output_layout` inverts the upstream path math for the
    chosen mode so the mod lands AT ``dreadvania_install_dir`` either way.

    ``exefs_overlay`` (name→source-path, e.g. our built ``subsdk9`` +
    ``main.npdm``) is copied into the final mod's ``exefs`` AFTER the patcher
    runs. The patcher always writes its own upstream ``subsdk9`` there, so
    without this re-assert our patched sysmodule would be clobbered on every
    patch and the Switch would fall back to listening on 6969.

    We also always re-assert open-dread-rando's exefs version-sentinel IPS
    patches (``Game.HasRandomizerPatches``) via :func:`_install_exefs_ips` —
    the vendored open-dread-rando submodule omits them and ``rmtree``s the
    exefs dir each run, so without this the game rejects every save as an
    "Unsupported Metroid Dread version". These land in the per-mode
    exefs-patches dir (alongside ``exefs`` for Ryujinx; the GLOBAL
    ``exefs_patches/DreadRandovania`` tree for Atmosphere, since a real Switch
    reads IPS from there, not from inside the title folder). See that helper
    for the full why.

    The Switch→PC collected-checks wiring lives in the client-sent
    Randovania bootstrap, so no post-patch init.lc edit is needed.
    """
    dep_err = check_dependencies(python_executable)
    if dep_err:
        return PatchResult(ok=False, message=dep_err)

    if not vanilla_romfs_dir.is_dir():
        return PatchResult(ok=False, message=f"vanilla romfs not found: {vanilla_romfs_dir}")
    # First-ever deploy: the per-title install dir (SD .../contents/<tid> or
    # Ryujinx .../DreadRandovania) may not exist yet — _maybe_auto_patch's
    # SD-mount guard deliberately admits a freshly-mounted card that has only
    # the `atmosphere` dir, on the documented premise that "the patcher itself
    # creates the per-title dir". The install dir is the patcher's OUTPUT target
    # (we overlay patched romfs onto vanilla_romfs_dir, never onto this dir's
    # prior contents), so create it here instead of failing — a first deploy
    # then behaves identically to every subsequent one.
    dreadvania_install_dir.mkdir(parents=True, exist_ok=True)

    # 1+2: build patcher input. mod_compatibility (when given) overrides the
    # template default so an Atmosphere/SD deploy writes flat into
    # contents/<tid>/ instead of nesting under DreadRandovania (which the
    # console ignores).
    patcher_input = build_patcher_input_from_placements(
        placements, layout_uuid=layout_uuid, mod_compatibility=mod_compatibility)
    if patcher_input_path is None:
        patcher_input_path = dreadvania_install_dir.parent / "ap_patcher_input.json"
    patcher_input_path.parent.mkdir(parents=True, exist_ok=True)
    patcher_input_path.write_text(json.dumps(patcher_input, indent=2), encoding="utf-8")

    # Account for the patcher's compatibility-mode path suffix so the mod
    # lands AT dreadvania_install_dir, not nested under a doubled segment.
    # RYUJINX appends "DreadRandovania"; ATMOSPHERE appends "contents/<tid>".
    # Our callers pass the FINAL mod dir, so we hand the patcher the matching
    # parent and let it re-create the leaf. exefs_patches_dir is where the
    # version-sentinel IPS belong on THIS platform (see _resolve_output_layout
    # and the docstring for the 6969-shadowing bug this all guards).
    compatibility = patcher_input.get("mod_compatibility")
    output_path, exefs_dir, exefs_patches_dir = _resolve_output_layout(
        dreadvania_install_dir, compatibility)

    # 3: run the upstream patcher CLI. Use absolute paths — relative
    # --output-path triggers a recursive romfs/build/ artifact upstream.
    py = python_executable or sys.executable
    cmd = [
        py, "-m", "open_dread_rando",
        "--input-path", str(vanilla_romfs_dir.resolve()),
        "--output-path", str(output_path.resolve()),
        "--input-json", str(patcher_input_path.resolve()),
        "--quiet",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
            env=_patcher_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        return PatchResult(ok=False, message="patcher CLI timed out after 600s",
                           patcher_input_path=patcher_input_path)
    except FileNotFoundError as exc:
        return PatchResult(ok=False, message=f"could not launch patcher: {exc}")

    if proc.returncode != 0:
        return PatchResult(
            ok=False,
            message=f"patcher CLI failed with exit {proc.returncode}",
            patcher_input_path=patcher_input_path,
            cli_returncode=proc.returncode,
            cli_stderr_tail="\n".join((proc.stderr or "").splitlines()[-20:]),
        )

    # 4: re-assert files the patcher just (re)wrote:
    #   (a) the version-sentinel IPS patches — the vendored patcher omits them
    #       and the game rejects the save without them (see _install_exefs_ips).
    #       These go to exefs_patches_dir, which differs by platform: alongside
    #       the mod's exefs for Ryujinx, but in the GLOBAL exefs_patches tree
    #       for Atmosphere (a real Switch reads IPS from there, not from inside
    #       the title folder).
    #   (b) our patched sysmodule over the upstream subsdk9 — without it the
    #       Switch falls back to server mode (listens on 6969, never dials).
    #       This always goes into the LayeredFS exefs dir.
    notes: list[str] = []
    try:
        exefs_patches_dir.mkdir(parents=True, exist_ok=True)
        ips_copied = _install_exefs_ips(exefs_patches_dir)
        if ips_copied:
            notes.append(
                "installed exefs version-sentinel patches ("
                + ", ".join(sorted(ips_copied)) + f") into {exefs_patches_dir}"
            )
        else:
            # No bundled .ips and the vendored patcher writes none either — the
            # save will be rejected as an unsupported version. Surface it loudly
            # rather than shipping a silently-broken mod. (A pip-installed
            # patcher would have written its own, so this is the vendored-only
            # failure mode.)
            notes.append(
                "WARNING: no bundled exefs version-sentinel .ips found; the "
                "game may reject the save as an unsupported version"
            )
        if exefs_overlay:
            exefs_dir.mkdir(parents=True, exist_ok=True)
            for name, src in exefs_overlay.items():
                shutil.copy2(src, exefs_dir / name)
            notes.append(
                "re-asserted patched sysmodule ("
                + ", ".join(sorted(exefs_overlay)) + f") into {exefs_dir}"
            )
    except OSError as exc:
        # Don't fail the whole patch silently — the romfs is already written;
        # surface the problem so the user knows the mod may be unbootable.
        return PatchResult(
            ok=False,
            message=(
                f"patcher succeeded but re-asserting exefs files into "
                f"{exefs_dir} failed: {exc}. The mod may reject the save as an "
                f"unsupported version, or the Switch may load the upstream "
                f"server-mode subsdk9 (port 6969) and never dial the client. "
                f"Re-run /setup's Deploy step."
            ),
            patcher_input_path=patcher_input_path,
            cli_returncode=0,
        )

    n_actors = len(placements.get("placements", []))
    n_cross = sum(1 for p in placements.get("placements", []) if not p.get("is_own_player", True))
    return PatchResult(
        ok=True,
        message=(
            f"patched OK. {n_actors} placements applied "
            f"({n_cross} cross-slot). Re-launch Dread to load the new mod."
        ),
        patcher_input_path=patcher_input_path,
        cli_returncode=0,
        notes=notes,
    )
