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
   - `open-dread-rando` is a pip command. The row shows the exact
     `python -m pip install open-dread-rando` invocation; run it in a
     terminal, then **Re-check**.
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

## Linux and macOS

The build pipeline has POSIX branches that should work with a
system-installed `devkitpro` package:

- `run_exlaunch_build` invokes `bash -lc "cd <checkout> &&
  ./exlaunch.sh build"` directly on non-Windows (no msys2 setup).
- `_devkitpro_gxx_under` probes both the `.exe` and bare paths under
  `<root>/devkitA64/bin/`; `_DEVKITPRO_DEFAULT_ROOTS` includes
  `/opt/devkitpro` and `/usr/local/devkitpro`.
- `check_python312` probes `python3.12` on PATH as a fallback after
  the Windows-only `py -3.12` probe.
- `detect_ryujinx_path` knows the per-platform default Ryujinx data
  roots (Windows: `%APPDATA%/Ryujinx/`; Linux:
  `$XDG_CONFIG_HOME/Ryujinx/` or `~/.config/Ryujinx/`; macOS:
  `~/Library/Application Support/Ryujinx/`).
- `detect_sd_candidates` is Windows-only (walks drive letters). On
  Linux/macOS the Custom folder deploy target is the right choice —
  point it at your SD mount path (e.g. `/media/<user>/SWITCH-SD`).

End-to-end validation on Linux and macOS has not been run from this
repo yet — if you run /setup on either platform, please open an issue
with the wizard.log (`~/.local/share/dread_ap/wizard.log`) so we can
correct any rough edges. The most likely friction points are:

- devkitPro install: pacman repos on Arch, the official installer
  script on Debian/Ubuntu (`pacman -S switch-dev` after setting up
  devkitpro-pacman). Confirm `$DEVKITPRO` is set correctly in the
  environment the AP Launcher inherits.
- Python 3.12 install: `apt install python3.12` / `brew install
  python@3.12`; the row's Auto-install button is a Windows-only
  (winget) path so it stays grey on POSIX.
- open-dread-rando pip install: same as Windows; the row shows the
  exact `pip install` command for your Python.
