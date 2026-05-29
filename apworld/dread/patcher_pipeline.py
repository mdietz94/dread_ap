"""Patcher pipeline glue — bundled inside the apworld so /patch works
from a deployed .apworld zip (no scripts/ needed).

Three pure functions that mirror the historic CLI scripts:

  * :func:`placements_to_overrides` — was ``scripts/seed_to_patcher_overrides.py``.
  * :func:`merge_overrides` — was ``scripts/build_patcher_json.py``.
  * :func:`build_telemetry_block` — was ``scripts/inject_ap_telemetry.py``.

The :func:`patch` orchestration runs the three in sequence, invokes the
upstream ``open-dread-rando`` CLI to write the modded romfs, and edits
the deployed ``init.lc`` to inject the AP telemetry block. It's the
implementation behind the in-client ``/patch`` command.

The three CLI scripts under ``scripts/`` are now thin wrappers around
this module — single source of truth for the conversion logic.
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

# Markers for idempotent re-injection of the telemetry block.
TELEMETRY_START_MARKER = (
    "-- BEGIN AP TELEMETRY INJECTION (apworld dread.patcher_pipeline)"
)
TELEMETRY_END_MARKER = "-- END AP TELEMETRY INJECTION"


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
# Telemetry injection
# ---------------------------------------------------------------------


def build_telemetry_block(locations: list[dict[str, Any]]) -> str:
    """Generate the Lua block injected into init.lc that wires Switch→AP
    pickup pushes through the modern (Send*-only) exlaunch API.

    Upstream open-dread-rando-exlaunch removed the ``RL.Get*AndSend``
    pull-style Lua helpers — only the C-implemented push primitives
    (``RL.SendIndices`` etc.) remain. Nothing in the patcher's emitted
    Lua actually calls them on pickup. This block:

      1. Pre-computes a static map of ``"<scenario>/<actor>" → pickup_index``.
      2. Wraps ``RandomizerPowerup.OnPickedUp`` (lazily — installed from
         the first ``RL.Update`` tick after the function exists) to
         flag the corresponding bit and force-send the bitfield.
      3. Wraps ``RL.Update`` (engine-driven C tick) for a throttled
         heartbeat that catches any state PC missed during disconnect.
    """
    actor_entries: list[tuple[str, int]] = []
    max_idx = -1
    for loc in locations:
        pidx = loc.get("pickup_index")
        if pidx is None:
            continue
        scenario = loc.get("scenario")
        actor = loc.get("actor")
        if scenario is None or actor is None:
            continue
        actor_entries.append((f"{scenario}/{actor}", int(pidx)))
        if int(pidx) > max_idx:
            max_idx = int(pidx)

    byte_count = (max_idx // 8) + 1 if max_idx >= 0 else 1

    map_body = "\n".join(
        f'    ["{key}"] = {pidx},'
        for key, pidx in sorted(actor_entries, key=lambda t: t[1])
    )

    return f"""{TELEMETRY_START_MARKER}
-- Auto-generated. The block between BEGIN/END markers is replaced
-- atomically when /patch (or scripts/inject_ap_telemetry.py) re-runs.
--
-- Wire contract (PC side parses in
-- apworld/dread/client/context.py::_handle_collected_indices):
--   RL.SendIndices("locations:" .. bitfield_bytes)
-- where bit i of byte b means pickup_index (b*8 + i) is collected.

Init.tAPPickupIndexByActor = {{
{map_body}
}}
Init.iAPBitfieldByteCount = {byte_count}
Init.tAPCollectedBits = Init.tAPCollectedBits or {{}}
Init.tAPCachedBytes = nil

local function _ap_recompute_bytes()
    local bytes = {{}}
    for i = 1, Init.iAPBitfieldByteCount do bytes[i] = 0 end
    for pidx, v in pairs(Init.tAPCollectedBits) do
        if v then
            local byte_idx = math.floor(pidx / 8) + 1
            local bit_mask = 1
            for _ = 1, pidx % 8 do bit_mask = bit_mask * 2 end
            if byte_idx >= 1 and byte_idx <= Init.iAPBitfieldByteCount then
                if bytes[byte_idx] % (bit_mask * 2) < bit_mask then
                    bytes[byte_idx] = bytes[byte_idx] + bit_mask
                end
            end
        end
    end
    Init.tAPCachedBytes = bytes
end

-- Send-throttle state. Bumped each time bits change; APMaybeSend only
-- pushes if the dirty cursor is ahead of the last-sent cursor.
Init.iAPDirtyCursor = Init.iAPDirtyCursor or 0
Init.iAPSentCursor = Init.iAPSentCursor or -1
-- Heartbeat: if no changes, send once every N ticks anyway. Tuned so the
-- heartbeat is roughly 1 Hz against a ~30-60 Hz RL.Update tick.
Init.iAPHeartbeatTickInterval = 60
Init.iAPHeartbeatTicksSince = 0

function Init.APMarkCollected(pickup_index)
    if pickup_index == nil then return end
    if Init.tAPCollectedBits[pickup_index] then return end
    Init.tAPCollectedBits[pickup_index] = true
    Init.tAPCachedBytes = nil
    Init.iAPDirtyCursor = Init.iAPDirtyCursor + 1
