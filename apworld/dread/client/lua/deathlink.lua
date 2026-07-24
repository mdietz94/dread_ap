-- dread_ap ORIGINAL — NOT vendored from Randovania.
-- DeathLink kill primitive. Randovania ships no Dread DeathLink, so this is
-- our own addition appended to the bootstrap (after the vendored parts, before
-- RL.Bootstrap=true). Inert until the client calls RL.KillPlayer() in response
-- to an incoming AP DeathLink.
--
-- Mirrors the cutscene-safety discipline of RL.ReceivePickup
-- (bootstrap_part_2.lua): only act when in-game and user interaction is
-- enabled; otherwise reschedule via Game.AddSF (which calls back by name) so a
-- death requested mid-cutscene is deferred, never dropped.
--
-- The kill zeroes the player's current life on the CLifeComponent. Verified on
-- Ryujinx (2.1.0): this runs the game's NORMAL death sequence — the death
-- animation plays, then the death screen — which reads far less jarring than
-- LIFE:ForceDead(false, true), which teleports straight to the death screen
-- with no animation. (ForceDead is what open-dread-rando uses on destructible
-- block actors; on Samus it short-circuits the cinematic.) If a future game
-- version stops reacting to a direct fCurrentLife write, the fallbacks are
-- LIFE:ForceDead(false, true) or LIFE:AddDamageSource(<lethal>). See the
-- dread-deathlink-apis memory.
--
-- Returns a status string (the client reads it): "killed" on a real kill,
-- "deferred" when rescheduled through a cutscene, or a drop reason. The client
-- clears its self-death suppression window on any non-kill status so a later
-- genuine death still broadcasts.
--
-- SAFE-ROOM DROP: a forced death at a Save / Navigation (Adam) / Map station is
-- DROPPED, not fired. These are the same non-combat "safe hub" rooms /warp
-- refuses to leave (lua/warp_guard.lua): Samus keeps control under a save /
-- conversation / map overlay, and a scripted death there can strand that
-- overlay or fight the room's respawn anchor. Unlike the cutscene case we do
-- NOT reschedule — the player may dwell in a safe room indefinitely, so we drop
-- the deathlink outright rather than ambush them the instant they step out. The
-- RL.IsIn*() checks are nil-guarded so a VM that hasn't loaded warp_guard.lua
-- yet fails open (the kill still fires). Boss arenas are deliberately NOT
-- guarded here: dying mid-boss is ordinary gameplay (a normal death respawns at
-- the fight checkpoint), unlike a /warp out of one, which corrupts the fight.
function RL.KillPlayer()
    if Game.GetCurrentGameModeID() ~= 'INGAME' then return "not_ingame" end
    if (RL.IsInSaveRoom and RL.IsInSaveRoom())
        or (RL.IsInNavRoom and RL.IsInNavRoom())
        or (RL.IsInMapRoom and RL.IsInMapRoom()) then
        return "in_safe_room"
    end
    local player = Game.GetPlayer()
    if player == nil or player.LIFE == nil then return "no_player" end
    if not Scenario.IsUserInteractionEnabled(true) then
        Game.AddSF(0.5, "RL.KillPlayer", "")
        return "deferred"
    end
    player.LIFE.fCurrentLife = 0
    return "killed"
end
