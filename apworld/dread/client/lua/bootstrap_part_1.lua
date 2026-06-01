function RL.GetInventoryAndSend()
    local r={}
    for i,n in ipairs(RL.InventoryItems) do
        r[i]=RandomizerPowerup.GetItemAmount(n)
    end
    local inventory = string.format("[%s]",table.concat(r,","))
    RL.SendInventory(RL.InventoryIndex(), inventory)
end
RL.InventoryItems=TEMPLATE("inventory")