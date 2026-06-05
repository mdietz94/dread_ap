-- dread_ap ORIGINAL -- NOT vendored from Randovania.
-- /warp boss-arena guard. Warping out of a boss fight with Game.LoadScenario
-- corrupts the encounter: the arena's intro / door-lock / checkpoint state
-- half-commits, so the fight can't be re-entered or reset cleanly (a player hit
-- this with Kraid -- re-entry broke the fight and the death-respawn bricked the
-- game). So the client refuses to /warp while Samus stands in a boss arena.
--
-- The engine exposes no getter for the live collision camera, but it fires
-- Scenario.OnSubAreaChange(old_sa, old_ag, new_sa, new_ag, ...) on every subarea
-- (collision-camera) transition -- the same hook the room-name display rides. We
-- WRAP it (chaining, never replacing, so the upstream body's progressive-model /
-- blast-shield / room-name updates still run) to record the live scenario +
-- subarea; RL.IsInBossArena() then checks them against the baked boss table.
--
-- We do NOT reset RL.CurrentSubArea on scenario load: that keeps the guard armed
-- through a death-respawn inside the same arena (no fresh subarea event fires, so
-- the stored Kraid camera persists and the warp stays blocked). The one residual
-- gap is connecting fresh while already standing in a boss arena -- CurrentSubArea
-- is nil until the next transition, so the very first /warp there isn't caught.
-- The wrap is installed once (RL._WarpGuardInstalled) so a reconnect re-sending
-- this bootstrap can't double-wrap and call the upstream body twice.
RL.BossArenas = TEMPLATE("boss_arenas")

function RL.IsInBossArena()
    if Game.GetCurrentGameModeID() ~= 'INGAME' then return false end
    if RL.CurrentSubArea == nil then return false end
    -- Only trust the tracked subarea if it belongs to the live scenario (guards
    -- against a stale camera id colliding with a different scenario's ids).
    if RL.CurrentScenario ~= CurrentScenarioID then return false end
    local cams = RL.BossArenas[CurrentScenarioID]
    return cams ~= nil and cams[RL.CurrentSubArea] == true
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
