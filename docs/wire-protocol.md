# Wire protocol — DreadClient ↔ Switch

This is the authoritative reference for the line-delimited JSON envelope
used between the patched Switch sysmodule
(`vendor/open-dread-rando-exlaunch/source/program/`) and DreadClient
(`apworld/dread/client/`).

The Switch is the dialer. DreadClient binds TCP `0.0.0.0:17777` and UDP
`0.0.0.0:17776`; the Switch's worker thread does UDP discovery, then TCP
connect, then exchanges line-delimited JSON.

## Discovery (UDP 17776)

Two-stage probe per (re)connect cycle:

1. **Loopback** — UDP probe to `127.0.0.1:17776`, 250 ms timeout. Covers
   Ryujinx on the same host as DreadClient.
2. **Subnet sweep** — burst-send probes to every host in the `/24` whose
   seed is read at runtime from `rom:/ap_config.json` `bridge_host` (written
   at deploy time from `detect_lan_ip()`; no compile-time bake). 2 s collect
   window. First valid reply wins. Skipped when the config is absent/invalid
   (loopback above still covers Ryujinx).

Replaces the old 255.255.255.255 broadcast (silently dropped on travel
routers, mesh repeaters, IGMP-snooping switches).

### Probe (Switch → PC)

```json
{"t":"discover","mod_ver":"dread-bridge-0.1.0"}
```

### Reply (PC → Switch)

```json
{"t":"bridge","host":"192.168.1.50","port":17777,"seed":"X4F2"}
```

`host` is the PC's auto-detected LAN IP (via `detect_lan_ip()`),
`port` is the BridgeServer's TCP port.

## TCP transport (port 17777)

Each message is one line of UTF-8 JSON terminated by `\n`. Lines
exceeding 8 KiB are dropped on both sides.

`SO_KEEPALIVE = 1` set on connect. Switch's read loop also handles
`POLLHUP / POLLERR` to detect drops quickly.

Backoff on disconnect / discovery failure: `1, 2, 5, 10, 30 s` cap.
Backoff resets after a connection is held for ≥ 1 s.

## Message catalogue

Every message has a `t` field identifying the type. Unknown `t` is
forward-compatibly ignored.

### `hello` — Switch → PC

First message after TCP connect. Empty `layout_uuid` is normal; the
bootstrap pushes a `layout_uuid` push once Lua land is up.

```json
{
  "t": "hello",
  "mod_ver": "dread-bridge-0.1.0",
  "dread_ver": "2.1.0",
  "layout_uuid": "",
  "device_id": ""
}
```

### `hello_ack` — PC → Switch

PC's response. `ok=false` rejects (mod_ver mismatch); the PC then closes
the socket. Otherwise the wire is live.

```json
{
  "t": "hello_ack",
  "ok": true,
  "slot": "Samus",
  "seed": "X4F2",
  "subs": {"logging": true, "multiWorld": true}
}
```

### `lua_exec` — PC → Switch

The RPC. PC sends Lua source; Switch's game thread evaluates it and
replies via `lua_exec_reply` (same `seq`). The Switch supports one
in-flight `lua_exec` at a time (single game-thread Lua state); the PC
serializes via a per-connection exec lock.

```json
{"t":"lua_exec","seq":42,"src":"return tostring(Init.bBeatenSinceLastReboot)"}
```

### `lua_exec_reply` — Switch → PC

```json
{"t":"lua_exec_reply","seq":42,"ok":true,"result":"false"}
```

### `log` — Switch → PC

Lua-driven via `RemoteLua.SendLog(level, msg)`. Routed by DreadClient to
the GUI log pane.

```json
{"t":"log","level":"info","msg":"Switch booted, RL.* ready"}
```

### `inventory` — Switch → PC

Push from `RL.GetInventoryAndSend()`. `index` is the Switch's
`InventoryIndex` blackboard prop (delivery match value).

```json
{"t":"inventory","index":3,"inventory":[1,0,2,1]}
```

### `collected` — Switch → PC

Push from `RL.GetCollectedIndicesAndSend()`. `hex` is a lowercase hex
bitfield: bit `b` of byte `i` set ⇒ pickup_index `i*8 + b` collected.

```json
{"t":"collected","hex":"0a0301"}
```

### `received_pickups` — Switch → PC

The delivery cursor. Drives `_attempt_delivery` on the PC side.

```json
{"t":"received_pickups","count":5}
```

### `game_state` — Switch → PC

```json
{"t":"game_state","scenario":"s010_cave","beaten":false}
```

### `layout_uuid` — Switch → PC

Pushed once after bootstrap so the PC can fix up the connection's
`device_id`.

```json
{"t":"layout_uuid","value":"5e2c1f78-..."}
```

### `ping` / `pong` — both directions

```json
{"t":"ping","ts_ms":1731536400000}
```

### `kick` — PC → Switch

Sent to inactive Switches (multi-Switch case). Switch closes the socket;
the worker loop redials after backoff.

```json
{"t":"kick","reason":"inactive"}
```

## Why JSON over the old binary protocol?

Connection issues were the dominant player support burden. The new
design:

- **Switch dials out** — no LAN-side firewall / NAT issues for the user,
  no IP entry, no manual reconnect after handheld↔dock swaps.
- **UDP discovery** — the user never types an IP; the PC's LAN IP is
  baked into the sysmodule at build time as the sweep seed.
- **Line-delimited JSON** — tcpdump-readable, forward-compatible, easy
  to write fakes for. The Lua-eval RPC fits inside one message type
  (`lua_exec`) so the entire Randovania `RL.*` bootstrap is preserved.
- **Multi-Switch** — multiple Switches can be connected at once (active
  + inactive); auto-promotion on disconnect.

See [docs/wire-wiring-notes.md](wire-wiring-notes.md) for the historical
binary-format retrospective.
