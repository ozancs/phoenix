# Recover an interrupted print

After an interruption, open the recovery screen in [Voyager](https://github.com/ozancs/voyager-ui), or run **PHOENIX_RECOVERY** in Mainsail or Fluidd. Opening the guide does not move or heat the printer.

1. **Inspect the print.** Confirm that the part is attached, the build plate and axes have not moved, and the nozzle is free. If plastic has frozen around the nozzle, heat it under supervision to free it before confirming. **Heat bed and nozzle** (`PHOENIX_HEAT`) uses the saved temperatures; heating the bed early also helps the part stay attached.
2. **Review the saved point and route.** Records are estimates of commanded position, not exact measurements of where power failed. An earlier point can be chosen only on the same layer. Only continue if you accept the uncertainty and the lifting/parking route is clear.
3. **Prepare recovery.** Phoenix lifts above the tallest point printed so far and parks at your saved location. It does not home Z. While parked, the printer's idle timeout is extended (60 min by default) so motors and heaters stay on; stay with the printer.
4. **Clean and prime.** Heat with **Heat bed and nozzle** and push out filament with **Extrude 10 mm** (`PHOENIX_PRIME LENGTH=10`, negative retracts). Remove excess plastic. Do not jog XYZ, home, disable motors or change mesh while parked.
5. **Resume.** Confirm the nozzle is ready and the return path is clear. Phoenix heats the bed first, then the nozzle, restores chamber heaters, returns and continues from the selected record. If the toolhead was moved while parked, Resume refuses and the recovery stays available.

To abandon recovery, use **Discard**. It forgets the interrupted job without moving the printer or changing heater targets. Starting a fresh print also replaces the previous recovery automatically.

If recovery preparation itself was interrupted, Phoenix blocks continuation because the old position can no longer be assumed. Review the machine manually before beginning a new print.

## Which commands do I need?

After setup, none during normal printing. **PHOENIX_SETUP_GUIDE** changes the park point, **PHOENIX_DEMO** demonstrates the flow, and **PHOENIX_RECOVERY** opens an available recovery. **PHOENIX_STATUS** shows a short status message; `PHOENIX_STATUS DETAILS=1` includes the installed version. Other commands are used by the guide or print hooks.

[Back to Phoenix](../README.md) · [Compatibility](COMPATIBILITY.md)
