# Vendored Randovania bootstrap Lua

These files are copied **verbatim** from Randovania (MIT licensed):

    randovania/games/dread/assets/lua/{bootstrap_part_0..3,bootstrap_locations}.lua

Provenance: randovania commit `68a2b5238d185eb29757e37f0ce5d485a18be2c0`.

They define the `RL.*` namespace the exlaunch sysmodule exposes a socket for but
does **not** itself implement: the collected-indices / inventory / received-pickups
query functions, the periodic `RL.UpdateRDVClient` poller, and — critically —
`RL.ReceivePickup` (idempotent, cutscene-safe item delivery via a single pending
pickup that defers through cinematics; see `bootstrap_part_2.lua`).

In Randovania these are sent live to the Switch at every connect by
`game_connection/executor/dread_executor.py::bootstrap()`. The patcher
(open-dread-rando) bakes only no-op stubs into the ROM (`custom_init.lua`), so the
PC client **must** send these. `client/bootstrap.py` replicates
`get_bootstrapper_for` + the chunked send, fed by this apworld's own data tables
instead of Randovania's game database.

`TEMPLATE("...")` placeholders are substituted at runtime by `client/bootstrap.py`
(mirroring `replace_lua_template` / `lua_convert` with `wrap_strings=False`, which
is verbatim string passthrough). Keep these files byte-identical to upstream so
they can be re-synced; do all customization in `bootstrap.py`.
