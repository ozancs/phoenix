# Setup guide (0.2)

There are two parts: host installation and a printer-side review. Neither moves or
heats the printer. The installer changes configuration only after displaying its
plan and obtaining a terminal confirmation (or explicit --yes).

## Host installation

`bash scripts/install.sh` performs include traversal, force_move merge, print hook
insertion, module links and Update Manager integration. Config diffs are previewed;
all edited originals are backed up with exact bytes and a manifest. A failed config
write rolls back completed writes. If power fails between writes, use the printed
backup directory/manifest to restore originals before restarting; this is not a
multi-file filesystem transaction. Backups are outside the config include tree so
wildcards cannot accidentally load them.

- Existing hooks/includes are not duplicated on rerun.
- User park values are not overwritten.
- Existing main-file SAVE_CONFIG blocks remain byte-identical and at the end.
- A changed file between preview/apply stops setup.
- Shared configuration outside the selected instance is not automatically rewritten.
- Unknown print status allows configuration preparation but never automatic restart.
- Running or paused prints stop application. A fresh idle check precedes restart.
- --check never changes configuration, module links, Git settings or services.
- --yes applies without asking; it does not imply --restart.
- MOONRAKER_API_KEY may be provided as an environment variable if local API access
  needs authentication. It is not written into any configuration or console output.

Options: --config-dir, --klipper-dir, --moonraker-url, --start-macro, --end-macro,
--check, --yes, --restart, --deploy-key. Existing CONFIG_DIR / KLIPPER_DIR environment
variables remain supported. See PRIVATE-REPO.md for deploy-key setup.

Automatic macro choices: PRINT_START or START_PRINT; PRINT_END or END_PRINT. If both
aliases exist, select explicitly. Hook insertion is at the top level of the gcode
body, outside Jinja conditionals: START at the end, END at the beginning. Review the
preview for macros which schedule deferred/asynchronous work; textual position
cannot prove all external initialization has completed. Custom macros can be named
with the command-line options. No standard macro means the guide provides slicer
instructions instead of claiming automatic integration. Already-sliced files only
benefit if they call the modified macros.

## Printer-side guide

`PHOENIX_SETUP_GUIDE` is a visible macro opening `PHOENIX_SETUP`.
The standard action:prompt UI works independently of Voyager. Console-only clients
can run `PHOENIX_SETUP PROMPT=0` for the same structured report.

1. Module loaded.
2. Compatible configuration (unsupported kinematics/transforms are reported).
3. Print hooks present, or explicit slicer-only acknowledgement.
4. Park point and path reviewed on an empty bed.
5. Small supervised test print (manual verification, never silently marked tested).

To save reviewed machine coordinates:

```gcode
PHOENIX_SETUP_PROFILE X=10 Y=10 CLEARANCE=5 CONFIRM=1
```

To use the current XY after normal printer controls positioned it:

```gcode
PHOENIX_SETUP_PROFILE USE_CURRENT=1 CONFIRM=1
```

The latter requires XY homed and waits for pending moves; it does not issue new
motion. Its default clearance is the displayed configured value. To acknowledge
slicer-only integration after actually adding both commands:

```gcode
PHOENIX_SETUP_SLICER CONFIRM=1
```

Profile and acknowledgement are saved in checksummed setup-profile.json under the
Phoenix state directory. No configfile.set/SAVE_CONFIG is used, avoiding conflicts
with included config files. No restart is needed. A config fingerprint change or
corrupt profile requires park review again. These edits are rejected while printing,
paused, busy or while an unfinished recovery exists. Setup does not certify the
physical route and does not resolve Z-hop uncertainty.

`setup.complete` means configuration/review are ready, not that a hardware test has
passed. The test step remains manual. Discarding a failed print does not erase the
reviewed setup profile.
