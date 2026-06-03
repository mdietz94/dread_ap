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
function RL.KillPlayer()
    if Game.GetCurrentGameModeID() ~= 'INGAME' then return end
    local player = Game.GetPlayer()
    if player == nil or player.LIFE == nil then return end
    if not Scenario.IsUserInteractionEnabled(true) then
        Game.AddSF(0.5, "RL.KillPlayer", "")
        return
    end
    player.LIFE.fCurrentLife = 0
end
