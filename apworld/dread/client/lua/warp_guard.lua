-- dread_ap ORIGINAL -- NOT vendored from Randovania.
-- /warp location guards. Game.LoadScenario from the wrong room is destructive:
--   * boss arena -> half-commits the fight's intro/door-lock/checkpoint so it
--     can't be re-entered or reset (a player hit this with Kraid: re-entry broke
--     the fight, the death-respawn bricked the game);
--   * Navigation ("Adam") room / Save Station / Map Station -> the conversation,
--     save dialog, or map console overlay keeps Samus controllable but survives
--     the reload, stranding an undismissable box. All are safe hubs you can walk
--     out of, so blocking /warp costs nothing.
-- No state flag distinguishes any of these from open play, so we gate on the live
-- collision camera. The engine has no getter for it, but fires
-- Scenario.OnSubAreaChange on every transition -- we WRAP that (chaining, never
-- replacing, so upstream's progressive-model / blast-shield / room-name updates
-- still run) to record the live scenario+subarea, then RL.IsIn*() check it.
--
-- We do NOT reset RL.CurrentSubArea on scenario load: that keeps the guard armed
-- through a death-respawn in the same room (no fresh subarea event fires). Residual
-- gap: connecting fresh while already standing in a guarded room leaves
-- CurrentSubArea nil until the next transition, so the first /warp there isn't
-- caught. The wrap installs once (RL._WarpGuardInstalled) so a reconnect re-sending
-- this bootstrap can't double-wrap. The RL.BossArenas / RL.NavRooms / RL.SaveRooms
-- / RL.MapRooms tables are assigned by their own bootstrap blocks (bootstrap.py),
-- keeping this code block under the 4 KiB send buffer; RL.IsIn*() read them at
-- /warp time, so those blocks just have to run before the first /warp (order-independent).

-- Shared check: is the live tracked subarea in `cams` for the current scenario?
local function _rl_subarea_in(cams_by_scenario)
    if Game.GetCurrentGameModeID() ~= 'INGAME' then return false end
    if RL.CurrentSubArea == nil then return false end
    -- Only trust the tracked subarea if it belongs to the live scenario (guards
    -- against a stale camera id colliding with a different scenario's ids).
    if RL.CurrentScenario ~= CurrentScenarioID then return false end
    local cams = cams_by_scenario[CurrentScenarioID]
    return cams ~= nil and cams[RL.CurrentSubArea] == true
end

function RL.IsInBossArena()
    return _rl_subarea_in(RL.BossArenas)
end

function RL.IsInNavRoom()
    return _rl_subarea_in(RL.NavRooms)
end

function RL.IsInSaveRoom()
    return _rl_subarea_in(RL.SaveRooms)
end

function RL.IsInMapRoom()
    return _rl_subarea_in(RL.MapRooms)
end

-- Ghavoran flipper-trap rooms. Unlike the guards above (safe hubs where a warp is
-- merely pointless), turning the Ghavoran flipper without the Spider Magnet floor
-- lowered sticks the area for good; Game.LoadScenario preserves the Blackboard flip
-- so /warp can't undo it -- only a checkpoint reload can. Refuse /warp here so the
-- player reloads instead of committing the broken state. Table: RL.FlipperTrapRooms.
function RL.IsInFlipperTrap()
    return _rl_subarea_in(RL.FlipperTrapRooms)
end

-- type-check guards a non-rando / unexpected ROM: if the hook is missing we
-- skip the wrap (boss detection stays inert, /warp falls back to allowed)
-- rather than later calling a nil and crashing every subarea transition.
if not RL._WarpGuardInstalled and type(Scenario.OnSubAreaChange) == "function" then
    RL._WarpGuardInstalled = true
    local _rl_orig_onsubareachange = Scenario.OnSubAreaChange
    function Scenario.OnSubAreaChange(old_sa, old_ag, new_sa, new_ag, disable_fade)
        RL.CurrentScenario = CurrentScenarioID
        RL.CurrentSubArea = new_sa
        return _rl_orig_onsubareachange(old_sa, old_ag, new_sa, new_ag, disable_fade)
    end
end
