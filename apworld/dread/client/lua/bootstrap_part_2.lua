function RL.InventoryIndex()
    local playerSection =  Game.GetPlayerBlackboardSectionName()
    return Blackboard.GetProp(playerSection, "InventoryIndex") or 0
end
function RL.ReceivedPickups()
    local playerSection =  Game.GetPlayerBlackboardSectionName()
    return Blackboard.GetProp(playerSection, "ReceivedPickups") or 0
end
function RL.GetReceivedPickupsAndSend(reset)
    if reset then
        RL.PendingPickup = nil
    end
    RL.SendReceivedPickups(RL.ReceivedPickups())
end
function RL.GivePendingPickup()
    if Scenario.IsUserInteractionEnabled(true) then
        Scenario.QueueAsyncPopup(RL.PendingPickup.msg, RL.PendingPickup.popup or 7.0)
        -- The grant (ConfirmPickup) happens now; the only thing the reschedule
        -- gates is clearing PendingPickup so the next item can be accepted. For a
        -- lone item we hold the default 7.5s so its popup is the visible one; for
        -- a backlog (a "release") the client sends a short delay so items flow
        -- fast. Refresh InventoryIndex first so the client's next ReceivePickup
        -- carries the post-grant index (the grant bumped it) and is accepted.
        local delay = RL.PendingPickup.delay or 7.5
        Game.AddSF(delay, "RL.GetInventoryAndSend", "")
        Game.AddSF(delay, "RL.GetReceivedPickupsAndSend", "b", true)
        RL.ConfirmPickup()
    else
        Game.AddSF(0.5, "RL.GivePendingPickup", "")
    end
end
function RL.ConfirmPickup()
    RL.PendingPickup.cls.OnPickedUp(nil,RL.PendingPickup.progression)
    Scenario.WriteToPlayerBlackboard("ReceivedPickups","f",RL.ReceivedPickups()+1)
end
function RL.ReceivePickup(msg,cls,progression_string,receivedPickupIndex,inventoryIndex,popup,delay)
    if not RL.PendingPickup then
        if receivedPickupIndex == RL.ReceivedPickups() and inventoryIndex == RL.InventoryIndex() then
            progression = assert(loadstring("return " .. progression_string))()
            RL.PendingPickup={cls=cls,progression=progression,msg=msg,popup=popup,delay=delay}
            Game.AddSF(0, "RL.GivePendingPickup", "")
        else
            Game.AddSF(0, "RL.GetInventoryAndSend", "")
            Game.AddSF(0.05, "RL.GetReceivedPickupsAndSend", "b", false)
        end
    end
end