"""Behavioral tests for client/lua/elun_gate.lua (Elun quarantine-gate re-open).

Runs the real Lua against a minimal fake of the engine surface it touches
(Game.GetActor, CUTSCENE:HasCutsceneBeenPlayed, EVENTPROP:ForceOpen, the
s060_quarantine level table, Scenario.OnSubAreaChange). Needs ``lupa``; skips
cleanly without it.

Run with:  python -m pytest apworld/dread/tests/test_elun_gate.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

lupa = pytest.importorskip("lupa")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))

from dread.client import bootstrap as bs  # noqa: E402

GATE_LUA = (ROOT / "client" / "lua" / "elun_gate.lua").read_text(encoding="utf-8")

# Fake engine. `played` drives HasCutsceneBeenPlayed; FORCE_OPENS counts gate
# opens; ORIG_CALLS proves the wrapped hook still chains to the original.
_FAKE_ENGINE = """
RL = {}
FORCE_OPENS = 0
ORIG_CALLS = 0
CUTSCENE_PLAYED = false
CUTSCENE_ERRORS = false
HAS_CUTSCENE_ACTOR = true
HAS_LEVEL_TABLE = true
CurrentScenarioID = "s060_quarantine"
local gate = {EVENTPROP = {}}
function gate.EVENTPROP:ForceOpen() FORCE_OPENS = FORCE_OPENS + 1 end
local cs = {CUTSCENE = {}}
function cs.CUTSCENE:HasCutsceneBeenPlayed()
    if CUTSCENE_ERRORS then error("no such method") end
    return CUTSCENE_PLAYED
end
Game = {}
function Game.GetActor(name)
    if name == "ev_gatesealed_second" then return gate end
    if name == "cutsceneplayer_113" and HAS_CUTSCENE_ACTOR then return cs end
    return nil
end
s060_quarantine = {}
function s060_quarantine.OnCutscene113Ended()
    local a = Game.GetActor("ev_gatesealed_second")
    if a ~= nil then a.EVENTPROP:ForceOpen() end
end
Scenario = {}
function Scenario.OnSubAreaChange(o, oa, n, na, f) ORIG_CALLS = ORIG_CALLS + 1; return "orig" end
"""


@pytest.fixture
def lua():
    rt = lupa.LuaRuntime(unpack_returned_tuples=True)
    rt.execute(_FAKE_ENGINE)
    rt.execute(GATE_LUA)
    return rt


def _transition(rt, old="collision_camera_012", new="collision_camera_011"):
    return rt.eval(f'Scenario.OnSubAreaChange("{old}", "", "{new}", "", false)')


def test_return_trip_reopens_gate(lua):
    """The reported bug: cutscene already played (warped out, came back) ⇒ the
    Save Station → Exterior Bridge transition must re-open the shutter."""
    lua.execute("CUTSCENE_PLAYED = true")
    assert _transition(lua) == "orig"  # original hook's return value preserved
    assert lua.eval("FORCE_OPENS") == 1
    assert lua.eval("ORIG_CALLS") == 1


def test_first_arrival_leaves_gate_to_the_cutscene(lua):
    lua.execute("CUTSCENE_PLAYED = false")
    _transition(lua)
    assert lua.eval("FORCE_OPENS") == 0
    assert lua.eval("ORIG_CALLS") == 1


@pytest.mark.parametrize("old,new,scenario", [
    ("collision_camera_011", "collision_camera_012", "s060_quarantine"),  # reverse
    ("collision_camera_000", "collision_camera_011", "s060_quarantine"),  # other room
    ("collision_camera_012", "collision_camera_011", "s050_forest"),      # other region
])
def test_other_transitions_do_nothing(lua, old, new, scenario):
    lua.execute(f'CUTSCENE_PLAYED = true; CurrentScenarioID = "{scenario}"')
    _transition(lua, old, new)
    assert lua.eval("FORCE_OPENS") == 0
    assert lua.eval("ORIG_CALLS") == 1


def test_played_check_error_still_opens(lua):
    """If HasCutsceneBeenPlayed misbehaves, err toward an open gate (a sealed
    region is the worse failure)."""
    lua.execute("CUTSCENE_ERRORS = true")
    _transition(lua)
    assert lua.eval("FORCE_OPENS") == 1


def test_missing_cutscene_actor_still_opens(lua):
    lua.execute("HAS_CUTSCENE_ACTOR = false")
    _transition(lua)
    assert lua.eval("FORCE_OPENS") == 1


def test_falls_back_to_gate_actor_without_level_table(lua):
    lua.execute("CUTSCENE_PLAYED = true; s060_quarantine = nil")
    assert lua.eval("RL.ReopenElunGate()") == "opened"
    assert lua.eval("FORCE_OPENS") == 1


def test_engine_error_never_breaks_the_transition(lua):
    lua.execute("CUTSCENE_PLAYED = true; "
                "function s060_quarantine.OnCutscene113Ended() error('boom') end")
    assert _transition(lua) == "orig"
    assert lua.eval("ORIG_CALLS") == 1


def test_reinstall_does_not_double_wrap(lua):
    """A reconnect re-sends the bootstrap; the wrap must install once."""
    lua.execute(GATE_LUA)
    lua.execute("CUTSCENE_PLAYED = true")
    _transition(lua)
    assert lua.eval("FORCE_OPENS") == 1
    assert lua.eval("ORIG_CALLS") == 1


def test_shipped_bootstrap_block_runs(lua):
    """The block actually sent over the wire (post-substitution) is the same
    working code, and fits the 4 KiB send buffer."""
    import json
    data = ROOT / "data"
    items = json.loads((data / "items.json").read_text(encoding="utf-8"))
    locations = json.loads((data / "locations.json").read_text(encoding="utf-8"))
    block = next(b for b in bs.build_bootstrap_code(items, locations)
                 if "function RL.ReopenElunGate" in b)
    assert len(block) < 4096
    rt = lupa.LuaRuntime(unpack_returned_tuples=True)
    rt.execute(_FAKE_ENGINE)
    rt.execute(block)
    rt.execute("CUTSCENE_PLAYED = true")
    _transition(rt)
    assert rt.eval("FORCE_OPENS") == 1
