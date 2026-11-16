# Changelog

## 0.4.10 - 2026-11-16

- Share the saved nozzle and bed temperatures with recovery interfaces so they
  can use the selected print record without asking you to enter temperatures again.
- Heating remains an explicit action; opening recovery does not enable heaters.

## 0.4.9 - 2026-11-15

- Discord announcements now include the release notes for the published version.
- Only new version tags send announcements; ordinary pushes stay quiet.
- No changes to printer behavior.

## 0.4.8 - 2026-11-15

- Send explicit Discord messages for version tags and main pushes, and verify
  that Discord returns a created message ID before reporting success.
- No changes to printer behavior.

## 0.4.7 - 2026-11-15

- Add Discord feed notifications for main-branch and version-tag pushes.
- No changes to printer behavior.

## 0.4.6 - 2026-11-15

- Simplify documentation to setup, compatibility, demo and recovery guides.
- Remove private-repository instructions and internal development documentation.
- No changes to printer behavior.

## 0.4.5 — 2026-11-10

- Introduce the Phoenix logo, a visual recovery overview and a shorter README.
  Add concise compatibility and recovery guides; refresh setup instructions.
- Remove internal handoff notes, Turkish documents and generated research output
  from the published tree. Simulation tools and tests remain available.
- When a print errors before the first checkpoint, show that no recovery point
  exists instead of offering an empty recovery flow. A fresh print can still
  replace the failed job automatically. Added a lifecycle regression test.

## 0.4.4 — 2026-11-04

- Park approval now tracks only effective XYZ minimum/maximum limits and kinematics.
  Speed, acceleration, stepper tuning and homing configuration edits no longer
  require repeated park approval. Full interrupted-print validation is unchanged.
- Adopt reviewed schema 1/2 profiles after checksum and current park/clearance
  bounds validation, without requiring another confirmation. Their old hashes
  cannot establish historical limits; schema 3 tracks future envelope changes.
  Unreviewed, corrupt and out-of-bounds profiles remain unapproved.

## 0.4.3 — 2026-11-03

- Park approval uses a separate geometry fingerprint (printer, steppers, travel
  limits, homing configuration, skew and thermal Z settings). PID tuning, saved
  meshes, print macros and unrelated plugin settings no longer invalidate it.
  Interrupted-print recovery continues to validate the complete configuration.
- Valid saved coordinates remain visible when a configuration change requires
  renewed approval, instead of silently falling back to phoenix.cfg defaults.
- Setup profiles use schema 2. An unchanged legacy profile migrates automatically;
  a legacy profile whose full configuration already changed needs one final review
  because it contains no separate geometry fingerprint. Corrupt or out-of-bounds
  profiles still cannot enable recovery. No printer movement is performed.

## 0.4.2 — 2026-11-02

- PHOENIX_START offloads G-code hashing, writer drain, replacement tombstones,
  journal fsync/replace and fence clearing to a disk worker. Klipper waits using
  reactor completion instead of blocking its event loop on disk operations.
- Pause the checkpoint producer while starting; publish recording only after both
  generations are durable. Preserve automatic replacement and duplicate-hook rules.
- Reject activation after shutdown or source-file replacement during the wait.
  I/O failures remain visible and do not falsely enable recovery recording.
- Log per-stage startup times and expose status.startup_timing. These are wall
  durations, not reactor-stall measurements or proof of a specific TTC cause.
- Added threading/lifecycle regression tests with blocked disk work. No printer
  settings were changed and no hardware Timer too close reproduction was performed.

## 0.4.1 — 2026-11-02

- A fresh virtual-SD print automatically supersedes pending/prepared/blocked
  recovery instead of failing at the end of PRINT_START after the prime line.
- Reopening the same filename counts as a new print; duplicate hooks in the same
  open file remain idempotent. Console-only START cannot discard an old recovery.
- Validate the new file/config first, replace journal generations and clear old
  selection/checklist/plan/fence state before recording the new job.
- Close the fallback recovery prompt and publish the new recording state.
- Regression tests cover pending/blocked recovery, invalid replacement, console
  commands and same-open-file error states. No hardware commands were executed.

## 0.4.0 — 2026-10-31

- Adds bounded read-only trapq evidence capture, session identity and explicit
  unavailable/pruned metadata; updates evidence at unchanged SD offsets.
