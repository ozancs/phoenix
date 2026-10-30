# Phoenix for Klipper

**User-guided print recovery. Give an interrupted print another chance.**

Phoenix records recent print states and helps an operator prepare, inspect and resume
an interrupted Klipper virtual-SD print. It works through ordinary G-code commands
in Mainsail and Fluidd, with a versioned status interface for a native Voyager experience.

**0.3.2 is experimental, not a hardware-validated release.**
Host-side tests pass; real printer validation is still required. A GitHub repository is available; no real printer validation has been performed here.

## Design decisions

- No Z homing, probing, QGL or Z tilt adjustment during recovery.
- Assume axes and build plate did not move after interruption; the operator confirms.
- Z-hop remains supported. A cut during a hop may leave a different Z than the record.
- Saved positions are commanded positions, **not a measurement of where power failed**.
- Selecting an older checkpoint never redefines the current position or adds model offsets.
- No automatic heat, homing, motion or resume at startup.
- No UPS or additional hardware required. UPS support is a future optional extension.
- No original-file rewriting and no repeated `PRINT_START` when resuming.

## What is implemented

- A Klipper `[phoenix]` module, periodic boundary sampling, file SHA-256 validation.
- A bounded checkpoint history with XYZ/E modes, offsets, temperatures, fan speeds,
  pressure advance, motion limits and bed mesh identity.
- Two checksummed, atomically replaced journal generations; writes run in a worker
  thread rather than performing disk fsync in the sampling timer.
- Explicit checklist, checkpoint selection, lift/park, cleaning pause, reheat and return.
- A durable recovery fence: a restart during preparation cannot silently reuse the old Z.
- Standard `action:prompt_*` checklist for clients supporting that protocol; console
  commands work independently of prompt support.
- Moonraker-visible `phoenix` status for Voyager, without machine-event noise in the console.
- Voyager client helpers, a theme-inheriting animated guide component and integration brief.

The native Voyager wizard is **not installed or integrated** yet. See
[the Claude handoff](docs/VOYAGER-CLAUDE-HANDOFF.md). The existing Voyager repository
was inspected but not modified.

## Initial scope

Cartesian / CoreXY, one extruder, ordinary virtual-SD G-code, conventional slicer
retractions and Z-hop. Named bed mesh profiles must already be saved persistently
and reload to the exact same mesh. Adaptive/unsaved meshes generally cannot be
recovered in 0.1. Runtime mesh offsets, skew correction, thermal Z adjustment,
bed tilt transforms, extra motion axes, dual carriages and active object exclusion
are not supported. Firmware-retracted states are skipped when sampling.

Arbitrary custom macro state, multi-material tool state, manual feeder state and
all possible third-party transforms cannot be reconstructed. Do not assume support
for a custom configuration simply because it loads. Start with the documented scope.
Sequential object printing can place a taller obstacle in the parking route; collision
checking is operator/profile based, not a complete model geometry analysis.

## Guided installation

For a public repository / an already authenticated clone:

```sh
cd ~
git clone https://github.com/ozancs/phoenix.git phoenix-for-klipper
cd ~/phoenix-for-klipper
bash scripts/install.sh
```

**Private repository?** Follow [the deploy-key bootstrap](docs/PRIVATE-REPO.md) first.
A read-only SSH deploy key is enough; no GitHub token in printer configuration.

The installer shows its changes and asks to apply them. It automatically:

- Traverses printer.cfg includes (including globs) and checks existing configuration.
- Installs module links, adds the missing include, and merges an existing force_move section.
- Adds PHOENIX_START at the end of PRINT_START/START_PRINT and PHOENIX_END at the
  beginning of PRINT_END/END_PRINT, when the choice is unambiguous.
- Adds Phoenix to Moonraker Update Manager, using the clone's remote and dev channel.
- Saves originals and a manifest under printer_data/phoenix-setup-backups/ before edits.
- Offers to restart only when Moonraker confirms the printer is idle. Unknown/paused/
  printing states do not trigger a restart. With --yes, restart requires --restart.

Rerun it safely to add missing parts; it preserves existing park settings. Preview only:

```sh
bash scripts/install.sh --check
```

Custom instance / macro names:

```sh
bash scripts/install.sh --config-dir ~/printer_2_data/config --moonraker-url http://127.0.0.1:7126 \
  --start-macro MY_PRINT_START --end-macro MY_PRINT_END
```

If macros are absent or ambiguous, setup tells you exactly what remains. It cannot
edit a slicer on another computer. In that case add PHOENIX_START after initialization,
PHOENIX_END before end/park/heater-off actions, and reslice. Existing files are not rewritten.
Do not run install during a print. Configs outside the selected instance, duplicate
sections, missing includes or uncertain inline macro bodies stop the automated edit.

After reconnect, open **PHOENIX_SETUP_GUIDE** from the macro list, or run:

```gcode
PHOENIX_SETUP
```

