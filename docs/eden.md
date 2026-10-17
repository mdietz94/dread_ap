# Eden (experimental)

This target deploys the existing Dread bridge to Eden's mod directory and
adapts its process metadata for Eden 0.2.1. Real Switch, Ryujinx and Custom
targets retain their existing paths and unmodified module metadata.

## Desktop setup

1. Install Eden from its [official downloads](https://eden-emu.dev/downloads/).
   Set up your own game, keys, firmware and Dread **2.1.0 update**, then confirm
   vanilla Dread boots. Leave additional executable mods disabled initially.
2. Install this APWorld in Archipelago and open **Dread Client**. Complete the
   existing Python/patcher prerequisites and select an extracted, vanilla,
   merged **2.1.0 RomFS** (containing `system/` and `packs/`).
3. On the Deploy page, select **Eden (experimental)**. Browse to the directory
   shown by Eden's **File > Open Eden Folder**, including for portable installs.
   Select the data folder itself, not its `load` subfolder or the executable.
4. Set Bridge IP to the client computer's reachable LAN IPv4 address. The game
   tries loopback discovery first, then sweeps this address's `/24` subnet.
5. Deploy and connect Dread Client to your AP server/slot. Wait for automatic
   patching to finish. Enable **DreadRandovania** in Eden and restart Dread
   completely so it loads the new mod.

Output:

```text
<Eden data>/load/010093801237c000/DreadRandovania/
    exefs/     (module, adapted main.npdm, executable IPS patches)
    romfs/     (seed overlay and ap_config.json)
```

State uses `deploy_target: "eden"` and `eden_root: "<Eden data>"` in
`%APPDATA%/dread_ap/setup_state.json` on Windows, or
`~/.local/share/dread_ap/setup_state.json` on macOS/Linux. Automatic patching
uses the patcher's existing Ryujinx-compatible layout; no directory links or
Ryujinx installation are needed.

Dread Client remains a separate desktop application. Start it through the
Archipelago Launcher; it provides the AP connection UI and the Dread status/log
tab. Keep one client running. Game-to-client traffic uses TCP 17777 and UDP
17776; the AP server address entered in the UI is a separate connection.
For a handheld on the same LAN, configure the bridge for the client computer
and transfer the generated mod separately. This target does not install to
Android over USB or synchronize handheld mods automatically.

## Metadata correction

The tested bundled NPDM contains DebugFlags descriptor `0x0008ffff` in both
ACI0 and ACID. Eden 0.2.1 rejects reserved bit 19. For the Eden destination
only, deployment clears it to `0x0000ffff`; automatic patching reapplies the
same correction after the upstream patcher writes its output.

The converter validates section/capability boundaries and the expected
descriptor, accepts already corrected input, and rejects unfamiliar metadata.
It never edits the bundled binary or shared prebuilt/source file. Selecting
another target later therefore still deploys the original metadata.

## Android startup memory limitation

**The Eden target alone is not sufficient on all Android devices.** A Retroid
Pocket Nova test needed an additional, manual executable-prepatch workaround.
That workaround is not implemented by this PR.

Android recorded three modded launches as LOW_MEMORY exits; one had about
7.9 GB RSS. Eden 0.2.1's loader builds an NSO image including Dread main's
1,892,495,264-byte BSS, then makes further whole-image copies while applying
IPS patches. This is a likely explanation for the startup spike, not an
instrumented allocation trace. See Eden's
[NSO loader](https://git.eden-emu.dev/eden-emu/eden/src/tag/v0.2.1/src/core/loader/nso.cpp)
and [patch manager](https://git.eden-emu.dev/eden-emu/eden/src/tag/v0.2.1/src/core/file_sys/patch_manager.cpp).

In the test, applying the five existing IPS32 records to the user's extracted
2.1.0 `main` ahead of time, recompressing it with updated section hashes, and
using it as an ExeFS replacement avoided the failure. The original IPS files
were moved outside the active mod directory. The unchanged BSS and build ID
were retained, and hactool verified the replacement's three section hashes.
The replacement contains game bytes and must not be distributed or committed.
Additional 4:3/60 FPS mods were disabled for the successful test.

**Automatic patching recreates the standard IPS layout.** It does not preserve
a manually prepatched Android installation: reapply any private workaround
before transferring fresh output. A loader-side fix in Eden or a separate,
validated local prepatch feature should be considered independently.

## Test report and remaining validation

The local compatibility workarounds were tested on October 17, 2026 with
Dread AP 0.27.0, Archipelago 0.6.8, and Dread 2.1.0. The new wizard selection
and complete release built from this PR still need manual acceptance testing.

| Configuration | Evidence |
| --- | --- |
| macOS, M4 / 24 GB, Eden stable 0.2.1 | Randomizer UI, gameplay around 60 FPS, bridge and Lua bootstrap with local metadata correction |
| Retroid Pocket Nova, QCS8550, Android 13 / API 33, about 11 GB usable RAM | Boot, Wi-Fi bridge, two Nav hints, incoming item, physical check, AP reconnect, save/restart using metadata correction **and private prepatched main** |
| Windows Eden | Not manually tested |
| Real Switch and Ryujinx with this PR | Manual regression testing pending; automated tests verify unchanged deployment bytes and target-switch isolation |

For the Retroid test, the client/server ran on the Mac. The Android runtime
identified itself as `v0.2.1-v0.2.1`; an exact APK revision was not established.
A server-injected Missile Tank changed capacity 30 → 32 and received cursor
0 → 1. AP reconnect did not duplicate it. A physical Waterfall pickup increased
capacity to 34 and the server to 1/149 checks. After saving and restarting
Eden, the bridge reconnected, save/patch hashes matched, capacity stayed 34,
received cursor stayed 1, and checks stayed 1.

Cross-slot multiplayer, offline collection recovery, DeathLink, goal completion
and full-playthrough stability were not tested. Some poll timeouts recovered
during gameplay. Initial manual key-format/file-permission mistakes were
corrected during setup and are not established upstream bugs.

Before removing the experimental label, test the packaged APWorld through the
wizard on Windows/macOS, then smoke-test a real Switch and Ryujinx with the
same seed: boot, one remote item, one location, and save/reconnect. Automated
metadata/path tests are not a substitute for those platform checks.
