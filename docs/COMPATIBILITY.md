# Before you install

Phoenix is experimental software for **single-extruder Cartesian and CoreXY printers running Klipper**, printing files through virtual SD. No additional hardware is required. Try the empty-bed demo and a small supervised test before relying on recovery.

## What your printer needs

- A part that remains attached to the same build plate after the interruption.
- Axes that have not moved while power was off. Phoenix cannot measure this for you.
- A clear lifting, parking and return route, with enough Z clearance.
- If bed mesh is used, Phoenix 0.4.11 and later record the complete mesh, including adaptive meshes, and restore it without probing. Older recovery records still require their original saved profile.

Setup checks the configuration and helps you save a park point. It cannot certify that the route is clear of a printed part, clips or other obstacles.

## Current limits

Multi-extruder and multi-material setups, dual carriages, extra motion axes, runtime mesh offsets, skew correction, thermal Z adjustment and bed tilt transforms are unsupported. Objects excluded during the print are excluded again after recovery. Custom macro and accessory state is not automatically reconstructed. Recording skips firmware-retracted states.

Z-hop is allowed, but an outage during a hop can leave physical Z different from the saved value. Likewise, queued movement may not have finished when power failed. Phoenix uses saved commanded positions, not a sensor measurement of the stopping point. Selecting an earlier record can repeat plastic; selecting a later one can skip it. Exact mid-line recovery is not available.

What happens without power depends on the machine. A bed that moves in Z can sink, so the print resumes too high; a gantry that moves in Z (for example a Voron 2.4) can sag toward the part and lose its leveling. Check the gap at the nozzle before confirming. Startup macros that home or probe automatically are reported as warnings: they can drive into the print when power returns.

Phoenix never homes Z during recovery. Optional XY homing requires a separately reviewed routine; it does not correct an unknown Z position. If the axes or plate moved, do not confirm that they stayed in place.

[Back to Phoenix](../README.md) · [Setup guide](SETUP.md)