The guide reports loaded module, configuration compatibility, print hooks and park
profile readiness. On an empty bed, use normal controls to position XY at a clear
park point, then explicitly accept that point and the route. The guide requests
homing before offering jog/save controls. Normal homing is an explicit empty-bed
setup action; recovery still never homes Z. XY jog buttons lift for clearance first.
Use **Save park and finish** to close the guide. For typed machine coordinates:

```gcode
; Example only: use your own reviewed park coordinates and clearance.
PHOENIX_SETUP_PROFILE X=10 Y=10 CLEARANCE=5 CONFIRM=1
```

The reviewed profile is saved in Phoenix's own checksummed setup-profile.json and
applies immediately. No SAVE_CONFIG or second restart is needed. It overrides the
example config's unreviewed park defaults. A changed printer configuration invalidates
that review. If you use slicer-only hooks, the guide lets you acknowledge adding them;
that acknowledgement is not a claim that Phoenix inspected your slicer.

Optional nominal layer metadata, using your slicer's actual placeholder:

```gcode
; PHOENIX_LAYER Z=<nominal layer Z>
```

Z-hop remains supported without this optional command. After setup, follow
[the first-printer test plan](docs/TESTING.md).

### Everyday use and demo

After setup, print normally. The installed print hooks start and stop recording;
you do not manually run CHECK, START, END, PREPARE or RESUME. Open PHOENIX_RECOVERY
only when an interrupted print is available. PHOENIX_SETUP_GUIDE is for initial
setup or changing the park point. Low-level commands remain for automation/API
compatibility, not as a list of routine tasks.

To see the flow on an empty bed, run **PHOENIX_DEMO**. It chooses bounded XYZ points,
parks, simulates heating without enabling heaters, signals with three part-fan
pulses and returns to the same point without extrusion. See [demo guide](docs/DEMO.md).

### Recovery

Run `PHOENIX_RECOVERY` / `PHOENIX_PROMPT` for the fallback checklist. The independent
console flow is:

```gcode
PHOENIX_STATUS
; Optional: 0 is newest, 1 is the preceding sample, etc.
PHOENIX_SELECT INDEX=0

; Set each to 1 only after checking it. VALUE=0 withdraws confirmation.
PHOENIX_CHECK ITEM=bed_attached VALUE=1
PHOENIX_CHECK ITEM=axes_unmoved VALUE=1
PHOENIX_CHECK ITEM=plate_unmoved VALUE=1
PHOENIX_CHECK ITEM=nozzle_free VALUE=1
PHOENIX_CHECK ITEM=path_clear VALUE=1
PHOENIX_CHECK ITEM=uncertainty_accepted VALUE=1
PHOENIX_PREPARE

; Now parked: clean excess plastic and prime using normal heater/extruder controls.
; Do not jog XYZ, disable motors, change mesh or home Z.
PHOENIX_RESUME CLEANED=1 PATH_CLEAR=1
```

If the nozzle is frozen into the part, do not confirm `nozzle_free` yet. Use the
printer's normal temperature controls under supervision to free it first. Phoenix
does not automatically heat a nozzle sitting against the part. Purging is currently
manual at the park position; RESUME restores saved targets and waits for heat.

To forget an interrupted job, use `PHOENIX_DISCARD CONFIRM=1`. This forgets recovery
records; it does not move axes or turn off heaters. Use normal heater controls as
needed. A partially prepared recovery is blocked after reboot and must be reviewed
manually; there is no automatic second recovery of the preparation stage.

## Accuracy and persistence limits

Sampling happens only between complete virtual-SD commands while the G-code mutex
is free. An offset and its interpreter state therefore belong to the same boundary.
This does **not** imply that the MCU completed all queued moves at that boundary.
Long macros and commands can create gaps longer than `save_interval`; the configured
interval is a target, not a maximum uncertainty bound. A worker backlog or power loss
before durable write can lose the newest sample. There is no motion-by-motion log.

Z-hop height is recorded as part of commanded Z; optional nominal layer Z makes the
apparent lift visible. Hop completion at power loss is not measured. Wrong assumed Z
can shift the remaining print vertically. XY homing can help XY but does not solve Z.
An older checkpoint may repeat deposited plastic; a later boundary may skip plastic.
There is no exact mid-line interpolation or arbitrary line selection in this version.

Atomic rename, checksums and fsync reduce file corruption risk; they do not guarantee
that every SD card/controller survives a power cut with its latest data intact.

## Development

```sh
python -m unittest discover -s tests -v
node --test tests/test_client.mjs
```

Python requires no third-party packages. The Vue component is a handoff component,
not a standalone app. See [testing](docs/TESTING.md), [API](docs/API.md),
[architecture](docs/ARCHITECTURE.md) and [Voyager requirements](docs/VOYAGER-CLAUDE-HANDOFF.md).

## References and license

Integration references: [Klipper virtual SD](https://github.com/Klipper3d/klipper/blob/master/klippy/extras/virtual_sdcard.py),
[G-code state](https://github.com/Klipper3d/klipper/blob/master/klippy/extras/gcode_move.py),
[coordinate systems](https://www.klipper3d.org/Code_Overview.html#coordinate-systems).

GPL-3.0-or-later. See [LICENSE](LICENSE).