end

function Init.APSendBitfield()
    if RL == nil or RL.SendIndices == nil then return end
    if Init.tAPCachedBytes == nil then _ap_recompute_bytes() end
    local parts = {{"locations:"}}
    for i = 1, Init.iAPBitfieldByteCount do
        parts[#parts + 1] = string.char(Init.tAPCachedBytes[i])
    end
    RL.SendIndices(table.concat(parts))
    Init.iAPSentCursor = Init.iAPDirtyCursor
    Init.iAPHeartbeatTicksSince = 0
end

function Init.APMaybeSend()
    if Init.iAPDirtyCursor ~= Init.iAPSentCursor then
        Init.APSendBitfield()
        return
    end
    Init.iAPHeartbeatTicksSince = Init.iAPHeartbeatTicksSince + 1
    if Init.iAPHeartbeatTicksSince >= Init.iAPHeartbeatTickInterval then
        Init.APSendBitfield()
    end
end

local _ap_try_install_pickup_hook = function()
    if RandomizerPowerup == nil or RandomizerPowerup.OnPickedUp == nil then
        return
    end
    if RandomizerPowerup._APHookInstalled then return end
    RandomizerPowerup._APHookInstalled = true
    local _orig_OnPickedUp = RandomizerPowerup.OnPickedUp
    function RandomizerPowerup.OnPickedUp(actor, resources)
        local result = _orig_OnPickedUp(actor, resources)
        if actor ~= nil and Scenario ~= nil and Scenario.CurrentScenarioID ~= nil then
            local key = Scenario.CurrentScenarioID .. "/" .. actor.sName
            local pidx = Init.tAPPickupIndexByActor[key]
            if pidx ~= nil then
                Init.APMarkCollected(pidx)
                Init.APSendBitfield()
            end
        end
        return result
    end
end

local _orig_RL_Update = RL.Update
function RL.Update()
    if _orig_RL_Update ~= nil then
        _orig_RL_Update()
    end
    _ap_try_install_pickup_hook()
    Init.APMaybeSend()
end
{TELEMETRY_END_MARKER}
"""


def inject_telemetry_into_init_lc(init_lc_path: Path, locations: Optional[list[dict]] = None) -> None:
    """Append (or replace) the telemetry block in the deployed ``init.lc``.

    Idempotent — re-running strips any previous block before appending."""
    if locations is None:
        locations = load_json("locations.json")
    block = build_telemetry_block(locations)
    body = init_lc_path.read_text(encoding="utf-8")
    if TELEMETRY_START_MARKER in body:
        before, _, rest = body.partition(TELEMETRY_START_MARKER)
        _, _, after = rest.partition(TELEMETRY_END_MARKER)
        body = before.rstrip() + "\n" + after.lstrip()
    init_lc_path.write_text(body.rstrip() + "\n\n" + block + "\n", encoding="utf-8")


# ---------------------------------------------------------------------
# Orchestration (the /patch command's implementation)
# ---------------------------------------------------------------------


@dataclass
class PatchResult:
    ok: bool
    message: str
    patcher_input_path: Optional[Path] = None
    init_lc_path: Optional[Path] = None
    cli_returncode: Optional[int] = None
    cli_stderr_tail: str = ""
    notes: list[str] = field(default_factory=list)


def check_dependencies() -> Optional[str]:
    """Return None if the patcher's Python deps are importable; else a
    user-readable message naming what's missing and how to fix it."""
    try:
        import open_dread_rando  # noqa: F401
    except ImportError:
        return (
            "open_dread_rando is not installed. Install it with:\n"
            "    pip install open-dread-rando"
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
      4. inject the AP telemetry block into the deployed init.lc
    """
    dep_err = check_dependencies()
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

    # 4: inject telemetry
    # open_dread_rando emits its output under <output-path>/DreadRandovania/.
    # When --output-path is itself a DreadRandovania folder, the patcher
    # writes another nested DreadRandovania subdir. Detect both layouts.
    candidates = [
        dreadvania_install_dir / "DreadRandovania" / "romfs" / "system" / "scripts" / "init.lc",
        dreadvania_install_dir / "romfs" / "system" / "scripts" / "init.lc",
    ]
    init_lc = next((p for p in candidates if p.exists()), None)
    if init_lc is None:
        return PatchResult(
            ok=False,
            message=(
                "patcher ran, but couldn't find init.lc to inject telemetry into.\n"
                f"Looked at: {[str(p) for p in candidates]}"
            ),
            patcher_input_path=patcher_input_path,
            cli_returncode=proc.returncode,
        )
    inject_telemetry_into_init_lc(init_lc)

    n_actors = len(placements.get("placements", []))
    n_cross = sum(1 for p in placements.get("placements", []) if not p.get("is_own_player", True))
    return PatchResult(
        ok=True,
        message=(
            f"patched OK. {n_actors} placements applied "
            f"({n_cross} cross-slot). Re-launch Dread to load the new mod."
        ),
        patcher_input_path=patcher_input_path,
        init_lc_path=init_lc,
        cli_returncode=0,
    )
