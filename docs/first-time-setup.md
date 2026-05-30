# First-time setup walkthrough

This is the click-by-click walkthrough for setting up DreadClient on a
brand-new machine. For the architectural backgrounder, see
[install-switch.md](install-switch.md).

## Time budget

- **First run total**: 15-25 minutes, of which most is waiting on
  downloads (devkitPro: ~700 MB, Python 3.12: ~25 MB, exlaunch git
  clone: ~30 MB).
- **Subsequent runs** (re-deploy to a different target, etc.): 30
  seconds. The wizard skips a no-op rebuild when the outputs are
  already on disk.

## What you need before starting

1. A modded Switch on Atmosphere CFW, OR Ryujinx installed on the PC.
2. A copy of Metroid Dread 2.1.0. Real cartridge or a clean digital dump.
3. An extracted vanilla Dread 2.1.0 romfs on the PC. If you haven't
   extracted yet:
   - Dump from a clean retail source with
     [NXDumpTool](https://github.com/DarkMatterCore/nxdumptool/releases)
     (Switch homebrew that produces a clean NSP/XCI).
   - Extract with [hactool](https://github.com/SciresM/hactool) or
     [nxgameuncomp](https://github.com/CompSciOrBust/nxgameuncomp)
     into a folder. The folder should contain `system/` and `packs/`
     subdirs.
4. Archipelago installed from
   [archipelago.gg](https://archipelago.gg/tutorial/Archipelago/setup/en).
5. The dread `.apworld` in `<AP-install>/custom_worlds/`. Either:
   - Download a release `.apworld` from this repo, or
   - From a source checkout: `python scripts/install_apworld.py
     <AP-install>/custom_worlds/`

## The walkthrough

### 1. Launch DreadClient for the first time

Open the Archipelago Launcher. Click the **Dread Client** button.

A Kivy window pops with the wizard already running on the **Welcome**
page (PR-A4 of the setup-wizard stack triggers this on first run; if
the setup state ever gets cleared the wizard pops again).

The Welcome page lists the requirements. Confirm them, then click
**Begin**.

### 2. Prereqs page

The wizard runs three detectors and shows the results:

- **devkitPro / devkitA64** - the cross-compiler that builds the
  Switch sysmodule. If it's not installed, click the **Install...**
  link to open
  [devkitpro.org/wiki/Getting_Started](https://devkitpro.org/wiki/Getting_Started).
  Install the **Switch-dev** package group (this also installs the
  bundled msys2 bash the wizard needs). Then click **Re-check**.

- **Python 3.12** - the patcher needs 3.12 specifically because
  `mercury_engine_data_structures` has no 3.13 wheel. If it's
  missing, click **Auto-install** (runs `winget install
  Python.Python.3.12`) and accept the UAC prompt. The wizard
  re-checks automatically.

- **open-dread-rando (Python patcher)** - the pip package the
  patcher runs as a subprocess. The row's `note` shows the exact
  pip command (`<python> -m pip install open-dread-rando`). Run
  it in PowerShell, then **Re-check**.

When all three rows are green, click **Next**.

### 3. RomFS picker page

Click **Browse...**. A native folder dialog pops. Navigate to your
extracted Dread 2.1.0 romfs folder (the one with `system/` and
`packs/` subdirs). Click **Select Folder**.

The wizard validates that the folder looks like a romfs (warns but
doesn't block if `system/` or `packs/` are missing - some extractors
produce slightly different layouts). The path is persisted to
`%APPDATA%/dread_ap/setup_state.json` as `romfs_path`.

Click **Next**.

### 4. Build page

The wizard runs three subprocess steps live in the log box:

1. **Cloning open-dread-rando-exlaunch** - the first time, this is
   a `git clone` (~30 MB download). Re-runs do `git fetch + reset
   --hard <pinned-sha>` instead.
2. **Applying the Ryujinx-fix patch** - idempotent (we probe for a
   sentinel string in `remote_api.cpp` so re-runs skip).
3. **Compiling the subsdk9 sysmodule** - `./exlaunch.sh build` under
   devkitPro's msys2 bash. ~30-60 seconds on a warm cache.

When the page status shows "Build complete: subsdk9 (NNNN bytes),
main.npdm (NNN bytes).", click **Next**.

If the build fails, the Retry button reappears. The status line names
the failing step; cross-reference the
[troubleshooting section in install-switch.md](install-switch.md#troubleshooting).

### 5. Deploy page

Three radio buttons:

- **Ryujinx (emulator)** - auto-detected to `%APPDATA%/Ryujinx/`.
  Click the radio if Ryujinx is your target.
- **Real Switch (SD card)** - auto-detected by walking A-Z drive
  letters and checking for an `atmosphere/` directory at the root.
  If your SD card is plugged in via a USB reader, it should appear
  here. If not, click **Re-detect**.
- **Custom folder** - for users who manage SD sync themselves (UMS,
  DBI / Goldleaf transfer, staging on a network share). Click
  **Browse...** to pick a folder. Files land at
  `<your-folder>/atmosphere/contents/010093801237c000/exefs/` (the
  same layout the SD-card deploy produces), so you can drop the
  whole subtree onto an SD card later.

Click **Next**. The deploy step copies subsdk9 + main.npdm to the
chosen destination.

### 6. Done page

"Installation successful." plus a target-specific "what to do next"
note:

- **Ryujinx**: close Dread completely (back to the game list, not
  just the title screen) and relaunch it. Ryujinx applies exefs
  mods at process start, so the new subsdk9 only loads on a fresh
  boot of the game.
- **Real Switch**: eject the SD card, plug it back into the Switch,
  boot Dread.
- **Custom folder**: copy the `atmosphere/...` subtree to your SD's
  root, then proceed as for Real Switch.

Click **Close**. The wizard exits; DreadClient is still up in the
other window.

### 7. Connect to AP

In DreadClient, type or paste:

```
/connect ap.gg:38281 YourSlotName
```

(or use the Connect bar). The Archipelago tab logs:

```
Auto-patch: writing per-seed romfs overlay to
  C:\Users\you\AppData\Roaming\Ryujinx\mods\contents\010093801237c000\DreadRandovania
  (vanilla romfs: C:\Users\you\Downloads\dread-romfs)
```

The per-seed patcher runs in a worker thread (~3 seconds). When it's
done, you can launch Dread.

### 8. Play

**Ryujinx**: in the Ryujinx window, close Dread and relaunch it. The
seed should load; collected pickups appear in DreadClient's log as
they happen.

**Real Switch**: boot Dread on the Switch. Same flow.

## When to re-run /setup

You'll need to re-run the wizard if:

- You switch deploy targets (Ryujinx -> Real Switch, or vice versa).
- You move the extracted romfs folder.
- You update the apworld to a newer version.
- The subsdk9 sysmodule wedges and the troubleshooting section says
  to rebuild from scratch.

You DO NOT need to re-run /setup for:

- A new AP seed (the per-seed patcher runs automatically on connect).
- A new AP server / slot (those are Connect bar fields).
- Restarting DreadClient (state is in `%APPDATA%/dread_ap/`).

## What lives where

| Path | What |
|---|---|
| `%APPDATA%/dread_ap/build/exlaunch-checkout/` | The cloned upstream repo |
| `%APPDATA%/dread_ap/build/exlaunch-checkout/src/open_dread_rando_exlaunch/deploy/` | subsdk9 + main.npdm before deploy |
| `%APPDATA%/dread_ap/setup_state.json` | The wizard's persistent state |
| `%APPDATA%/dread_ap/wizard.log` | Wizard breadcrumb log (page transitions, populate() crashes) |
| `%APPDATA%/dread_ap/launch-crash.log` | Tk-surfaced launch crashes (when launching from a .pythonw-style ArchipelagoLauncher.exe) |
| `%APPDATA%/Ryujinx/mods/contents/010093801237c000/DreadRandovania/exefs/` | Ryujinx deploy target |
| `<SD>:\atmosphere\contents\010093801237c000\exefs\` | Real-Switch deploy target |

If you ever need to start fresh, delete `%APPDATA%/dread_ap/` and re-run
DreadClient. The wizard pops automatically (the first-run gate detects
the missing state).
