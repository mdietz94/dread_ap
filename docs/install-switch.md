# Installing on a modded Switch

This guide gets you to "AP-ready" — DreadClient is installed, the patched
exlaunch sysmodule is on your SD card (or Ryujinx), and the per-seed romfs
patcher will run automatically when you connect to an Archipelago server.

For the end-to-end walkthrough that screenshots each step, see
[first-time-setup.md](first-time-setup.md). This guide is the
backgrounder — what each piece is, why it's there, and what to do if
something doesn't behave.

## Prerequisites

- Modded Switch on Atmosphere CFW (>= 1.7.0, FW >= 18.0.0)
- Metroid Dread 2.1.0 installed natively (digital or cartridge)
- A pre-extracted vanilla Dread 2.1.0 romfs folder on the PC. If you
  haven't extracted yet, dump from a clean retail source with
  [NXDumpTool](https://github.com/DarkMatterCore/nxdumptool/releases)
  and extract with hactool / nxgameuncomp. The per-seed patcher overlays
  on top of this — we never read NSP/XCI directly.
- An SD card you can write to from the PC (USB / card reader)

Ryujinx-only users: the SD card item is replaced by an existing Ryujinx
install. Everything else still applies.

## The flow

```
                  +-------------------------+
   DreadClient -> | /setup                  |
                  | * checks devkitPro / Py |
                  | * builds the sysmodule  |
                  | * deploys subsdk9       |
                  | * remembers romfs path  |
                  +-------------------------+
                              |
                              v
   DreadClient -> /connect ap.gg:38281 player-name
                              |
                              v
   _on_connected -> _maybe_auto_patch(romfs_path, deploy_dir)
                              |
                              v
                          ROM patched
                              |
                              v
                  Restart Dread in Ryujinx
                  (or boot from SD on real HW)
                              |
                              v
                       Play the seed
```

## Step 1 - install Archipelago and the Dread apworld

Get the latest Archipelago release from
[archipelago.gg](https://archipelago.gg/tutorial/Archipelago/setup/en).
Drop the dread `.apworld` into `<AP-install>/custom_worlds/` (or build
the apworld zip from this repo with `python scripts/install_apworld.py`).

Launch the Archipelago Launcher. You should see a **Dread Client** entry.

## Step 2 - run `/setup` once

Click **Dread Client**. The first time you do, the Kivy setup wizard pops
automatically (PR-A4 of the setup-wizard stack). Walk through the pages:

1. **Welcome** - the requirements list. Read it; this is where you
   confirm Dread 2.1.0 and that you have an extracted romfs.
2. **Prereqs** - green-checks for **devkitPro / devkitA64**,
   **Python 3.12**, and **open-dread-rando**.
   - Python 3.12 has an "Auto-install" button that runs
     `winget install Python.Python.3.12` for you.
   - devkitPro's installer is interactive (admin prompts, registry
     writes). Click "Install..." to open
     [devkitpro.org/wiki/Getting_Started](https://devkitpro.org/wiki/Getting_Started),
     install the **Switch-dev** package group, then click **Re-check**.
   - `open-dread-rando` is vendored as a git submodule at
     `vendor/open-dread-rando/` — if you cloned without
     `--recurse-submodules`, run
     `git submodule update --init vendor/open-dread-rando` and **Re-check**.
     The wizard then installs the patcher's pip-only runtime deps
     (mercury-engine-data-structures, jsonschema, json-delta,
     open-dread-rando-exlaunch) into the detected Python 3.12 when you
     click "Auto-install".
3. **RomFS picker** - Browse to your extracted Dread 2.1.0 romfs folder
   (the one with `system/` and `packs/` subdirs). Persisted to
   `%APPDATA%/dread_ap/setup_state.json` as `romfs_path`.
4. **Build** - runs three subprocess steps live in the log box:
   - `git clone` (or fetch + reset) of
     `randovania/open-dread-rando-exlaunch` at the pinned commit
   - `git apply --ignore-whitespace` of our Ryujinx-fix patch
     (see [vendor/CHANGES.md](../vendor/CHANGES.md) for the half-open
     socket bug it addresses)
   - `./exlaunch.sh build` under devkitPro's bundled msys2 bash
   - About 30-60 seconds on a warm cache; a few minutes on first
     run because of the clone.
5. **Deploy target** - pick Ryujinx (auto-detected under
   `%APPDATA%/Ryujinx/`), SD card (auto-detected by `atmosphere/` marker),
   or Custom folder (for users who stage to a network share / DBI /
   Goldleaf).
6. **Done** - the success banner explains the target-specific "next
   step" (the Ryujinx-relaunch reminder, the eject-SD-and-reinsert
   reminder, etc).

The wizard remembers everything in `%APPDATA%/dread_ap/setup_state.json`;
re-running `/setup` from inside DreadClient is fine (it pre-fills from
that file, and the Build page skips a no-op rebuild when the outputs
are already on disk).

## Step 3 - connect to AP

Type or click `/connect ap.gg:38281 SlotName`. On `_on_connected`,
DreadClient logs

```
Auto-patch: writing per-seed romfs overlay to <deploy-dir>
            (vanilla romfs: <your-extracted-romfs>)
```

and runs `open_dread_rando` against your romfs in a worker thread. The
patcher takes ~3 seconds on a warm cache. When it's done, you can boot
Dread.

## Step 4 - boot Dread

**Ryujinx**: close Dread completely (back out to the game list, NOT just
the title screen) and relaunch it. Ryujinx applies exefs mods at process
start, so the patched `subsdk9` only loads on a fresh game launch.

**Real Switch (SD card)**: eject the SD card from the PC, plug it back
into the Switch, boot Dread. Atmosphere picks up the sysmodule
automatically.

## What lands on disk (and why there are no `.ips` in the title folder)

A complete install is made of **three** kinds of files, and they do
*not* all live under the game's title folder. This trips people up: a
common report is "I installed to the SD card but I don't see any `.ips`
files." That's expected — on a real Switch the `.ips` files are
deliberately *not* inside `atmosphere/contents/<title-id>/`.

| File(s) | Ryujinx | Real Switch (Atmosphere / SD) |
|---|---|---|
| `subsdk9` + `main.npdm` (the sysmodule) | `mods/contents/<tid>/DreadRandovania/exefs/` | `atmosphere/contents/<tid>/exefs/` |
| Patched `romfs/` (the per-seed overlay) | same `DreadRandovania/romfs/` | `atmosphere/contents/<tid>/romfs/` |
| **Version-sentinel `.ips` patches** | **same `exefs/` folder** | **`atmosphere/exefs_patches/DreadRandovania/`** |

(`<tid>` is Dread's title id, `010093801237c000`.)

Why the split on real hardware: Atmosphere reads exefs IPS patches from
a **global** `atmosphere/exefs_patches/` tree — a *sibling* of
`contents/`, not a child of any title folder. Inside it, the patchset
subfolder name (`DreadRandovania`) is arbitrary; Atmosphere scans every
subfolder and applies any `.ips` whose **filename matches the running
NSO's build id**. There is no title id anywhere in that path — the
build-id filename is the entire targeting mechanism. (Ryujinx is simpler:
it reads IPS from the mod's own `exefs/`, so there they sit alongside
`subsdk9`.)

What the patches do: they inject `Game.HasRandomizerPatches` (a "version
sentinel") into the `main` NSO. open-dread-rando's `custom_scenario.lua`
**rejects a new save with "Unsupported Metroid Dread version" if that
function is missing** — even on a correct 2.1.0 ROM. So these `.ips` are
load-bearing, not optional. They're written by the per-seed auto-patch on
`/connect` (not by `/setup`'s deploy step), into the per-platform
location above. We ship two build-id-named `.ips` under
`apworld/dread/data/exefs_patches/` and re-assert them on every patch
(`patcher_pipeline._install_exefs_ips`), because the vendored
open-dread-rando checkout omits them and wipes the exefs dir each run.

## Troubleshooting

**Wizard says "/setup is deprecated"**: typo - that's the **/patch**
deprecation shim. /setup is the new command; /patch and /patch_python
are kept as deprecation shims pointing at /setup (PR-A3 of the stack).

**Auto-patch skipped on connect**: DreadClient's log will name the
reason. The five "skip" paths all lead back to **re-run /setup** with
a hint at which page to fix:
- `setup_state.json` missing -> never ran /setup
- `romfs_path` not recorded -> wizard didn't finish the RomFS page
- `romfs_path` no longer exists -> you deleted/moved the romfs folder
- `deploy_target` not recorded -> wizard didn't finish the Deploy page
- `slot_data` has no placements -> re-generate the seed with a recent
  apworld version

**"I don't see any `.ips` files on the SD card"**: that's expected if
you looked inside `atmosphere/contents/<tid>/`. On real hardware the
version-sentinel patches live in the **global**
`atmosphere/exefs_patches/DreadRandovania/` tree instead (see "What
lands on disk" above). Check there: you should find two build-id-named
`.ips`. If that folder is empty or missing, the auto-patch didn't run —
when it did, the DreadClient log shows an `installed exefs
version-sentinel patches (...)` note after the `Auto-patch:` line (and a
loud `WARNING: no bundled exefs version-sentinel .ips found` if the
patches were missing). Its absence, or an "SD card not mounted" skip, is
the tell.

**Game boots but rejects a new save as "Unsupported Metroid Dread
version"** (even on a correct 2.1.0 ROM): the version-sentinel `.ips`
didn't land. On real hardware, confirm
`atmosphere/exefs_patches/DreadRandovania/*.ips` exists; on Ryujinx,
confirm the two `.ips` sit in the mod's `exefs/` alongside `subsdk9`.
Re-run the auto-patch by reconnecting with the SD card mounted.

**Patcher fails with `ModuleNotFoundError: open_dread_rando`**: the
Python that the patcher subprocess invokes doesn't have the package.
Re-run /setup's Prereqs page and use the row's Auto-install / pip command.

**Switch can't talk to PC on connect**: that's the wire from the bridge
to the patcher, not relevant to dread. Dread's wire is the opposite
direction: DreadClient (PC) connects to the Switch's :6969 exlaunch
listener. If DreadClient can't dial in, the sysmodule didn't load. On
Ryujinx, the close-and-relaunch reminder from the Done page is the
usual cause.

**Wizard fails partway through Build**: the log box shows the failing
subprocess. Common causes:
- `git not found on PATH` -> install git from
  [git-scm.com](https://git-scm.com/download/win)
- `aarch64-none-elf-g++ not found` -> devkitPro's **Switch-dev** package
  group wasn't installed; re-run the devkitPro installer and re-check
- `msys2/usr/bin/bash.exe` missing -> same as above, but specifically
  the **msys2** component wasn't selected
- `git apply` "patch does not apply" -> the pinned upstream sha got
  force-pushed. Bump `PINNED_EXLAUNCH_COMMIT` in
  `apworld/dread/_setup/build.py` and re-validate.

**Switch sysmodule wedges (Ryujinx)**: Ryujinx's bsd:u service can
get stuck in a bad state if you were running an old/buggy build of the
sysmodule before this one. A full Ryujinx host restart (not just a
game restart) clears it. The shipped patch fixes the underlying bug
(half-open socket handling) so this only matters for users migrating
from an older build.

## Linux

The build pipeline has POSIX branches (`bash -lc "cd <path> &&
./exlaunch.sh build"`) that should work with a system-installed
`devkitpro` package; `detect_ryujinx_path` and `detect_sd_candidates`
short-circuit to None/[] so the user uses the Custom folder deploy
target. End-to-end Linux validation hasn't been done from this repo
yet - if you run /setup on Linux, please open an issue with the
wizard.log so we can correct any rough edges.
