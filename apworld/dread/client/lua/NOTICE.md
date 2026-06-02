# Vendored Randovania bootstrap Lua

These files are copied **verbatim** from [randovania/randovania](https://github.com/randovania/randovania)
(**GPL-3.0**; this is the reason our combined work is GPL-3.0 — see the top-level
[LICENSE](../../../../LICENSE)):

    randovania/games/dread/assets/lua/{bootstrap_part_0..3,bootstrap_locations}.lua

Provenance: randovania commit `68a2b5238d185eb29757e37f0ce5d485a18be2c0`.

## Local modifications (not upstream)

`bootstrap_part_2.lua` — `RL.ReceivePickup` / `RL.GivePendingPickup` take two
optional trailing args (`popup`, `delay`) so the PC client can override the
hard-coded 7.0s popup / 7.5s reschedule per delivery. Upstream gates every
received item to ~7.5s, which makes an Archipelago "release" (a burst of items)
crawl in at one item every several seconds. When more items are queued behind
the current one the client sends short values so the backlog drains fast, and
falls back to the upstream defaults (`... or 7.0` / `... or 7.5`) for a lone
item so its popup still lingers. `GivePendingPickup` also re-sends
`InventoryIndex` alongside `ReceivedPickups` on the reschedule, so the client's
next `ReceivePickup` carries the post-grant index. See `client/protocol.py`
(`build_receive_pickup_lua`) and `client/context.py` (`_attempt_delivery`).
Candidate to upstream. All other files remain byte-identical.

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
is verbatim string passthrough). Keep these files as close to byte-identical to
upstream as possible so they can be re-synced; prefer doing customization in
`bootstrap.py`, and document any unavoidable in-file edits under "Local
modifications" above.
