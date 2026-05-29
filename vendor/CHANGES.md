# vendor/CHANGES.md

This file tracks our local diffs vs. the upstream sources in `vendor/`.

## Current status (as of project Phase 1)

**No local changes.** Both repos are vendored as shallow clones from their
default branches. We use them as references and as a build-time pin; we do
not ship modified copies yet.

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

**Why vendored**: reference only. We do not redistribute this binary; end
users download the upstream release directly per
[docs/install-switch.md](../docs/install-switch.md). Our copy is so we can
read the Lua bootstrap files and the C++ source when debugging wire
issues, and pin a release tag in our docs.

We expect zero modifications here. If we hit a sysmodule limitation, the
right path is an upstream PR (or, if blocked, fork and submit upstream
when ready) — not a private hard fork.

## Updating the vendored copies

```pwsh
cd vendor\open-dread-rando
git fetch --depth 1 origin
git checkout origin/main
```

After updating, re-run `scripts/phase1_validate.py` against a Switch with
the matching exlaunch release installed to confirm nothing in the Lua
bootstrap surface area changed underneath us.
