# Empty-bed recovery demo

Run `PHOENIX_DEMO` in the printer console. It opens a guided demonstration, not a
real recovery. Opening it does not move anything. The normal print journal, saved
park profile, source file and print statistics are not modified.

1. Remove prints and other obstacles from the bed/travel area. Turn heater targets off.
2. If the printer is not homed, the guide asks you to home using its normal controls.
   Demo does not execute homing macros: user-defined homing may heat the nozzle.
   After homing, turn any heater targets off and recheck the demo.
3. Preview the generated example XYZ. Confirm the bed/path is clear to start motion.
4. Phoenix moves to that point and pauses as if a print had been interrupted. It
   does not actually cut power or invalidate the printer's known coordinates.
5. Confirm the shown position and clear park/return route in the demo checklist.
6. Choose existing home position (`HOME_MODE=KEEP`) or, with standard homing,
   home X/Y (`HOME_MODE=XY`, never Z). Custom homing defaults to KEEP: demo never
   lost position, so re-homing is not necessary to demonstrate the rest of the flow.
   Simulate recovery: lift, optionally home X/Y, move to the reviewed
   park point, pretend to heat for two seconds, pulse the standard part fan three
   times (full power for two seconds, off for one second), restore its prior speed,
   and return to exactly the same commanded XYZ. No heater, extrusion or file-resume
   commands are sent. Custom `homing_override` / `G28` macros are neither executed
   nor bypassed. KEEP works with them and explicitly does not test re-homing.
7. Finish demo. The head stays at the demo point; no automatic return-to-home move.

Positions derive from the configured axis minimum/maximum, including negative
minima and smaller machines. A reviewed park profile supplies the actual park XY;
otherwise the preview explicitly labels an example park. Invalid saved coordinates
are rejected before motion. Both initial travel and park/return paths are bounded.
Moves use machine coordinates and preserve E. This is for an **empty bed**, not a
collision analysis of an actual model. If no standard part fan exists, a short
message substitutes for the readiness pulses.

Demo requires idle/no active or paused print/no pending real recovery. Setup changes
and real Phoenix actions are unavailable until you finish the demo. Motor disable,
unexpected homing, position changes or move errors prevent a simulated return.
The Exit/Finish button is for waiting stages; it cannot interrupt a running G-code
handler. The printer's normal emergency stop remains the immediate stop mechanism.

All demo state is in memory. A restart abandons it; it is never detected as a real
power-loss recovery. This tests the guided flow and commanded movements, **not**
power-loss accuracy, journal durability or printing quality.

## Frontend contract

`phoenix.demo.phase`: off, ready, positioning, interrupted, lifting, homing, parking,
keeping_position, simulating_heat, signaling, returning, complete, blocked. `demo.message` describes
the active step; action prompts are also refreshed before each step. `demo.plan`,
`demo.checks`, `demo.error` appear as needed. `demo.home_xy_available` indicates
whether the automatic XY homing step is available, NOT whether recovery demo is
possible. `demo.home_modes` always includes `keep`; it also includes `xy` with
standard homing. `demo.home_mode` records the chosen route. `demo.park_source` is
`saved_profile` or `example`. Display `demo.fan_warning` if present; do not claim
that a fan pulse occurred when no standard [fan] was found.
Always label the native screen DEMO and keep it separate from real recovery.

User entry: `PHOENIX_DEMO`. Internal button commands (not ordinary user actions):

- `_PHOENIX_DEMO_START BED_CLEAR=1 CONFIRM=1`
- `_PHOENIX_DEMO_CHECK ITEM=position_ok VALUE=1`
- `_PHOENIX_DEMO_CHECK ITEM=path_clear VALUE=1`
- `_PHOENIX_DEMO_RECOVER CONFIRM=1 HOME_MODE=KEEP` (or `HOME_MODE=XY` when available)
- `_PHOENIX_DEMO_EXIT`

Never pre-check the confirmations. Heater/position/phase guards are enforced by the
backend even when called directly. Do not show the real power-loss notification for
demo.interrupted: the authoritative real `phoenix.phase` remains idle.
