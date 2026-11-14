# Set up Phoenix

Install while your printer is idle. The installer previews its changes, saves backups and connects Phoenix to your print macros and Moonraker Update Manager.

## Install

```sh
cd ~
git clone https://github.com/ozancs/phoenix.git phoenix-for-klipper
cd ~/phoenix-for-klipper
bash scripts/install.sh
```

Review and accept the install plan, then accept the offered restart when the printer is idle. If setup cannot identify your print macros, it explains what remains to connect.

## Choose your park point

Open **PHOENIX_SETUP_GUIDE** from the macro list, or run `PHOENIX_SETUP`. In [Voyager](https://github.com/ozancs/voyager-ui), you can use the Phoenix setup screen.

1. Clear the bed and home using the normal printer controls.
2. Move to a suitable park point with a clear approach and enough lifting clearance.
3. Save the current XY, or enter machine coordinates in the setup screen where available.
4. Confirm that the bed and route are clear, then finish setup.

The point is saved immediately; no SAVE_CONFIG or extra restart is needed. Normal tuning changes and software updates preserve it. A change to XYZ travel limits or kinematics requires a new review. Setup does not prove a route will be clear of every future print.

Try **PHOENIX_DEMO** on the empty bed, then start with a small supervised print. After setup, recording starts and stops automatically with your print hooks.

## Updates

Update Phoenix through Moonraker Update Manager in [Voyager](https://github.com/ozancs/voyager-ui), Mainsail or Fluidd, then restart Klipper while idle. Updates do not require choosing the park point again unless its travel limits or kinematics changed.

## Custom installations

Preview without applying changes:

```sh
bash scripts/install.sh --check
```

For another printer instance or custom macro names:

```sh
bash scripts/install.sh --config-dir ~/printer_2_data/config --moonraker-url http://127.0.0.1:7126 \
  --start-macro MY_PRINT_START --end-macro MY_PRINT_END
```

If your slicer handles initialization without a suitable macro, add `PHOENIX_START` after initialization and before printing the model, and `PHOENIX_END` before end/park/heater-off actions. Reslice existing files, then acknowledge the hooks in setup. Phoenix cannot edit the slicer on your computer.

Rerunning the installer does not duplicate existing hooks or overwrite your park settings. Backups live in `printer_data/phoenix-setup-backups/`. The options `--klipper-dir`, `--deploy-key`, `--yes` and `--restart` are also available; `--yes` alone does not restart services. Never install during a print.

[Back to Phoenix](../README.md) · [Compatibility](COMPATIBILITY.md) · [Recovery guide](RECOVERY.md)
