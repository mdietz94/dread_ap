# Wire protocol reference

The PC client talks to the Switch over TCP on port **6969**. This port is
opened by the upstream [open-dread-rando-exlaunch](https://github.com/randovania/open-dread-rando-exlaunch)
sysmodule. We did not author it; we just speak its protocol.

Reference implementation (MIT, by Randovania):
- [dread_executor.py](https://github.com/randovania/randovania/blob/main/randovania/game_connection/executor/dread_executor.py) — framing, connect, keep-alive
- [dread_remote_connector.py](https://github.com/randovania/randovania/blob/main/randovania/game_connection/connector/dread_remote_connector.py) — `RL.ReceivePickup` and high-level item routing
- [mercury_remote_connector.py](https://github.com/randovania/randovania/blob/main/randovania/game_connection/connector/mercury_remote_connector.py) — shared poll loop with Samus Returns

If anything below is wrong, those three files are authoritative.

## Byte layout

### Packet type enum

In upstream code these are declared as `IntEnum` members with literal
`b"N"` values. Python's `int()` accepts bytes that parse as integer
literals, so `int(b"3") == 3`, and `member.to_bytes()` writes one raw byte.
The wire bytes are therefore `0x01`..`0x09`, not ASCII `'1'..'9'`.

| Value | Name | Direction | Notes |
|---|---|---|---|
| 0x01 | PACKET_HANDSHAKE | both | First packet on the wire. Body is a single interest byte. |
| 0x02 | PACKET_LOG_MESSAGE | S→C | Log line from the sysmodule, debug only. |
| 0x03 | PACKET_REMOTE_LUA_EXEC | both | Body is `[4-byte LE length][utf-8 lua source]`. Switch eval's it via `loadstring` and replies. |
| 0x04 | PACKET_KEEP_ALIVE | C→S | Empty body. Sent every 2 s by `_send_keep_alive`. |
| 0x05 | PACKET_NEW_INVENTORY | S→C | Per-item counts, emitted by `RL.GetInventoryAndSend()`. |
| 0x06 | PACKET_COLLECTED_INDICES | S→C | Bitfield of collected pickup nodes. |
| 0x07 | PACKET_RECEIVED_PICKUPS | S→C | Current value of `Blackboard.ReceivedPickups`. |
| 0x08 | PACKET_GAME_STATE | S→C | Current scenario / game mode / boss state. |
| 0x09 | PACKET_MALFORMED | S→C | Sysmodule failed to parse the last C→S frame; treat as soft error. |

### Request frames (PC → Switch)

```
PACKET_HANDSHAKE:
  [0x01][interest_byte]
    interest_byte = 0x02 for MULTIWORLD, 0x01 for LOGGING (bit-or to combine)

PACKET_REMOTE_LUA_EXEC:
  [0x03][len_le_u32][utf-8 lua source]

PACKET_KEEP_ALIVE:
  [0x04]
```

### Response frames (Switch → PC)

Every Switch-originated frame, including unsolicited push messages, uses
the same 4-byte header:

```
[success_byte][len_le_u24][payload]
```

Where `success_byte` is `0x01` for ok / `0x00` for error, and `len_le_u24`
is a 24-bit little-endian length (struct trick: read 3 bytes, append `\x00`,
unpack as `<l`). Payload bytes follow with no padding.

Push messages (NEW_INVENTORY, COLLECTED_INDICES, etc.) carry the same
header but are not in reply to a particular Lua-exec request — the client
must demultiplex by content. Randovania's executor maintains a request
counter (`(req+1) % 256`) but doesn't put it in the frame; matching is
positional (FIFO over the read loop).

## Connect sequence

1. `asyncio.open_connection(host, 6969)`
2. Write `PACKET_HANDSHAKE` with `interests = MULTIWORLD` (`b"\x01\x02"`)
3. Read one response frame (handshake ack)
4. Run Lua:
   ```lua
   return string.format('%d,%d,%s,%s,%s', RL.Version, RL.BufferSize,
                        tostring(RL.Bootstrap), Init.sLayoutUUID, GameVersion)
   ```
5. Parse ASCII CSV reply → `(api_version, buffer_size, bootstrap, layout_uuid, game_version)`
6. If `bootstrap == "false"`, send the bootstrap Lua files (split into chunks ≤ `buffer_size` bytes — upstream calls this in `DreadExecutor.bootstrap()`). When the game is patched with open-dread-rando this is already true and we skip it.
7. Run `Game.AddSF(2.0, RL.UpdateRDVClient, "")` to schedule the 2-second poll loop
8. Start the keep-alive task (write `PACKET_KEEP_ALIVE` every 2 s)
9. Start the read loop (parse incoming frames, dispatch by `PacketType`)

## RL.* Lua API surface

The exlaunch sysmodule defines a Lua namespace `RL` via four bootstrap
chunks (`bootstrap_part_0.lua` … `bootstrap_part_3.lua`) at game launch.
That namespace is our entire API surface — we do not get raw memory
access; everything goes through the game's own Lua runtime.

| Function | Purpose | Reply packet |
|---|---|---|
| `RL.GetInventoryAndSend()` | Read every tracked item's `RandomizerPowerup.GetItemAmount()` and reply with a JSON map | `PACKET_NEW_INVENTORY` |
| `RL.GetCollectedIndicesAndSend()` | Read the Blackboard bitfield of locally-collected pickup nodes | `PACKET_COLLECTED_INDICES` |
| `RL.GetReceivedPickupsAndSend()` | Read `Blackboard.GetProp(playerSection, "ReceivedPickups")` (a counter, used for "did the server already grant me item N?" idempotence) | `PACKET_RECEIVED_PICKUPS` |
| `RL.GetCurrentGameStateAndSend()` | Read `Game.GetCurrentGameModeID()` + scenario + boss state | `PACKET_GAME_STATE` |
| `RL.UpdateRDVClient(arg)` | One periodic tick — fires all four queries above. Re-schedules itself via `Game.AddSF(2.0, RL.UpdateRDVClient, "")`. | All of the above |
| `RL.ReceivePickup(message, cls, progression_string, num_pickups, inventory_index)` | Grant an AP item live. Internally calls `RL.PendingPickup.cls.OnPickedUp(nil, progression)`, the game's native pickup callback. Queues if user input is currently disabled (cutscene/cinematic). | none (in-game popup) |

Other things the bootstrap exposes:
- `Init.sLayoutUUID` — UUID baked into the patched ROM, used to confirm the seed running matches what we generated
- `Init.bBeatenSinceLastReboot` — flips true after the final cutscene. Used for goal detection.
- `GameVersion` — string from the game's own globals (`"v2.1.0"`)

## Item grant call

```python
# Reference: dread_remote_connector.py:DreadRemoteConnector.send_pickup
lua = (
    f"RL.ReceivePickup("
    f"{message!r},"           # popup text, e.g. "Received Missile Tank from Player 2"
    f"{parent},"              # int — Lua object reference to the pickup parent class
    f"{progression_as_lua!r}," # Lua table literal: '{ {item_id=42, quantity=1}, ... }'
    f"{num_pickups},"         # int — index of this grant in the received-pickups sequence
    f"{self.inventory_index}" # int — server-side index, for idempotence
    f")"
)
await executor.run_lua_code(lua)
```

The `progression_as_lua` table is the heart of the grant: each entry has
`{item_id=<RandomizerPowerup id>, quantity=<int>}`. For a Missile Tank
this is `{ {item_id=ITEM_MissileTank, quantity=1} }`. The Lua side passes
it to `OnPickedUp`, which is the game's native pickup-grant function, so
all downstream effects (inventory increment, sound, HUD flash) fire as
they would for a vanilla pickup.

> **What our client actually sends (and two caveats).** The snippet above is
> the Randovania *reference*. Current `open-dread-rando-exlaunch` does NOT
> define `RL.ReceivePickup`, so our client
> (`apworld/dread/client/protocol.py::build_receive_pickup_lua`) calls
> `RandomizerPowerup.OnPickedUp(nil, <resources>)` directly. Two consequences
> the reference's signature implies but our path does NOT provide:
> 1. **No idempotence.** `inventory_index` is a no-op in our Lua, so a repeat
>    send re-grants the item. Additive items (Missile / Energy / Power Bomb
>    tanks) would gain capacity twice. Delivery dedup lives entirely in the
>    PC-side `received_count` cursor, which advances on send.
> 2. **No cutscene queueing.** Upstream `RL.ReceivePickup` queues a grant when
>    user input is disabled (cinematic); calling `OnPickedUp` directly does
>    not, so an item delivered mid-cutscene can be dropped. This is the
>    documented risk #1 in CLAUDE.md. A safe replay needs idempotent delivery
>    (gate on the `PACKET_RECEIVED_PICKUPS` / `Blackboard.ReceivedPickups`
>    count) FIRST — it cannot be lifted from smo_archipelago as-is.

## Polling cadence

Upstream's `RL.UpdateRDVClient` self-schedules every 2 s via `Game.AddSF`.
This is the floor for any latency-sensitive feature. Push-style (sysmodule
fires immediately on pickup) would require an exlaunch patch upstream.

For v0.1 we accept the 2 s p99 for both location-checks and game-state.
Item grants are unaffected — they're synchronous request/response over the
Lua-exec channel.
