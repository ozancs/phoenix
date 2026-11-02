# Changelog

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
  verify unhomed → homed → current-XY save; Voyager's mode toggle alone does not save.
- Native Voyager UI follow-up is documented separately; no Voyager files changed.
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
- Exposes setup affordances and separate demo state for native Voyager integration.
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
- Exposes structured setup progress for Voyager and updates its integration handoff.
- Handles duplicate start hooks without resetting a job; new files get new jobs.
- Local validation: 56 Python tests, 6 JavaScript tests and shell syntax checks.
  Real printer, Moonraker service and GitHub deploy-key authentication validation
  still require installation on a printer. No claim of hardware-tested recovery.

## 0.1.0 — 2026-10-30

- Initial experimental checkpoint journal, user-controlled recovery and Voyager contract.
