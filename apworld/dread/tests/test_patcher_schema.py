"""Validate the door/transport/start patcher output against open-dread-rando's
real schema.json — the contract the patcher actually enforces.

This catches format drift between what we emit and what the patcher accepts
(e.g. a door_type not in the enum, or a door_patch with a null actor). Skips
gracefully when the vendored schema, ``jsonschema``, or the AP runtime is absent.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT.parent) not in sys.path:
    sys.path.insert(0, str(ROOT.parent))

REPO = ROOT.parent.parent
GRAPH_PATH = ROOT / "data" / "logic_graph.json"


def _schema_path():
    """Locate the vendored open-dread-rando schema (submodule or bundled copy)."""
    for cand in (
        REPO / "vendor" / "open-dread-rando" / "src" / "open_dread_rando"
        / "files" / "schema.json",
        ROOT / "_vendored_patcher" / "open_dread_rando" / "files" / "schema.json",
    ):
        if cand.exists():
            return cand
    return None


def _door_type_enum(schema):
    return set(schema["properties"]["door_patches"]["items"]
               ["properties"]["door_type"]["enum"])


schema_required = pytest.mark.skipif(
    not (GRAPH_PATH.exists() and _schema_path()),
    reason="needs logic_graph.json + vendored open-dread-rando schema.json",
)


# ---- pure checks (no AP runtime) -----------------------------------------

@schema_required
def test_assignable_door_types_in_schema_enum():
    """Every door weakness we may ASSIGN renders to a door_type the patcher
    accepts (the enum has e.g. 'sensor_door', not the logic DB's 'phantom_cloak'
    — so assigning Sensor Lock / Phase Shift doors would be rejected; they must
    not be in the assignable pool)."""
    g = json.loads(GRAPH_PATH.read_text())
    schema = json.loads(_schema_path().read_text(encoding="utf-8"))
    enum = _door_type_enum(schema)
    dr = g["door_rando"]
    pool = [w for w in dr["change_to"] if w != dr.get("locked_weakness")]
    for weakness in pool:
        dt = dr["weakness_door_type"].get(weakness)
        assert dt is not None, f"assignable weakness {weakness!r} has no door_type"
        assert dt in enum, f"door_type {dt!r} ({weakness}) not in schema enum"


@schema_required
def test_every_rando_door_has_patcher_actor():
    """A rando-eligible door must have an actor the patcher can rewrite (else its
    door_patch would carry a null actor and the schema rejects it)."""
    g = json.loads(GRAPH_PATH.read_text())
    for sid, meta in g["dock_sides"].items():
        assert meta["patcher"].get("actor"), f"door {sid} has no patcher actor"


# ---- full merged patcher input (AP runtime gated) ------------------------

def _ap() -> bool:
    try:
        import BaseClasses, Options  # noqa: F401
        from worlds.AutoWorld import World  # noqa: F401
        import jsonschema  # noqa: F401
    except ImportError:
        return False
    return True


runtime = pytest.mark.skipif(
    not (_ap() and GRAPH_PATH.exists() and _schema_path()),
    reason="needs AP runtime + jsonschema + vendored schema",
)


@runtime
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_full_patcher_input_validates(seed):
    import jsonschema
    from test.general import setup_multiworld, gen_steps
    from Fill import distribute_items_restrictive
    from dread.World import DreadWorld
    from dread.patcher_pipeline import placements_to_overrides, merge_overrides

    template = json.loads(
        (ROOT / "data" / "starter_preset_patcher.json").read_text(encoding="utf-8"))
    schema = json.loads(_schema_path().read_text(encoding="utf-8"))

    mw = setup_multiworld(DreadWorld, gen_steps, seed=seed,
                          options={"door_lock_rando": 1, "transport_rando": 1,
                                   "starting_area": 3})
    distribute_items_restrictive(mw)
    overrides = placements_to_overrides(mw.worlds[1]._build_placements_payload())
    patcher_input = merge_overrides(template, overrides)

    jsonschema.validate(patcher_input, schema)
    assert patcher_input["door_patches"], "expected door_patches"
    assert patcher_input["elevators"], "expected elevators"
