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
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ._data_loader import load_json


# A neutral placeholder item used when the AP placement is for ANOTHER
# slot. The byte pattern doesn't matter to the game (the patcher writes
# whatever resource we say), and the player sees the cross-slot caption
# instead of the resource icon. Picking a vanilla Missile Tank shape
# means no special handling is needed by open-dread-rando.
CROSS_SLOT_PLACEHOLDER = {"item_id": "ITEM_WEAPON_MISSILE_MAX", "quantity": 2}

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

    starting_location = STARTING_AREA_INDEX_TO_LOCATION.get(
        int(starting_area_idx),
        STARTING_AREA_INDEX_TO_LOCATION[0],
    )

    pickup_resources: dict[str, list] = {}
    pickup_captions: dict[str, str] = {}

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
            patcher_item_id = p.get("patcher_item_id") or ""
            quantity = int(p.get("quantity", 1))
            if not patcher_item_id:
                continue  # defensive — events were already filtered
            pickup_resources[key] = [[
                {"item_id": patcher_item_id, "quantity": quantity}
            ]]
            # Overwrite the template's stale caption so the in-game popup names
            # the AP-placed item, not the starter-preset's vanilla one (e.g. a
            # pedestal now holding a Missile Tank shouldn't still say "Flash
            # Shift acquired."). Matches the template's "<item> acquired." form.
            ap_item_name = p.get("ap_item_name", "")
            if ap_item_name:
                pickup_captions[key] = f"{ap_item_name} acquired."
        else:
            ap_item_name = p.get("ap_item_name", "Item")
            pickup_resources[key] = [[dict(CROSS_SLOT_PLACEHOLDER)]]
            pickup_captions[key] = f"Sent {ap_item_name} to {recipient}"

    if layout_uuid is None:
        layout_uuid = layout_uuid_from_seed(str(seed_id), slot_name)

    cfg_id = f"AP-{str(seed_id)[:8]}-{slot_name}"

    return {
        "layout_uuid": layout_uuid,
        "configuration_identifier": cfg_id,
        "starting_location": starting_location,
        "starting_items": starting_items,
        "cosmetic_combat": placements.get("cosmetic_combat", {}),
        "required_artifacts": placements.get("required_artifacts"),
        "pickup_resources": pickup_resources,
        "pickup_captions": pickup_captions,
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

    # Cosmetic / combat leaves. Only fields actually supplied are written;
    # an absent key leaves the template default untouched (so a pre-this-change
    # seed payload yields byte-identical output).
    cosmetic_combat = overrides.get("cosmetic_combat", {})
    for field_name, path in COSMETIC_COMBAT_PATHS.items():
        if field_name in cosmetic_combat:
            _set_in(out, path, cosmetic_combat[field_name])

    # Goal: how many Metroid DNA must be collected. Overwrite only the count,
    # preserving the template's objective.hints. None ⇒ leave template default.
    required_artifacts = overrides.get("required_artifacts")
    if required_artifacts is not None:
        out.setdefault("objective", {})["required_artifacts"] = int(required_artifacts)

    pickup_resources = overrides.get("pickup_resources", {})
    pickup_captions = overrides.get("pickup_captions", {})

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
) -> dict[str, Any]:
    """End-to-end placements → patcher_input.json conversion using the
    bundled starter template."""
    overrides = placements_to_overrides(placements, layout_uuid=layout_uuid)
    return merge_overrides(load_starter_template(), overrides)


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
    install when the user definitely ``pip install``ed open-dread-rando
    into a real Python."""
    py = python_executable or sys.executable
    base = Path(py).name.lower()
    if "archipelagolauncher" in base or base in {"archipelago.exe", "archipelago"}:
        return f"{py}  (frozen Archipelago launcher — won't have open-dread-rando)"
    if python_executable:
        return f"{py}  (override; set via /patch_python)"
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
    if python_executable and python_executable != sys.executable:
        try:
            proc = subprocess.run(
                [python_executable, "-c",
                 f"import open_dread_rando, mercury_engine_data_structures; "
                 f"print('{_PROBE_OK_TOKEN}')"],
                capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            return (
                f"configured Python not found: {python_executable}\n"
                "Set a valid interpreter with:  /patch_python <path-to-python.exe>"
            )
        except subprocess.TimeoutExpired:
            return f"dep probe timed out launching {python_executable}"
        if proc.returncode == 0 and _PROBE_OK_TOKEN in (proc.stdout or ""):
            return None
        # Surface the actual ImportError for actionable diagnostics.
        err = (proc.stderr or proc.stdout or "").strip()
        last = err.splitlines()[-1] if err else f"exit {proc.returncode}"
        return (
            f"open_dread_rando / mercury_engine_data_structures not importable "
            f"from {python_executable}\n"
            f"    {last}\n"
            f"Install with:  {python_executable} -m pip install open-dread-rando"
        )

    try:
        import open_dread_rando  # noqa: F401
    except ImportError:
        return (
            f"open_dread_rando is not installed in {describe_python()}.\n"
            "Install with:\n"
            "    pip install open-dread-rando\n"
            "Or, if running from the frozen Archipelago launcher, point at a real Python:\n"
            "    /patch_python <path-to-python.exe>"
        )
    try:
        import mercury_engine_data_structures  # noqa: F401
    except ImportError:
        return (
            "mercury_engine_data_structures is not installed (open_dread_rando dep).\n"
            "Try:  pip install --upgrade open-dread-rando"
        )
    return None


def patch(
    placements: dict[str, Any],
    dreadvania_install_dir: Path,
    vanilla_romfs_dir: Path,
    *,
    layout_uuid: Optional[str] = None,
    patcher_input_path: Optional[Path] = None,
    python_executable: Optional[str] = None,
) -> PatchResult:
    """End-to-end /patch implementation. Pure-ish (writes to disk, runs
    a subprocess) — returns a PatchResult that the caller surfaces as
    log output.

    Steps:
      1. dependency check
      2. build patcher_input.json from `placements`
      3. invoke `python -m open_dread_rando` against the vanilla romfs,
         writing into `dreadvania_install_dir` (overwriting in place)

    The Switch→PC collected-checks wiring lives in the client-sent
    Randovania bootstrap, so no post-patch init.lc edit is needed.
    """
    dep_err = check_dependencies(python_executable)
    if dep_err:
        return PatchResult(ok=False, message=dep_err)

    if not vanilla_romfs_dir.is_dir():
        return PatchResult(ok=False, message=f"vanilla romfs not found: {vanilla_romfs_dir}")
    if not dreadvania_install_dir.is_dir():
        return PatchResult(ok=False, message=(
            f"dreadvania install dir not found: {dreadvania_install_dir}\n"
            "Run the Randovania GUI installer at least once first — we overlay onto its output."
        ))

    # 1+2: build patcher input
    patcher_input = build_patcher_input_from_placements(placements, layout_uuid=layout_uuid)
    if patcher_input_path is None:
        patcher_input_path = dreadvania_install_dir.parent / "ap_patcher_input.json"
    patcher_input_path.parent.mkdir(parents=True, exist_ok=True)
    patcher_input_path.write_text(json.dumps(patcher_input, indent=2), encoding="utf-8")

    # 3: run the upstream patcher CLI. Use absolute paths — relative
    # --output-path triggers a recursive romfs/build/ artifact upstream.
    py = python_executable or sys.executable
    cmd = [
        py, "-m", "open_dread_rando",
        "--input-path", str(vanilla_romfs_dir.resolve()),
        "--output-path", str(dreadvania_install_dir.resolve()),
        "--input-json", str(patcher_input_path.resolve()),
        "--quiet",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
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
    )
