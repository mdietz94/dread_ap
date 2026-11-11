-- dread_ap ORIGINAL -- NOT vendored from Randovania.
-- Re-open Elun's quarantine shutter (actor ev_gatesealed_second) on return trips.
--
-- The s060_quarantine level script opens that gate ONLY from the end callback of
-- cutscene 113 ("quarantine arrival", launched on the Save Station ->
-- Exterior Bridge transition, cc012 -> cc011) or from the X-release trigger that
-- sits BEHIND the gate. The cutscene plays once per save, and the gate's open
-- state does not survive a scenario reload. So leaving Elun before releasing the
-- X -- which `/warp` makes possible from anywhere (vanilla has no exit there) --
-- seals Elun for good: on return the cutscene is already "played", its callback
-- never fires, and the only other opener is unreachable. Randovania logic models
-- Exterior Bridge -> Bridge Gate -> Purple Drapes as free, so the region must
-- stay enterable.
--
-- Fix: on that same cc012 -> cc011 transition, once the arrival cutscene has
-- been played, call the level's own OnCutscene113Ended (= EVENTPROP:ForceOpen).
-- Vanilla already force-opens this gate when it may be open
-- (OnBeforeQuarantineDoorsOpenCutsceneStarts), so a redundant open is harmless.
-- On the very first arrival (cutscene not yet played) we stay out of the way and
-- let the cutscene's own callback open it. If the played-check itself errors,
-- we open anyway (sealing the region is the worse failure).
--
-- s060_quarantine.OnSubAreaChange forwards to Scenario.OnSubAreaChange AFTER its
-- own LaunchCutscene_113 call, so we chain-wrap the Scenario hook (same pattern
-- as warp_guard.lua) and run after the level's handler. Installed once
-- (RL._ElunGateInstalled) so a reconnect re-sending the bootstrap can't
-- double-wrap; every engine call is pcall'd so a surprise here can never break
-- subarea transitions.

function RL.ReopenElunGate()
    local cs = Game.GetActor("cutsceneplayer_113")
    if cs ~= nil then
        local ok, played = pcall(function() return cs.CUTSCENE:HasCutsceneBeenPlayed() end)
        if ok and played == false then return "first_arrival" end
    end
    if s060_quarantine ~= nil and type(s060_quarantine.OnCutscene113Ended) == "function" then
        s060_quarantine.OnCutscene113Ended()
        return "opened"
    end
    local gate = Game.GetActor("ev_gatesealed_second")
    if gate == nil then return "no_gate" end
    gate.EVENTPROP:ForceOpen()
    return "opened"
end

if not RL._ElunGateInstalled and type(Scenario.OnSubAreaChange) == "function" then
    RL._ElunGateInstalled = true
    local _rl_orig_elun_onsubareachange = Scenario.OnSubAreaChange
    function Scenario.OnSubAreaChange(old_sa, old_ag, new_sa, new_ag, disable_fade)
        local r = _rl_orig_elun_onsubareachange(old_sa, old_ag, new_sa, new_ag, disable_fade)
        if CurrentScenarioID == "s060_quarantine"
                and old_sa == "collision_camera_012" and new_sa == "collision_camera_011" then
            pcall(RL.ReopenElunGate)
        end
        return r
    end
end
