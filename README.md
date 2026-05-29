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

**Phase 1 in progress.** Wire-up validation script at [scripts/phase1_validate.py](scripts/phase1_validate.py).
Nothing playable yet.

## Architecture

```
[ Switch / Dread 2.1.0 ]  <--TCP/binary LAN-->  [ PC client (Python) ]  <--ws-->  [ AP server ]
   exlaunch sysmodule                              DreadContext(CommonContext)       archipelago.gg
   (UPSTREAM Randovania)                           Kivy GUI (Tracker + Connections)
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

- [open-dread-rando](https://github.com/randovania/open-dread-rando) — the RomFS patcher. Forked to accept Archipelago slot_data instead of a Randovania seed.
- [open-dread-rando-exlaunch](https://github.com/randovania/open-dread-rando-exlaunch) — the in-game sysmodule. Used as-is.
- [mercury-engine-data-structures](https://github.com/randovania/mercury-engine-data-structures) — Mercury Engine file IO. Pip dependency, no fork.
- [randovania/randovania/game_connection/](https://github.com/randovania/randovania/tree/main/randovania/game_connection) — the reference Lua protocol we replicate.

When we find bugs in vendor/ that aren't AP-specific, we file upstream.

## Plan

See [PLAN.md](PLAN.md) (a copy of the original implementation plan).

## License

MIT for our code; upstream licenses preserved under vendor/. See [LICENSE](LICENSE).
