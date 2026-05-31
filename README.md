# dread_ap

Metroid Dread Archipelago for modded Switch.

Sister project to [smo_archipelago](https://github.com/mdietz94/smo_archipelago).
The pattern is the same: in-game module on the Switch talks to a Python
client on the PC over LAN; the Python client talks to an Archipelago server
over the standard AP websocket. The crucial difference is that Dread's
in-game module is **not ours** — it's [open-dread-rando-exlaunch](https://github.com/randovania/open-dread-rando-exlaunch),
a Randovania project that already exposes a Lua-eval TCP socket on port
6969. We do not write any C++ or Switch sysmodule code; we just speak the
existing protocol.

## Status

The apworld (logic, items/locations, options) and the PC client — wire protocol,
idempotent cutscene-safe item delivery, goal detection, and a **Kivy GUI** — are
implemented and unit-tested. The remaining gate is a live integration smoke on
real hardware / Ryujinx (does the bootstrap load on the 2.1.0 ROM, does an item
pop, does a check register). Early wire-up validation lives at
[scripts/phase1_validate.py](scripts/phase1_validate.py).

## Installing & running the client

1. Install the apworld into your Archipelago checkout:
   `python scripts/install_apworld.py` (folder mode → `worlds/dread/`; pass
   `--mode apworld` for a `dread.apworld` zip, or `--ap-root <path>` to target a
   specific install).
2. Launch Archipelago's **Launcher** and click **"Dread Client"** (or open a
   `.dreadap` file). The client window opens with the standard AP server bar plus
   a **"Dread"** tab (status + log) and a top-bar **Switch-status pill**.
3. Enter your AP server address and connect as usual. Point the client at your
   Switch / Ryujinx: click the Switch pill → edit the IP → **Reconnect**, or run
   `/dread_connect <ip[:port]>` in the command bar.
4. The Switch dial sometimes loses the race with Dreadvania's own startup. If the
   pill is orange/"error", just click it and Reconnect (or `/dread_connect`) — the
   delivery protocol is idempotent, so retrying never double-grants items.

## Architecture

```
[ Switch / Dread 2.1.0 ]  <--TCP/binary LAN-->  [ PC client (Python) ]  <--ws-->  [ AP server ]
   exlaunch sysmodule                              DreadContext(CommonContext)       archipelago.gg
   (UPSTREAM Randovania)                           Kivy GUI (status + Switch pill)
   - bootstraps RL.* Lua namespace                 LuaProtocol on port 6969
   - opens TCP :6969                               (lifted from smo_archipelago)
   - executes arbitrary Lua, returns result
   romfs/
   (open-dread-rando patcher output;
    per-seed item placements, starting
    inventory, teleporter shuffle)
```

## Relationship to Randovania

This is a soft fork. We vendor and credit:

- [open-dread-rando](https://github.com/randovania/open-dread-rando) (GPL-3.0) — the RomFS patcher. Vendored as a pinned git submodule at `vendor/open-dread-rando/`; we don't fork — we write an adapter that turns AP slot_data into the patcher's existing JSON input schema.
- [open-dread-rando-exlaunch](https://github.com/randovania/open-dread-rando-exlaunch) (GPL-2.0) — the in-game sysmodule. We carry two local patches against it (`apworld/dread/_setup/exlaunch-*.diff`) applied by `apworld/dread/_setup/build.py`; upstream source is not redistributed.
- [mercury-engine-data-structures](https://github.com/randovania/mercury-engine-data-structures) (MIT) — Mercury Engine file IO. Pip dependency, no fork.
- [randovania/randovania](https://github.com/randovania/randovania) (GPL-3.0) — the reference Lua-eval protocol we replicate, and the source for the verbatim Lua bootstrap files under `apworld/dread/client/lua/`.

When we find bugs upstream that aren't AP-specific, we file PRs.

## Plan

See [PLAN.md](PLAN.md) (a copy of the original implementation plan).

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).

Copyright (C) 2026 Maxwell Dietz.

This is a combined work that incorporates GPL-3.0 code from
[open-dread-rando](https://github.com/randovania/open-dread-rando) (vendored as
a pinned submodule under `vendor/open-dread-rando/`) and verbatim Lua bootstrap
files from [randovania](https://github.com/randovania/randovania) (GPL-3.0,
under `apworld/dread/client/lua/`). GPL-2.0 patches to
[open-dread-rando-exlaunch](https://github.com/randovania/open-dread-rando-exlaunch)
live in `apworld/dread/_setup/exlaunch-*.diff`. The combined work is GPL-3.0
under the GPL's compatibility rules (GPL-2.0 → GPL-3.0 via "or later"
relicensing). [mercury-engine-data-structures](https://github.com/randovania/mercury-engine-data-structures)
is an MIT pip dependency.
