# Before you install

Phoenix is experimental software for **single-extruder Cartesian and CoreXY printers running Klipper**, printing files through virtual SD. No additional hardware is required. Try the empty-bed demo and a small supervised test before relying on recovery.

## What your printer needs

- A part that remains attached to the same build plate after the interruption.
- Axes that have not moved while power was off. Phoenix cannot measure this for you.
- A clear lifting, parking and return route, with enough Z clearance.
- If bed mesh is used, a named profile already saved persistently that reloads identically. Adaptive or unsaved meshes cannot currently be restored reliably.

Setup checks the configuration and helps you save a park point. It cannot certify that the route is clear of a printed part, clips or other obstacles.

## Current limits

Multi-extruder and multi-material setups, dual carriages, extra motion axes, runtime mesh offsets, skew correction, thermal Z adjustment, bed tilt transforms and active object exclusion are unsupported. Custom macro and accessory state is not automatically reconstructed. Recording skips firmware-retracted states.

Z-hop is allowed, but an outage during a hop can leave physical Z different from the saved value. Likewise, queued movement may not have finished when power failed. Phoenix uses saved commanded positions, not a sensor measurement of the stopping point. Selecting an earlier record can repeat plastic; selecting a later one can skip it. Exact mid-line recovery is not available.

Phoenix never homes Z during recovery. Optional XY homing requires a separately reviewed routine; it does not correct an unknown Z position. If the axes or plate moved, do not confirm that they stayed in place.

[Back to Phoenix](../README.md) · [Setup guide](SETUP.md)
