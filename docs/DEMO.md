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

[Back to Phoenix](../README.md)
