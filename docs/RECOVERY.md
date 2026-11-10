# Recover an interrupted print

After an interruption, open the recovery screen in [Voyager](https://github.com/ozancs/voyager-ui), or run **PHOENIX_RECOVERY** in Mainsail or Fluidd. Opening the guide does not move or heat the printer.

1. **Inspect the print.** Confirm that the part is attached, the build plate and axes have not moved, and the nozzle is free. If plastic has frozen around the nozzle, use the normal temperature controls under supervision to free it before confirming.
2. **Review the saved point and route.** Records are estimates of commanded position, not exact measurements of where power failed. Only continue if you accept the uncertainty and the lifting/parking route is clear.
3. **Prepare recovery.** Phoenix lifts and parks at your saved location. It does not home Z.
4. **Clean and prime.** Remove excess plastic and use the normal heater/extruder controls as needed. Do not jog XYZ, home, disable motors or change mesh while parked.
5. **Resume.** Confirm the nozzle is ready and the return path is clear. Phoenix restores printing temperatures, returns and continues from the selected record.

To abandon recovery, use **Discard**. It forgets the interrupted job without moving the printer or changing heater targets. Starting a fresh print also replaces the previous recovery automatically.

If recovery preparation itself was interrupted, Phoenix blocks continuation because the old position can no longer be assumed. Review the machine manually before beginning a new print.

## Which commands do I need?

After setup, none during normal printing. **PHOENIX_SETUP_GUIDE** changes the park point, **PHOENIX_DEMO** demonstrates the flow, and **PHOENIX_RECOVERY** opens an available recovery. **PHOENIX_STATUS** shows a short status message. Other commands are used by the guide or print hooks.

[Back to Phoenix](../README.md) · [Compatibility](COMPATIBILITY.md)
