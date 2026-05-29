# End-to-end runbook — Dread + AP, first multiworld session

Walks a user from "fresh Switch dump + Dread ROM" to "first multiworld
gameplay session" with the dread_ap apworld. The dev who wrote this
likely doesn't have a Switch — the steps were validated in unit tests
and against Ryujinx where possible; real-hardware steps are documented
exhaustively enough that someone with the Switch can execute them
without asking questions.

If something doesn't work, see "Common failure modes" at the bottom.

## Prerequisites (one-time)

- **Switch hardware** running Atmosphere CFW. Sysmodule support required
  (Atmosphere ≥ 1.6.0 ships it). The user's microSD is mounted at
  `D:\switch\` in this project's CLAUDE.md.
- **prod.keys** at `C:\Users\maxwe\.switch\prod.keys` (or wherever your
  toolchain expects it).
- **A Metroid Dread 2.1.0 dump** — the NSP/XCI you legally extracted from
  your own console. Not stored anywhere in this repo.
- **Ryujinx** at `C:\Users\maxwe\AppData\Roaming\Ryujinx\` for dev
  iteration before deploying to hardware.
- **Python 3.13** on PATH with the dread_ap repo's deps installed.
- **Archipelago checkout** at `../smo_archipelago/vendor/Archipelago/`
  (the dread_ap project doesn't ship its own; reuses the sibling's).
- **open-dread-rando-exlaunch sysmodule** installed on the Switch — see
  [vendor/open-dread-rando-exlaunch/README](../vendor/open-dread-rando-exlaunch/)
  for the build + install steps. After install, exlaunch listens on
  TCP port 6969 once Dread launches.
- **The Dread Client (this repo's PC-side process)** built into your
  Archipelago install — see step 0 below.

## Step 0 — Install the apworld into Archipelago

```pwsh
cd C:\Users\maxwe\Documents\dread_ap
python scripts\install_apworld.py
```

Expected output:

```
Installed apworld at <archipelago-root>\custom_worlds\dread_archipelago.apworld
```

After this, Archipelago's Generate.py sees Metroid Dread as a known game.

## Step 1 — Smoke-test the Switch wire (recommended before generating)

Before spending time on multiworld generation, confirm the Switch is
reachable and exlaunch is responding. With Dread running on the Switch
(or Ryujinx) at the title screen or in-game:

```pwsh
python scripts\phase1_validate.py <switch-ip>
```

Expected output, in order:

```
[phase1] connecting to <ip>:6969
[phase1] connected; sending PACKET_HANDSHAKE(interests=MULTIWORLD)
[phase1] handshake ack received
[phase1] T1: bare Lua eval (does the runtime answer at all?)
  T1 success=True payload=b'2'
[phase1] T2: does the Randovania `RL` namespace exist?
  T2 success=True payload=b'table'
[phase1] T3: Randovania-style API-version handshake
  T3 success=True payload=b'1,4096,true,<uuid>,2.1.0'
    api_version  = 1
    buffer_size  = 4096
    bootstrap    = true
    layout_uuid  = <uuid>
    game_version = 2.1.0
[phase1] T4: read current inventory bitfield (RL.GetInventoryAndSend)
  T4 success=True payload_bytes=0
    (RL.GetInventoryAndSend issues a separate PACKET_NEW_INVENTORY message)
[phase1] OK — wire is up. Proceed to Phase 2.
```

A non-zero exit status means **stop here** and triage with the failure
table below. The rest of the pipeline depends on this working.

## Step 2 — Generate the AP seed

For a Dread-only smoke seed:

```pwsh
python scripts\ap_generate.py `
  --player_files_path apworld\dread_archipelago\tests\seeds\dread_smoke.yaml `
  --outputpath apworld\dread_archipelago\tests\seeds\out
```

For the 2-slot Dread + Clique multiworld smoke:

```pwsh
python scripts\ap_generate.py `
  --player_files_path apworld\dread_archipelago\tests\seeds\dread_clique.yaml `
  --outputpath apworld\dread_archipelago\tests\seeds\out
```

Expected output (abbreviated):

```
...
wrote .../apworld/dread_archipelago/tests/seeds/out/AP_<seed-id>.zip
```

The zip contains:

- `AP_<id>.archipelago` — the multidata file the AP server consumes.
- `AP_<id>_Spoiler.txt` — human-readable placements.
- `AP_<id>_P<n>_Dread_<slot>.json` — the Dread per-slot placements,
  written by `DreadWorld.generate_output`. One per Dread slot.

## Step 3 — Convert the Dread slot to patcher overrides

```pwsh
python scripts\seed_to_patcher_overrides.py `
  apworld\dread_archipelago\tests\seeds\out\AP_<seed-id>.zip `
  --slot Samus `
  --output build\dread_overrides.json
```

Expected output:

```
wrote build/dread_overrides.json (NNNN bytes)
  pickups: ~137 resource overrides, M cross-slot captions
```

For the Dread-only seed `M == 0` (no cross-slot items). For
`dread_clique.yaml` you should see at least one cross-slot caption
(some Clique item placed at a Dread location).

## Step 4 — Merge with the patcher template

```pwsh
python scripts\build_patcher_json.py `
  --template vendor\open-dread-rando\tests\test_files\patcher_files\starter_preset_patcher.json `
  --ap-overrides build\dread_overrides.json `
  --output build\dread_patcher_input.json
