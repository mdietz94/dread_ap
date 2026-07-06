# vendor/CHANGES.md

This file tracks our local diffs vs. the upstream sources in `vendor/`.

## Current status

`open-dread-rando-exlaunch/` is now a **HARD FORK** at
`github.com/mdietz94/open-dread-rando-exlaunch-bridge`. Upstream sync
is abandoned. The wire format is entirely different: Switch is the TCP
dialer (UDP-discovered), line-delimited JSON envelope wraps the
Lua-eval RPC. See [docs/wire-protocol.md](../docs/wire-protocol.md) for
the full spec.

The previously-separate Ryujinx-fix patch is folded into the fork's
initial commit. `apworld/dread/_setup/build.py` no longer runs a
separate `git apply` step.

`open-dread-rando/` is still clean and reference-only.

### 2026-05-31 — Switch sysmodule hard fork (bridge networking)

- New file structure under `source/program/`:
  - `discovery.{cpp,hpp}` — UDP probe + /24 subnet sweep
  - `json_line.{cpp,hpp}` — fixed-buffer JSON encoder + Reader (ported
    from `smo_archipelago/switch-mod/src/util/Json.{cpp,hpp}` with the
    namespace renamed to `dread::ap::json`)
  - `remote_api.{cpp,hpp}` — REWRITTEN: TCP client + JSON dispatch +
    `nn::nifm` init + exponential backoff
  - `main.cpp` — EDITED: lua_exec_reply emit, JSON push helpers
  - `config.mk` — passes `BRIDGE_HOST` + `MOD_VERSION` to CXXFLAGS
- Wire is now line-delimited JSON, max 8 KiB / line. Switch dials PC
  on TCP 17777; PC binds UDP 17776 for discovery. Backoff: 1/2/5/10/30s.
- The Lua bootstrap (`apworld/dread/client/lua/bootstrap_part_*.lua`)
  was edited to emit hex-encoded bitfields (`collected`) and pass
  separate `(index, jsonArrStr)` args (`inventory`); the C senders
  build the JSON envelope around the bootstrap-composed inner values.

### 2026-07-06 — nn::fs worker-thread fix LANDED (fork PR #6)

