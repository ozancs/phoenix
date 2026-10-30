# Changelog

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
