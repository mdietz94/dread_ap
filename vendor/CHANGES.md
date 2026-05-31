# vendor/CHANGES.md

This file tracks how we consume upstream Randovania sources under `vendor/`.

## Current status

- `open-dread-rando/` — pinned **git submodule** at `66d85916` (upstream v2.20.1).
  Carries the patcher source so end users do not need to `pip install
  open-dread-rando`. Clean (no local patches); we adapt to its JSON input
  schema instead of forking.
- `open-dread-rando-exlaunch/` — **gitignored**; if you clone it locally for
  reference, it carries two of our local patches applied by
  `apworld/dread/_setup/build.py`:
  1. "exlaunch: Ryujinx-safe non-blocking socket loop" — see below.
  2. "exlaunch: TCP-client + UDP-discovery topology" — see below.

## Subdirectories

### `open-dread-rando/` — submodule

Pinned git submodule from [randovania/open-dread-rando](https://github.com/randovania/open-dread-rando)
(**GPL-3.0**). The RomFS patcher for Metroid Dread.

Clone with `git clone --recurse-submodules`, or initialize an existing clone
with `git submodule update --init vendor/open-dread-rando`.

**Why submodule, not pip dep**: bringing the source into the tree gives us
deterministic upgrades, lets the setup wizard skip a `pip install open-dread-rando`
step, and pins the JSON schema / model_data this apworld targets to a known
commit. We still don't fork — we write an adapter that produces the
patcher's existing JSON input shape from AP slot_data.

**Bumping the pin**:

```pwsh
cd vendor\open-dread-rando
git fetch origin
git checkout <new-tag-or-sha>
cd ..\..
git add vendor/open-dread-rando
git commit -m "Bump open-dread-rando to <tag>"
```

After bumping, re-run `pytest apworld/dread/tests/` (the schema /
model_data fixtures we read from the submodule may have shifted).

**Runtime deps still required**: open-dread-rando depends on
`mercury-engine-data-structures`, `jsonschema`, `json-delta`, and
`open-dread-rando-exlaunch` (the Python helper, distinct from the Switch
sysmodule). These remain pip-installed into the user's patcher Python; the
wizard installs them. We just no longer install open-dread-rando itself.

### `open-dread-rando-exlaunch/` — gitignored dev clone

Optional local clone of [randovania/open-dread-rando-exlaunch](https://github.com/randovania/open-dread-rando-exlaunch)
(**GPL-2.0**). The in-game sysmodule (subsdk9 + main.npdm) that opens the
Lua-eval socket on port 6969.

**Why gitignored**: end users get the sysmodule by following the upstream
README's build instructions (or downloading a release `.nso`). We are not in
the business of redistributing Nintendo-adjacent binaries, and the source
isn't needed at runtime — only when rebuilding the patched `.nso`. Our two
local patches live as plain diff files at
`apworld/dread/_setup/exlaunch-*.diff` and apply against a user-side clone
by `apworld/dread/_setup/build.py`.

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

For `open-dread-rando/` see "Bumping the pin" above. For
`open-dread-rando-exlaunch/` (if you keep a local reference clone), pull
from the upstream remote and re-apply the diff files in
`apworld/dread/_setup/`.

After updating, re-run `scripts/phase1_validate.py` against a Switch with
the matching exlaunch release installed to confirm nothing in the Lua
bootstrap surface area changed underneath us.