- Adds acceleration-aware trajectory envelopes, multiple segment candidates,
  strict offline linear G-code indexing and evidence-based confidence classes.
- PHOENIX_ANALYZE and status.analysis expose uncertainty and validation failures;
  unknown physical position/bounds remain null. Real recovery motion is unchanged.
- Adds pinned upstream lookahead simulation (6,600 interruptions), inverse-homing
  sensitivity experiments (2,800), actual competitor matcher comparison and CI.
- Adds fault-injection/regression tests, source provenance, research report and
  separate hardware validation checklist. No physical/virtual-MCU success claim.
- Existing journals, two-module install/update paths and custom-homing demo retained.

## 0.3.2 — 2026-10-30

- Custom homing no longer leaves the demo without a Continue action. KEEP uses
  the existing valid home position; no homing macros are executed or bypassed.
- Standard homing retains the explicit XY option. Legacy demo commands choose
  KEEP automatically when custom homing is detected.
- UI status exposes available/chosen home modes and a keeping-position message.
  Completion explains that KEEP does not test real power-loss re-homing.
- Homing/position/checklist guards remain enforced; real recovery is unchanged.

## 0.3.1 — 2026-10-30

- Demo uses the reviewed park XY, validates it against travel limits, and labels
  the generated example park when setup is unfinished.
- Confirmed demo recovery lifts, homes XY, parks and returns; Z is never homed.
  Custom G28/homing overrides require review instead of being run or bypassed.
- Live action-prompt progress and native `demo.message` describe each movement,
  simulated warm-up, fan signal and return. Failures replace the progress screen.
- Part-fan signals now run at full power for two seconds each, with one second off.
  Previous speed is restored; absent standard fans produce a visible notice.
- Setup status uses the subscription timestamp for homing state. Regression tests
  verify unhomed → homed → current-XY save; [Voyager](https://github.com/ozancs/voyager-ui)'s mode toggle alone does not save.
- Native [Voyager](https://github.com/ozancs/voyager-ui) UI follow-up is documented separately; no [Voyager](https://github.com/ozancs/voyager-ui) files changed.
- Host tests pass; printer motion and native UI validation remain pending.

## 0.3.0 — 2026-10-30

- Setup now checks homing before offering park selection; explicit empty-bed home,
  bounded 1/10 mm XY jog and Save park and finish replace the raw setup report.
- Routine console JSON/events removed; DETAILS=1 remains for diagnostics.
- Human-readable action errors replace internal phase names. Normal printing needs
  no manual Phoenix commands; low-level hooks remain compatible.
- Adds PHOENIX_DEMO: bounded empty-bed movements, simulated interruption/checklist,
  park, pretend heat, three part-fan pulses, fan restoration and return to the same
  XYZ. No heaters, extrusion, real recovery journal or file-resume operations.
- Exposes setup affordances and separate demo state for native [Voyager](https://github.com/ozancs/voyager-ui) integration.
- Host tests cover unhomed controls, setup jog limits, no-motion menu opening,
  complete demo, fan failure restoration and 200 varied machine-limit examples.
  Real printer validation of this version remains pending.

## 0.2.0 — 2026-10-30

- Guided installer scans includes and previews changes, then backs up edited files.
- Automatically merges force_move, installs the include and connects unambiguous
  start/end macros. Missing or ambiguous macros remain explicit setup actions.
- Adds Moonraker Update Manager integration and printer-side read-only deploy-key
  support for the private repository. Access is verified before config writes.
- Adds PHOENIX_SETUP / PHOENIX_SETUP_GUIDE and an idle-only reviewed park profile.
  Profile persistence requires neither config editing nor SAVE_CONFIG/restart.
- Exposes structured setup progress for [Voyager](https://github.com/ozancs/voyager-ui) integrations.
- Handles duplicate start hooks without resetting a job; new files get new jobs.
- Local validation: 56 Python tests, 6 JavaScript tests and shell syntax checks.
  Real printer, Moonraker service and GitHub deploy-key authentication validation
  still require installation on a printer. No claim of hardware-tested recovery.

## 0.1.0 — 2026-10-30

- Initial experimental checkpoint journal, user-controlled recovery and [Voyager](https://github.com/ozancs/voyager-ui) contract.
