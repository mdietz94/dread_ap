# vendor/CHANGES.md

This file tracks our local diffs vs. the upstream sources in `vendor/`.

## Current status

`open-dread-rando-exlaunch/` carries two local patches, applied in order
by `apworld/dread/_setup/build.py`:

  1. "exlaunch: Ryujinx-safe non-blocking socket loop" — see below.
  2. "exlaunch: TCP-client + UDP-discovery topology" — see below.

`open-dread-rando/` is still clean and reference-only.

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

**Why vendored**: was reference-only; now a soft fork carrying two local
patches (see below). End users still download the upstream release for
production hardware. The local fork only matters if we ship our own
.nso build, or for the upstream PRs.

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

**Patch — exlaunch: TCP-client + UDP-discovery topology**

Files touched:
- `open-dread-rando-exlaunch/source/nn/socket.hpp` — add `SendTo` and
  `RecvFrom` wrapper declarations alongside the others. (`Bind`,
  `Connect`, `InetHtons`, `InetAton`, `Poll` are already declared post
  Ryujinx-fix.)
- `open-dread-rando-exlaunch/source/program/remote_api.cpp` —
  - Delete `g_TcpSocket` and `CreateServerSocket()` (the listening
    socket is gone).
  - Add discovery helpers in the anon namespace: `ProbeJson`,
    `FindSubstr`, `ParseBridgeReply`, `WaitUdpReply`, `ProbeOne`,
    `SweepSubnet`, `ResolveBridge`, `DialBridge`. The probe/reply wire
    format matches `apworld/dread/client/discovery.py` exactly:
    `{"t":"discover","mod_ver":"dread-ap"}\n` →
    `{"t":"bridge","host":"<ipv4>","port":<int>}\n`.
  - Rewrite `SocketSpawn`'s outer loop to resolve via
    `ResolveBridge(loopback → /24 sweep)` then `DialBridge()` →
    inner Poll loop (unchanged from the Ryujinx-fix patch) → teardown
    → 1 s backoff → retry. The inner Poll discipline + `Close`-only
    teardown survive verbatim because they're load-bearing for
    Ryujinx half-open recovery.
  - Add an `#include "bridge_config.hpp"` so the `/24` seed comes from
    a build-time `DEFAULT_BRIDGE_SUBNET` macro.
- `open-dread-rando-exlaunch/source/program/bridge_config.hpp` — new
  file. Defines `DEFAULT_BRIDGE_SUBNET` to `"192.168.1.1"` under a
  `#ifndef` fallback. The /setup wizard's BridgeIpPage rewrites this
  via `build.write_bridge_config(seed_ip)` to bake the user's PC
  LAN IP before each compile; without wizard customization the
  fallback is used.

**Why**: the previous topology had the PC dial the Switch on port 6969,
which forced an exponential-backoff supervisor on the PC side to handle
"Switch not ready yet" / "Switch IP unknown" / "DHCP changed
mid-session." We inverted to mirror smo_archipelago's discipline: the PC
binds UDP :17779 + TCP :17777 and answers a small JSON discovery probe;
the Switch sweeps loopback then its baked `/24` to find the bridge and
TCP-dials it. The wire frame format (`PACKET_HANDSHAKE`,
`PACKET_REMOTE_LUA_EXEC`, etc.) is unchanged — only the underlying
socket initiation flips. nifm is intentionally NOT used to learn the
Switch's own IP for the sweep seed (it crashed sail-init in SMO; the
build-time `DEFAULT_BRIDGE_SUBNET` stands in).

**Verified-by-precedent**: smo_archipelago's `ApDiscovery.cpp` runs the
same loopback → `/24` sweep flow and has been validated on Ryujinx + real
HW. Our `ResolveBridge` is a port of that file's `resolveBridge` with
the SMO-specific JSON encoder/parser replaced by `FindSubstr`-based
inline scans (the dread tree doesn't carry SMO's `util/Json.hpp`).

**Verified locally**: builds clean against the pinned exlaunch commit
under devkitPro msys2 bash. Produces a ~200 KB subsdk9 with no compile
errors / warnings. End-to-end Ryujinx integration smoke is the next
gate (deploy the new subsdk9; confirm the Switch UDP-discovers
DreadClient's responder within ~2 s of game launch, TCP-dials, runs
the RL.* bootstrap, and an in-game pickup registers on the AP server).

**Upstream PR plan**: this lands as a separate PR from the Ryujinx-fix
because it changes user-visible behavior (port 6969 listening removed).
Discuss with randovania before opening — they may want to keep the
listening-side as an option for direct-Switch tooling. Could merge as
a feature flag with the listening side gated off by default.

## Updating the vendored copies

```pwsh
cd vendor\open-dread-rando
git fetch --depth 1 origin
git checkout origin/main
```

After updating, re-run `scripts/phase1_validate.py` against a Switch with
the matching exlaunch release installed to confirm nothing in the Lua
bootstrap surface area changed underneath us.