- PR #6 on `mdietz94/open-dread-rando-exlaunch`
  ("fs: never touch nn::fs from the worker thread; close handles on all
  paths", branch `fix/no-worker-thread-fs`, head `2aa652a`) is MERGED to
  main as `392869c64693f84e77aec794271ce694ccba7c4b`.
- Fixes the save-breaking `nn::fs` unclosed-accessor abort (Result
  2002-6455): `rom:/ap_config.json` is now read once on the game thread
  inside the RomMounted window, and `OpenAndReadFile` closes handles on
  all failure paths. Crashing NSO build id was `39B5F4A9…`; the fixed
  build is `FCB054ED…`.
- `PINNED_EXLAUNCH_COMMIT` in `apworld/dread/_setup/build.py` is bumped
  to the merge commit, so user builds (and the release workflow's
  prebuilt sysmodule) pick up the fix. Note: a rebuild before this bump
  would have clobbered manually-deployed fixed binaries with the old
  pin — that hazard is now closed.

The remainder of this file describes the OLD soft-fork model that the
bridge work superseded.

## Subdirectories

### `open-dread-rando/`

Shallow clone of [randovania/open-dread-rando](https://github.com/randovania/open-dread-rando).
The RomFS patcher for Metroid Dread.

**Why vendored, not forked**: the patcher reads a fully-documented JSON
schema ([src/open_dread_rando/files/schema.json](open-dread-rando/src/open_dread_rando/files/schema.json)).
Everything we need for v0.1 — pickup remap, starting_items, starting_location,
elevators, layout_uuid for seed identity, hints, text_patches — is already
expressible via the JSON. So we don't fork; we install upstream as a pip
dep and write an adapter that produces the JSON from AP slot_data.

**Why vendored at all**: to pin a known-working commit, to read source
during dev iteration without going to GitHub, and to have a working copy
ready if we *do* hit something that needs a fork later.

If upstream gains an AP-relevant bug we need to fix immediately, branch
off this checkout, push to a personal fork, install via
`pip install git+...` instead of pypi, and add a note here. File the
upstream PR concurrently.

### `open-dread-rando-exlaunch/`

Shallow clone of [randovania/open-dread-rando-exlaunch](https://github.com/randovania/open-dread-rando-exlaunch).
The in-game sysmodule (subsdk9 + main.npdm) that opens the Lua-eval socket
on port 6969.

**Why vendored**: was reference-only; now a soft fork carrying one local
patch ("exlaunch: Ryujinx-safe non-blocking socket loop", below). End
users still download the upstream release for production hardware. The
local fork only matters if we ship our own .nso build, or for the
upstream PR.

**Patch — exlaunch: Ryujinx-safe non-blocking socket loop**

Files touched:
- `open-dread-rando-exlaunch/source/nn/socket.hpp` — add `#include <poll.h>`
  and an `s32 Poll(pollfd*, ulong, s32)` declaration alongside the other
  `nn::socket::*` wrappers. The Nintendo SDK function exists but the
  exlaunch header didn't declare it.
- `open-dread-rando-exlaunch/source/program/remote_api.cpp` — replace the
  2 ms busy-sleep loop in `SocketSpawn` with `nn::socket::Poll`, add
  `POLLHUP`/`POLLERR` handling for instant dead-peer teardown, and replace
  the blocking `nn::socket::Recv(..., 0)` calls in `ParseClientPacket`
  with a poll + `MSG_DONTWAIT` helper (`RecvFullNonBlocking`) bounded by a
  per-call deadline. `SendLogic` switched to `MSG_DONTWAIT` with
  leave-on-EWOULDBLOCK / partial-erase. `ResetValueAliveTimer` retuned for
  the new poll cadence (~3 s idle window, down from 10 s — `POLLHUP` is
  now the primary mechanism, the timer is a backstop). Comment block at
  the top updated to note that `nn::socket::Poll` works on Ryujinx (the
  earlier "fcntl doesn't work, MSG_DONTWAIT is the only non-blocking
  knob" advisory remained accurate but missed `Poll`).

**Why**: upstream's loop is blocking on a half-open Ryujinx socket
(`Recv(..., 0)` mid-Lua-payload or `Send(..., 0)` to a dead peer parks
the whole socket subsystem per the Ryujinx limitation documented at the
top of the upstream file), forcing a 10-second wait for the manual
keep-alive timer before the sysmodule can re-accept. The patch makes
every socket op non-blocking and uses `poll()` as the only thing that
ever sleeps in the kernel. Architecture is otherwise unchanged: single
thread, manual keep-alive, binary TLV framing.

**Verified-by-precedent**: smo_archipelago's Switch client uses the same
`nn::socket::Poll` call (`switch-mod/src/ap/ApClient.cpp:209-216`,
`sockPollReadable`) and that codepath has been validated on Ryujinx +
real HW. So we know `Poll` works; the Dread upstream just didn't try it.

**Upstream PR plan**: file against
[randovania/open-dread-rando-exlaunch](https://github.com/randovania/open-dread-rando-exlaunch)
once we've confirmed the patched .nso boots cleanly on Ryujinx and grants
an item end-to-end. The diff is small enough (~80 lines) to land as one
PR. Once merged, point this vendor copy at the merged commit and delete
this section.

## Updating the vendored copies

```pwsh
cd vendor\open-dread-rando
git fetch --depth 1 origin
git checkout origin/main
```

After updating, re-run `scripts/phase1_validate.py` against a Switch with
the matching exlaunch release installed to confirm nothing in the Lua
bootstrap surface area changed underneath us.
