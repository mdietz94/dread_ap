# Installing on a modded Switch

This guide gets you to "Phase 1 ready" — your Switch is reachable on the LAN,
exlaunch is installed, and `scripts/phase1_validate.py` returns exit code 0.
It does **not** make the game playable in Archipelago yet; that's Phase 2+.

## Prerequisites

- Modded Switch on Atmosphere CFW (≥ 1.7.0, FW ≥ 18.0.0)
- Metroid Dread 2.1.0 installed natively (digital or cartridge)
- Your Switch on the same LAN as the PC running the AP client
- `prod.keys` / `dev.keys` already in place (for prior NSP/RomFS work; not needed at runtime)
- Switch IP reachable from your PC — note it from Settings → Internet → Connection Status

## Step 1 — install the exlaunch sysmodule (upstream Randovania)

We use the upstream sysmodule as-is — no fork, no rebuild. Get the latest
release from [open-dread-rando-exlaunch releases](https://github.com/randovania/open-dread-rando-exlaunch/releases)
(or build from source if you prefer).

The release is a ZIP with the layout:

```
atmosphere/
  contents/
    010093801237C000/
      exefs/
        subsdk1
        main.npdm
```

Where `010093801237C000` is Dread's title id. Copy `atmosphere/` to the root
of your microSD card (merge with the existing `atmosphere/` folder).

Verify on the Switch:
1. Reboot, hold volume-up if your Atmosphere setup needs it
2. Launch Dread
3. The game should boot normally. The sysmodule starts the TCP listener on
   port 6969 once Dread is in the title screen or in-game.

If the game refuses to boot or hangs on the splash, the sysmodule isn't
compatible with your Atmosphere version. Check the upstream release notes
for the matching Atmosphere range.

## Step 2 — confirm the wire is up

From the PC, with the Switch on the title screen or in-game:

```pwsh
cd C:\Users\maxwe\Documents\dread_ap
python scripts\phase1_validate.py <switch-ip>
```

Expected output (good):

```
[phase1] connecting to 192.168.1.42:6969
[phase1] connected; sending PACKET_HANDSHAKE(interests=MULTIWORLD)
[phase1] handshake ack: success=True payload=b''
[phase1] T1: bare Lua eval (does the runtime answer at all?)
  T1 success=True payload=b'2'
[phase1] T2: does the Randovania `RL` namespace exist?
  T2 success=True payload=b'table'
[phase1] T3: Randovania-style API-version handshake
  T3 success=True payload=b'1,4096,true,...,v2.1.0'
    api_version  = 1
    buffer_size  = 4096
    bootstrap    = true
    layout_uuid  = ...
    game_version = v2.1.0
...
[phase1] OK — wire is up. Proceed to Phase 2.
```

If T1 passes but T2 says `payload=b'nil'`, the exlaunch is running but
your game is not patched with open-dread-rando — that's expected at this
stage. T2 needs a Randovania-patched ROM to succeed (which is Phase 2's
job). For pure "wire validation", T1 passing is sufficient.

## Troubleshooting

**Connection refused**: exlaunch sysmodule not loaded. Check that `subsdk1`
landed in the right directory and that Atmosphere accepts it (check the
Atmosphere logs on the SD card).

**Connection timeout**: Switch isn't on your network or has a firewall.
Try pinging the IP first. Some travel routers (per the smo_archipelago
experience) block UDP broadcasts and have aggressive client isolation —
direct TCP usually still works.

**T1 fails with success=False**: the bootstrap Lua isn't loaded. Make sure
Dread is actually in-game (the bootstrap fires on `bsoLaunchScript`, which
happens at title load).

**T3 fails / `RL` is nil**: ROM is not patched. This is fine for Phase 1.
Continue when Phase 2 lands a patcher run.

## Ryujinx (dev iteration)

For dev work without a Switch:

1. Build (or download) the exlaunch sysmodule release as above
2. In Ryujinx: right-click Dread → Open Mods Directory → drop the `exefs/`
   files (`subsdk1` and `main.npdm`) into a subdirectory there
3. Boot Dread in Ryujinx
4. The sysmodule listens on `127.0.0.1:6969`
5. Run `python scripts\phase1_validate.py 127.0.0.1`

Ryujinx is fastest for iteration, but always validate the final flow on
real hardware before declaring a phase done — there have been JIT timing
quirks in the past (see the smo_archipelago `M9` notes for an example).