```

Expected output:

```
wrote build/dread_patcher_input.json (NNNNN bytes)
```

Sanity-check the output validates against the patcher schema:

```pwsh
python -c "import json, jsonschema; jsonschema.validate(json.load(open('build/dread_patcher_input.json')), json.load(open('vendor/open-dread-rando/src/open_dread_rando/files/schema.json')))"
```

If this raises, the JSON shape drifted from what the patcher expects —
see the failure table below.

## Step 5 — Run the patcher to produce a RomFS

Requires an extracted Dread 2.1.0 RomFS at a known path (e.g.
`D:\dread\romfs`).

```pwsh
python -m open_dread_rando `
  --input-path D:\dread\romfs `
  --output-path build\dread_seed `
  --input-json build\dread_patcher_input.json
```

Expected output: a `build\dread_seed\` directory mirroring the structure
of `D:\dread\romfs\` with patched files.

## Step 6 — Deploy to the Switch

Copy `build\dread_seed\*` to the Switch SD card's

```
/atmosphere/contents/010093801237C000/romfs/
```

(`010093801237C000` is the Dread title ID for both region releases.)

For Ryujinx, point its "Mods Directory" at `build\dread_seed\romfs`
under the Dread mod folder, or symlink it.

## Step 7 — Start the AP server

In a separate terminal:

```pwsh
python scripts\ap_server.py apworld\dread_archipelago\tests\seeds\out\AP_<seed-id>.archipelago
```

Default port is 38281.

## Step 8 — Start the Dread Client

```pwsh
python -m worlds.dread_archipelago.client.main `
  --connect localhost:38281 `
  --name Samus `
  --switch-host <switch-ip>
```

The headless client logs to stdout. It will:

1. Connect to AP server at `localhost:38281` as slot `Samus`.
2. Dial the Switch at `<switch-ip>:6969`.
3. Pull initial state.
4. Start a 2-second poll loop.

## Step 9 — Play

Boot Dread on the Switch. Once you're in-game, the client should:

- Forward AP-received items as `RL.ReceivePickup` calls, popping the
  vanilla Dread "Item acquired" UI.
- Watch the Switch's `PACKET_COLLECTED_INDICES` push and forward each
  newly-set pickup_index as a `LocationChecks` to the AP server.
- Report `ClientStatus.CLIENT_GOAL` when `Init.bBeatenSinceLastReboot`
  flips true (final cutscene).

### Smoke checklist

Quick proof the wire is actually working:

- [ ] Walk to the first Charge Beam pedestal in Artaria. Collect it.
      Within ~2s the client log should print `forwarding 1 collected
      location(s) to AP`.
- [ ] In the other slot (e.g. Clique), check whatever your spoiler
      says contains a Dread item. The Dread player should see an item
      popup with the AP-forwarded item.
- [ ] If you generated `dread_clique.yaml`, have ButtonPusher click
      their button. The Dread player should get the corresponding popup.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `phase1_validate.py` connect refused | exlaunch sysmodule not loaded | Install the sysmodule per `vendor/open-dread-rando-exlaunch/README`; confirm `/atmosphere/contents/0100000000000352/` exists on SD |
| `phase1_validate.py` T2 says `RL` is not a table | Dread isn't running the open-dread-rando patcher output | Run step 5 to produce a RomFS, deploy it (step 6), restart Dread |
| `seed_to_patcher_overrides.py` says "no Dread placements JSON" | The seed wasn't generated with `DreadWorld.generate_output` (older apworld), or wrong --slot name | Check `--slot` matches a `name:` in the YAML; verify the apworld is the current zipped build |
| `build_patcher_json.py` says "pickup keys that don't exist in template" | Seed and template disagree on the pickup set | Likely an apworld vs template version skew — re-run extract_dread_data.py |
| `jsonschema.validate` fails on patcher input | `layout_uuid` regex mismatch or starting_location wrong | Check the regex in vendor/open-dread-rando/.../schema.json; the converter derives a UUID via sha256 — should always match |
| Client connects but no items flow | Slot name mismatch | Confirm `--name Samus` matches the `name:` in the YAML AND the AP server logs say "connected" for that slot |
| Items flow PC→Switch but checks don't flow Switch→PC | bootstrap Lua not running, or `RL.GetCollectedIndicesAndSend()` not subscribed | Reinstall RomFS; verify `RL.Bootstrap` is `true` in the API probe output |
| Cross-slot popup shows wrong item | The placeholder caption is built from `ap_item_name` in the placements JSON — verify it's not blank | Look at the `dread_overrides.json` `pickup_captions` section |

## What's NOT in this milestone

- **Kivy GUI**: this is a separate later milestone. The headless client
  works but lacks the in-app `/dread_status`, `/poke`, command UI.
- **Cross-region access rules**: the apworld currently uses a star
  topology in `Regions.py`. Tightening to `accessibility: items` is M2
  Gate B work (see `docs/randovania-logic-port-m2plumbing.md`).
- **Progressive beams / suits**: each item is currently a flat unlock.
- **Hint distribution and deathlink**: post-v0.1.
- **Multi-Switch routing**: one client connects to one Switch. (smo_archipelago
  supports multiple; we don't yet.)

## Repro from a clean checkout

For someone validating the pipeline who just cloned the repo:

```pwsh
git clone <repo> dread_ap
cd dread_ap
python -m pytest apworld/dread_archipelago/tests/ scripts/tests/ -q
# Expect: NNN passed
python scripts/install_apworld.py
# Skip steps 1, 6, 8, 9 if no Switch — generation + conversion can be
# verified without hardware.
python scripts/ap_generate.py --player_files_path apworld/dread_archipelago/tests/seeds/dread_clique.yaml --outputpath apworld/dread_archipelago/tests/seeds/out
python scripts/seed_to_patcher_overrides.py apworld/dread_archipelago/tests/seeds/out/AP_*.zip --slot Samus --output build/test_overrides.json
python scripts/build_patcher_json.py --template vendor/open-dread-rando/tests/test_files/patcher_files/starter_preset_patcher.json --ap-overrides build/test_overrides.json --output build/test_input.json
```
