-- dread_ap ORIGINAL -- NOT vendored from Randovania.
-- "Lights Out" race mode: hide the in-game map so the player navigates from
-- memory. open-dread-rando has NO map-hide patch, so we do it at runtime by
-- setting Visible=false on the map GUI objects -- the same idiom the vendored
-- custom_scenario.lua uses for its own HUD panels
-- (GUI.SetProperties(obj, { Visible = false })). Inert until the client sets
-- the RL.LightsOut flag (from the seed's `lights_out` slot_data) and calls
-- RL.ApplyLightsOut().
--
-- The HUD is rebuilt per scenario (Scenario.InitGui), so we WRAP InitGui
-- (chaining, never replacing -- upstream's DNA / death-counter / room-name
-- setup still runs) to re-hide the map on every load. Installed once
-- (RL._LightsOutInstalled) so a reconnect re-sending this bootstrap can't
-- double-wrap. The type-check guards a non-rando / unexpected ROM: if InitGui
-- is missing we skip the wrap (lights-out stays inert) rather than crashing.
--
-- !!! The map display-object paths below are CANDIDATES and MUST be confirmed
-- on hardware/Ryujinx via /poke (enumerate the GUI children of IngameMenuRoot).
-- Each is hidden nil-safely (GUI.GetDisplayObject returns nil for a missing
-- path, exactly as custom_scenario.lua relies on), so an unconfirmed path is
-- simply skipped, never fatal. The HUD minimap and the pause-menu map are
-- separate objects; pause-map suppression in particular is unverified.

-- Candidate map GUI object paths (HUD minimap + pause-menu map). Confirm/refine
-- these via /poke before relying on hardware behaviour.
RL.LightsOutTargets = RL.LightsOutTargets or {
    "IngameMenuRoot.minimap",
    "IngameMenuRoot.iconshudcomposition.minimap",
    "IngameMenuRoot.map",
}

-- Hide every resolvable map object. No-op unless RL.LightsOut is set, so it is
-- safe to call from the per-scenario hook on every load regardless of the seed.
function RL.ApplyLightsOut()
    if not RL.LightsOut then return end
    for _, path in ipairs(RL.LightsOutTargets) do
        local obj = GUI.GetDisplayObject(path)
        if obj ~= nil then
            GUI.SetProperties(obj, { Visible = false })
        end
    end
end

if not RL._LightsOutInstalled and type(Scenario.InitGui) == "function" then
    RL._LightsOutInstalled = true
    local _rl_orig_initgui = Scenario.InitGui
    function Scenario.InitGui(...)
        local r = _rl_orig_initgui(...)
        RL.ApplyLightsOut()
        return r
    end
end
