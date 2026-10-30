# Phoenix for Klipper

**User-guided print recovery. Give an interrupted print another chance.**

Phoenix records recent print states and helps an operator prepare, inspect and resume
an interrupted Klipper virtual-SD print. It works through ordinary G-code commands
in Mainsail and Fluidd, with a versioned status interface for a native Voyager experience.

**0.1.0 is an experimental first implementation, not a hardware-validated release.**
Host-side tests pass; real printer validation is still required. No GitHub repository,
remote deployment or printer installation has been performed by creating this project.

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
- Moonraker-visible `phoenix` status and machine-readable event hints for Voyager.
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

## Install on a Klipper host

Copy this directory to the host (later: clone its GitHub repository), then:

```sh
cd ~/phoenix-for-klipper
bash scripts/install.sh
```

For custom paths:

```sh
KLIPPER_DIR=~/klipper CONFIG_DIR=~/printer_data/config bash scripts/install.sh
```

The installer links two modules and copies an example config only if it is absent.
It does not overwrite `printer.cfg`, restart services or enable recovery motion.

1. Merge `[force_move]` with your existing section if present.
2. Add `[include phoenix.cfg]` to `printer.cfg`.
3. Review machine-coordinate `park_x`, `park_y`, `clearance`, speeds and optional
   `home_xy_gcode`. Only after checking your printer, set `profile_ready: True`.
4. Restart Klipper while idle.
5. Run `PHOENIX_STATUS`.

Default recovery does not home XY either: all axes are assumed unchanged. Optional
XY homing can re-establish XY, but its macros must never home/move Z. An existing
`homing_override` may make `G28 X Y` do more than expected. Review it first.

### Slicer / macro integration

```gcode
; After PRINT_START and setup, before printing the model:
PHOENIX_START

; Optional, at each layer, using your slicer's nominal layer-height variable:
; PHOENIX_LAYER Z=<nominal layer Z>

; After the last model extrusion, before PRINT_END / parking / motor disable:
PHOENIX_END
```

Do not insert literal `<nominal layer Z>` into a file. The layer command is optional
metadata; it is not needed for recording actual commanded Z, including hops. Add
START/END once, either in slicer code or print macros, not both. START must execute
inside a virtual-SD job. END waits for outstanding moves before clearing recovery.

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
