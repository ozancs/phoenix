"""Phoenix for Klipper: user-guided, best-effort power-loss recovery.

Copyright (C) 2026 Phoenix for Klipper contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""
import copy
import json
import logging
import math
from pathlib import Path
import re
import time
import uuid

from .phoenix_core import (Journal, JournalWriter, atomic_write, canonical, digest,
                           demo_plan, recovery_plan, verify_file, capture_motion_evidence,
                           assess_evidence, run_disk_task, initialize_recording)

VERSION = "0.4.4"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
CHECKS = ("bed_attached", "axes_unmoved", "plate_unmoved", "nozzle_free",
          "path_clear", "uncertainty_accepted")
CHECK_TEXT = {
    "bed_attached": "The print is still firmly attached to the bed.",
    "axes_unmoved": "The axes have not moved since the interruption.",
    "plate_unmoved": "The build plate has not been removed or shifted.",
    "nozzle_free": "The nozzle is free of the print (not frozen into plastic).",
    "path_clear": "The lift, optional XY homing and parking path are clear.",
    "uncertainty_accepted": "I accept position, queue and Z-hop uncertainty.",
}


class Phoenix:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.interval = config.getfloat("save_interval", 2., minval=0.5)
        self.history_size = config.getint("history_size", 30, minval=2, maxval=120)
        self.profile_ready = config.getboolean("profile_ready", False)
        self.profile_error = None
        self.slicer_confirmed = False
        self.park = [config.getfloat("park_x", 0.), config.getfloat("park_y", 0.)]
        self.clearance = config.getfloat("clearance", 5., above=0.)
        self.travel_speed = config.getfloat("travel_speed", 40., above=0., maxval=100.)
        self.z_speed = config.getfloat("z_speed", 3., above=0., maxval=20.)
        self.max_selection_z_delta = config.getfloat("max_selection_z_delta", 2., minval=0.)
        macro = self.printer.load_object(config, "gcode_macro")
        self.home_xy = macro.load_template(config, "home_xy_gcode", "")
        self.directory = config.get("state_path", "~/printer_data/phoenix")
        self.vsd = self.printer.load_object(config, "virtual_sdcard")
        self.move = self.printer.load_object(config, "gcode_move")
        self.active = False
        self.active_file = None
        self.phase = "idle"
        self.reason = ""
        self.job = None
        self.selected = 0
        self.checks = {key: False for key in CHECKS}
        self.plan = None
        self.error = None
        self.layer_z = None
        self.revision = 0
        self.busy = False
        self.demo = {"phase": "off"}
        self.session_id = uuid.uuid4().hex
        self.validation_issue = None
        self.startup_timing = None
        self.shutdown_seen = False
        self.journal = self.writer = None
        for name in ("START", "END", "STATUS", "SELECT", "CHECK", "PREPARE",
                     "RESUME", "DISCARD", "PROMPT", "LAYER", "SETUP",
                     "SETUP_PROFILE", "SETUP_SLICER", "DEMO", "ANALYZE"):
            self.gcode.register_command("PHOENIX_" + name, getattr(self, "cmd_" + name),
                                        desc={"SETUP": "Set up Phoenix or change the park point",
                                              "STATUS": "Show Phoenix recording and recovery status",
                                              "ANALYZE": "Explain recovery evidence and uncertainty without moving",
                                              "DEMO": "Try recovery on an empty bed, without heating or extrusion",
                                              "START": "Automatic print-start hook; not a manual action",
                                              "END": "Automatic print-end hook; not a manual action"}.get(name))
        for name in ("SETUP_HOME", "SETUP_JOG", "DEMO_START", "DEMO_CHECK", "DEMO_RECOVER", "DEMO_EXIT"):
            self.gcode.register_command("_PHOENIX_" + name, getattr(self, "cmd_" + name))
        self.printer.register_event_handler("klippy:ready", self._ready)
        self.printer.register_event_handler("klippy:shutdown", self._shutdown)
        self.printer.register_event_handler("stepper_enable:motor_off", self._motor_off)
        self.printer.register_event_handler("homing:home_rails_begin", self._homing_begin)

    def _ready(self):
        self.toolhead = self.printer.lookup_object("toolhead")
        self.configfile = self.printer.lookup_object("configfile")
        self._load_setup_profile()
        try:
            self.journal = Journal(self.directory)
            self.job = self.journal.load()
            self.writer = JournalWriter(self.journal)
            if self.journal.fence.exists():
                self.phase = "blocked"
                self.error = "Recovery was interrupted after preparation began. Position needs manual review."
            elif self.job and self.job.get("phase") == "recording":
                self.phase = "detected" if self.job.get("checkpoints") else "blocked"
                self.reason = "unclean_shutdown"
                if self.phase == "blocked":
                    self.error = "Interrupted job has no usable checkpoint."
            elif self.job and self.job.get("phase") == "blocked":
                self.phase = "blocked"
                self.error = self.job.get("error", "Previous Phoenix operation failed")
            self._event("ready")
            if not self._setup_status()["complete"]:
                self.gcode.respond_info("Phoenix setup needs attention. Open PHOENIX_SETUP_GUIDE or run PHOENIX_SETUP.")
            if self.phase in ("detected", "blocked"):
                self.gcode.respond_info("Phoenix: interrupted print detected (power loss or restart). "
                                        "No automatic motion. Run PHOENIX_PROMPT or PHOENIX_STATUS.")
        except Exception as exc:
            self.phase, self.error = "blocked", str(exc)
            logging.exception("Phoenix journal initialization")
            self.gcode.respond_info("Phoenix unavailable: " + str(exc))
        self.reactor.register_timer(self._sample, self.reactor.monotonic() + self.interval)

    def _shutdown(self):
        self.active = False
        self.shutdown_seen = True

    def _motor_off(self, *args):
        if self.demo["phase"] not in ("off", "ready", "complete"):
            self.demo.update(phase="blocked", error="Motors were disabled. Exit the demo and home again.")
        if self.phase in ("preparing", "prepared", "heating", "returning"):
            self._block("Motors were disabled during recovery; position is no longer trusted.")

    def _homing_begin(self, homing_state, rails):
        if self.demo["phase"] == "homing":
            if 2 in homing_state.get_axes():
                self.demo.update(phase="blocked", error="Demo recovery must not home Z.")
                raise self.gcode.error(self.demo["error"])
        elif self.demo["phase"] not in ("off", "ready", "complete"):
            self.demo.update(phase="blocked", error="Homing changed the demo position. Exit and start again.")
        if self.phase in ("preparing", "prepared", "heating", "returning") and 2 in homing_state.get_axes():
            self._block("Z homing is not allowed during Phoenix recovery.")
            raise self.gcode.error(self.error)

    def _block(self, message):
        self.active = False
        self.phase, self.error = "blocked", message
        if self.job and self.writer:
            self.job["phase"], self.job["error"] = "blocked", message
            self.writer.submit(self.job)
        self._event("blocked")

    def _event(self, event):
        self.revision += 1
        # Moonraker object subscriptions carry state/revision. Console is for people.
        logging.debug("Phoenix event=%s phase=%s revision=%s", event, self.phase, self.revision)

    def _settings(self):
        return self.configfile.get_status(self.reactor.monotonic())["settings"]

    def _config_hash(self):
        return digest(self._settings())

    def _profile_path(self):
        return Path(self.directory).expanduser() / "setup-profile.json"

    def _park_config_hash(self):
        # Approval tracks the usable machine envelope only. Full print-state
        # validation remains separate in _config_hash().
        status = self.toolhead.get_status(self.reactor.monotonic())
        return digest({"kinematics": self._settings().get("printer", {}).get("kinematics"),
                       "minimum": list(status["axis_minimum"][:3]),
                       "maximum": list(status["axis_maximum"][:3])})

    def _load_setup_profile(self):
        path = self._profile_path()
        if not path.exists():
            return
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            profile = envelope["profile"]
            if digest(profile) != envelope["sha256"] or profile["schema"] not in (1, 2, 3):
                raise ValueError("Setup profile is corrupt; review the park point again")
            self._validate_park(profile["park"], profile["clearance"])
            self.park, self.clearance = profile["park"], profile["clearance"]
            # Old schemas contain only a broad, irreversible hash. Adopt their
            # reviewed point after current bounds validation; do not claim that
            # historical limits were compared. Schema 3 detects future changes.
            if profile["schema"] == 3 and profile["config_hash"] != self._park_config_hash():
                raise ValueError("Travel limits or kinematics changed; saved park point retained. "
                                 "Review it again")
            self.profile_ready = bool(profile["reviewed"])
            self.slicer_confirmed = bool(profile.get("slicer_confirmed", False))
            self.profile_error = None
            if profile["schema"] != 3:
                self._persist_setup_profile()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.profile_ready = False
            self.profile_error = str(exc)

    def _validate_park(self, park, clearance):
        status = self.toolhead.get_status(self.reactor.monotonic())
        if len(park) != 2 or not all(math.isfinite(v) for v in park):
            raise ValueError("Invalid park coordinates")
        for axis, value in enumerate(park):
            if not status["axis_minimum"][axis] <= value <= status["axis_maximum"][axis]:
                raise ValueError("Park point exceeds travel limits")
        if not math.isfinite(clearance) or not 0 < clearance <= status["axis_maximum"][2] - status["axis_minimum"][2]:
            raise ValueError("Invalid Z clearance")

    def _persist_setup_profile(self):
        profile = {"schema": 3, "config_hash": self._park_config_hash(), "park": self.park,
                   "clearance": self.clearance, "reviewed": self.profile_ready,
                   "slicer_confirmed": self.slicer_confirmed}
        atomic_write(self._profile_path(), canonical({"profile": profile, "sha256": digest(profile)}))

    def _setup_status(self, eventtime=None):
        if eventtime is None:
            eventtime = self.reactor.monotonic()
        settings = self._settings()
        compatible_error = None
        try:
            self._compatible()
        except ValueError as exc:
            compatible_error = str(exc)
        hooks = {"start": [], "end": []}
        for name, options in settings.items():
            if name.startswith("gcode_macro "):
                for kind, command in (("start", "PHOENIX_START"), ("end", "PHOENIX_END")):
                    if re.search(r"^\s*" + command + r"(?:\s|$)", options.get("gcode", ""), re.M):
                        hooks[kind].append(name[12:])
        connected = all(hooks.values()) or self.slicer_confirmed
        homed = self.toolhead.get_status(eventtime).get("homed_axes", "")
        editable = (self.phase == "idle" and not self.busy and not self.vsd.is_active()
                    and self.demo["phase"] == "off"
                    and self._status("print_stats").get("state") not in ("printing", "paused"))
        return {"complete": self.profile_ready and connected and not self.profile_error and not compatible_error,
                "profile_ready": self.profile_ready, "profile_error": self.profile_error,
                "park": list(self.park), "clearance": self.clearance,
                "hooks": hooks, "slicer_confirmed": self.slicer_confirmed,
                "homed_axes": homed, "can_edit": editable,
                "can_use_current": editable and all(axis in homed for axis in "xy"),
                "can_jog": editable and all(axis in homed for axis in "xyz"),
                "current_position": list(self.toolhead.get_position())[:3],
                "steps": [
                    {"id": "module", "state": "ok", "message": "Phoenix module loaded"},
                    {"id": "printer", "state": "blocked" if compatible_error else "ok",
                     "message": compatible_error or "Printer configuration supported"},
                    {"id": "hooks", "state": "ok" if connected else "needs_action",
                     "message": "Print hooks found/confirmed" if connected else
                     "Rerun setup to connect macros, or add PHOENIX_START/PHOENIX_END in your slicer"},
                    {"id": "park", "state": "ok" if self.profile_ready else "needs_action",
                     "message": self.profile_error or ("Park point saved" if self.profile_ready else
                     "Choose a clear park point on an empty bed")},
                    {"id": "test", "state": "manual", "message": "Verify recording on a small supervised test print"}]}

    def cmd_SETUP(self, gcmd):
        status = self._setup_status()
        if gcmd.get_int("DETAILS", 0):
            gcmd.respond_info(json.dumps(status, indent=2))
        if not gcmd.get_int("PROMPT", 1):
            if not gcmd.get_int("DETAILS", 0):
                gcmd.respond_info("Phoenix setup is ready." if status["complete"] else
                                  "Phoenix setup is not finished. Open PHOENIX_SETUP_GUIDE.")
            return
        send = lambda line: self.gcode.respond_raw("// action:prompt_" + line)
        send("begin Phoenix setup")
        if not status["can_edit"]:
            send("text Finish the current print or close the interrupted-print recovery before changing setup.")
        elif any(step["state"] == "blocked" for step in status["steps"]):
            for step in status["steps"]:
                if step["state"] == "blocked":
                    send("text " + step["message"])
        elif not all(status["hooks"].values()) and not self.slicer_confirmed:
            send("text Connect Phoenix to your prints. Run the installer again to add the print hooks.")
            send("text If you use slicer-only code: add PHOENIX_START after initialization and PHOENIX_END before end/park actions, then reslice.")
            send("button I added both slicer commands|PHOENIX_SETUP_SLICER CONFIRM=1 REFRESH=1|primary")
        elif self.profile_ready and not gcmd.get_int("EDIT", 0):
            send("text Setup complete. Phoenix records automatically during your normal prints.")
            send("text Saved park: X=%.1f Y=%.1f. Clearance: %.1f mm." % (self.park[0], self.park[1], self.clearance))
            send("button Change park point|PHOENIX_SETUP EDIT=1|secondary")
            send("footer_button Finish|PHOENIX_CLOSE_PROMPT|primary")
            send("show")
            return
        elif not status["can_jog"]:
            send("text First, home the printer so its position is known. Remove all prints from the bed.")
            send("text This setup-only action runs your normal homing routine; all axes may move. Recovery never homes Z.")
            send("button Bed is empty - home printer|_PHOENIX_SETUP_HOME BED_CLEAR=1 CONFIRM=1|primary")
            send("button I homed using printer controls - continue|PHOENIX_SETUP EDIT=1|secondary")
        else:
            send("text Choose the park position. Keep the bed empty and the travel path clear.")
            current = status["current_position"]
            send("text Current X=%.1f Y=%.1f. Jog buttons first lift to at least %.1f mm above Z minimum." % (current[0], current[1], self.clearance))
            for distance in (1, 10):
                send("button_group_start")
                limits = self.toolhead.get_status(self.reactor.monotonic())
                for axis, sign in (("X", -1), ("X", 1), ("Y", -1), ("Y", 1)):
                    index = "XY".index(axis)
                    if limits["axis_minimum"][index] <= current[index] + sign * distance <= limits["axis_maximum"][index]:
                        send("button %s%+d mm|_PHOENIX_SETUP_JOG AXIS=%s DISTANCE=%d CONFIRM=1|secondary" %
                             (axis, sign * distance, axis, sign * distance))
                send("button_group_end")
            send("text Save only after checking that this park point and its approach will be clear during recovery.")
            send("footer_button Save park and finish|PHOENIX_SETUP_PROFILE USE_CURRENT=1 CONFIRM=1 FINISH=1|primary")
        send("footer_button Close|PHOENIX_CLOSE_PROMPT|secondary")
        send("show")

    def cmd_SETUP_HOME(self, gcmd):
        self._setup_edit_allowed(gcmd)
        if gcmd.get_int("BED_CLEAR", 0) != 1:
            raise gcmd.error("Remove prints from the bed before homing for setup.")
        self.busy = True
        try:
            self.gcode.run_script_from_command("G28")
            self.toolhead.wait_moves()
        finally:
            self.busy = False
        # Re-open park selection, including when changing an already saved point.
        followup = self.gcode.create_gcode_command("PHOENIX_SETUP", "PHOENIX_SETUP EDIT=1", {"EDIT": "1"})
        self.cmd_SETUP(followup)

    def cmd_SETUP_JOG(self, gcmd):
        self._setup_edit_allowed(gcmd)
        if not self._setup_status()["can_jog"]:
            raise gcmd.error("Home the printer on an empty bed before choosing a park point.")
        axis = gcmd.get("AXIS", "").upper()
        distance = gcmd.get_float("DISTANCE")
        if axis not in ("X", "Y") or not math.isfinite(distance) or not 0 < abs(distance) <= 10:
            raise gcmd.error("Choose an X or Y jog between 0 and 10 mm.")
        self.toolhead.wait_moves()
        limits = self.toolhead.get_status(self.reactor.monotonic())
        lift = list(self.toolhead.get_position())[:3]
        lift[2] = max(lift[2], limits["axis_minimum"][2] + self.clearance)
        target = list(lift)
        target["XY".index(axis)] += distance
        for index, value in enumerate(target):
            if not limits["axis_minimum"][index] <= value <= limits["axis_maximum"][index]:
                raise gcmd.error("That move would exceed the printer's travel limits.")
        self.busy = True
        try:
            self._move_machine(lift, self.z_speed)
            self._move_machine(target, min(self.travel_speed, 20.))
        finally:
            self.busy = False
        followup = self.gcode.create_gcode_command("PHOENIX_SETUP", "PHOENIX_SETUP EDIT=1", {"EDIT": "1"})
        self.cmd_SETUP(followup)

    def _demo_heaters_off(self):
        if any(self._status(name).get("target", 0) > 0 for name in ("extruder", "heater_bed")):
            return False
        return not any(heater.get_status(self.reactor.monotonic()).get("target", 0) > 0
                       for _, heater in self.printer.lookup_objects("heater_generic"))

    def _demo_plan(self):
        limits = self.toolhead.get_status(self.reactor.monotonic())
        plan = demo_plan(self.toolhead.get_position(),
                         (limits["axis_minimum"], limits["axis_maximum"]), self.clearance)
        if self.profile_ready:
            for i, value in enumerate(self.park):
                if not math.isfinite(value) or not limits["axis_minimum"][i] <= value <= limits["axis_maximum"][i]:
                    raise ValueError("Saved park point is outside travel limits. Update Phoenix setup first.")
            plan["park"][:2] = self.park
        return plan

    def _demo_progress(self, phase, message):
        if self.demo["phase"] == "blocked":
            raise ValueError(self.demo["error"])
        self.demo.update(phase=phase, message=message)
        self.revision += 1
        for line in ("begin Phoenix demo - no real print", "text " + message, "show"):
            self.gcode.respond_raw("// action:prompt_" + line)

    def _demo_home_available(self):
        # Do not bypass machine-specific homing routines, or execute unknown
        # macros that could heat, probe or home Z during an XY-only demo step.
        return not any(name in self._settings() for name in ("homing_override", "gcode_macro g28"))

    def _demo_guard(self, gcmd, homed=False, cold=True):
        if self.busy:
            raise gcmd.error("Wait for the current demo movement to finish. Emergency stop remains available.")
        if self.phase != "idle" or self.vsd.is_active() or self._status("print_stats").get("state") in ("printing", "paused"):
            raise gcmd.error("Demo requires an idle printer with no interrupted print waiting for recovery.")
        try:
            self._compatible()
        except ValueError as exc:
            raise gcmd.error(str(exc))
        if homed and not all(axis in self.toolhead.get_status(self.reactor.monotonic())["homed_axes"] for axis in "xyz"):
            raise gcmd.error("Home the printer using its normal controls, then reopen PHOENIX_DEMO.")
        if cold and not self._demo_heaters_off():
            raise gcmd.error("Turn off all heater targets before starting the demo.")

    def cmd_DEMO(self, gcmd):
        self._demo_guard(gcmd, cold=False)
        if self.demo["phase"] == "off":
            self.demo = {"phase": "ready", "checks": {"position_ok": False, "path_clear": False}, "error": None}
        send = lambda line: self.gcode.respond_raw("// action:prompt_" + line)
        send("begin Phoenix demo - no real print")
        stage = self.demo["phase"]
        if stage == "ready":
            send("text Remove all prints from the bed. This demo moves XYZ but never heats, extrudes or resumes a file.")
            homed = self.toolhead.get_status(self.reactor.monotonic())["homed_axes"]
            if not self._demo_heaters_off():
                send("text Turn off heater targets using the printer controls. The demo never changes your heater settings.")
                send("button Heater targets are off - recheck|PHOENIX_DEMO|primary")
            elif not all(axis in homed for axis in "xyz"):
                send("text First home with the printer's normal controls, then turn any heater targets off and recheck here.")
                send("text Demo does not run your homing macros because they may enable heaters.")
                send("button I homed the printer - recheck|PHOENIX_DEMO|primary")
            else:
                plan = self._demo_plan()
                send("text Example interruption point: X=%.1f Y=%.1f Z=%.1f" % tuple(plan["origin"]))
                send("text %s park: X=%.1f Y=%.1f" % (
                    "Saved" if self.profile_ready else "Example (finish setup to use your own)", *plan["park"][:2]))
                send("button Bed and travel path are empty - start demo|_PHOENIX_DEMO_START BED_CLEAR=1 CONFIRM=1|primary")
        elif stage == "interrupted":
            send("text Pretend power was lost here. The actual printer is still powered and its position is known.")
            send("text Saved demo point: X=%.1f Y=%.1f Z=%.1f" % tuple(self.demo["plan"]["origin"]))
            send("text Next: lift, park, simulate warm-up, signal with the fan, then return. Z is not homed.")
            if not self._demo_home_available():
                send("text Your printer uses custom homing. Demo can keep the existing home position because no power or position was actually lost. Your homing routine will not be changed or run.")
            for key, label in (("position_ok", "The nozzle is at the shown point; nothing moved"),
                               ("path_clear", "The bed and parking/return path are clear")):
                checked = self.demo["checks"][key]
                send("button %s%s|_PHOENIX_DEMO_CHECK ITEM=%s VALUE=%d|%s" %
                     ("[OK] " if checked else "[ ] ", label, key, not checked, "success" if checked else "secondary"))
            if all(self.demo["checks"].values()):
                if self._demo_home_available():
                    send("button Home X/Y and continue|_PHOENIX_DEMO_RECOVER CONFIRM=1 HOME_MODE=XY|primary")
                send("footer_button Continue using current home position|_PHOENIX_DEMO_RECOVER CONFIRM=1 HOME_MODE=KEEP|primary")
        elif stage == "complete":
            send("text Demo complete. Returned to the same XYZ point. No heat, extrusion or file resume was performed.")
            if self.demo.get("home_mode") == "keep":
                send("text Existing home position was used; no homing command was sent. This demo did not test re-homing after a real power loss.")
            if self.demo.get("fan_warning"):
                send("text " + self.demo["fan_warning"])
        elif stage == "blocked":
            send("text " + self.demo.get("error", "Exit the demo and check the printer."))
        send("footer_button Finish demo|_PHOENIX_DEMO_EXIT|secondary")
        send("show")

    def cmd_DEMO_START(self, gcmd):
        self._demo_guard(gcmd, homed=True)
        if self.demo["phase"] != "ready":
            raise gcmd.error("Open PHOENIX_DEMO to start a new demonstration.")
        if gcmd.get_int("BED_CLEAR", 0) != 1 or gcmd.get_int("CONFIRM", 0) != 1:
            raise gcmd.error("Confirm the bed and travel path are empty before starting the demo.")
        self.toolhead.wait_moves()
        plan = self._demo_plan()
        self.demo.update(phase="positioning", plan=plan)
        self.busy = True
        try:
            self._demo_progress("positioning", "Moving to the example interruption point. Heaters remain off.")
            self._demo_move(plan["initial_lift"], self.z_speed)
            self._demo_move(plan["initial_above"], min(self.travel_speed, 30.))
            self._demo_move(plan["origin"], self.z_speed)
            self.demo["phase"] = "interrupted"
        except Exception as exc:
            self.demo.update(phase="blocked", error=str(exc))
            self.gcode.respond_raw("// action:prompt_begin Phoenix demo stopped")
            self.gcode.respond_raw("// action:prompt_text " + str(exc))
            self.gcode.respond_raw("// action:prompt_footer_button Finish demo|_PHOENIX_DEMO_EXIT|secondary")
            self.gcode.respond_raw("// action:prompt_show")
            raise gcmd.error(str(exc))
        finally:
            self.busy = False
        self.cmd_DEMO(gcmd)

    def _demo_move(self, position, speed):
        if self.demo["phase"] == "blocked":
            raise ValueError(self.demo["error"])
        self._move_machine(position, speed)
        if self.demo["phase"] == "blocked":
            raise ValueError(self.demo["error"])

    def cmd_DEMO_CHECK(self, gcmd):
        self._demo_guard(gcmd, homed=True)
        if self.demo["phase"] != "interrupted":
            raise gcmd.error("Start the demo before confirming its recovery checks.")
        item = gcmd.get("ITEM", "")
        if item not in self.demo["checks"]:
            raise gcmd.error("Choose a check from the demo screen.")
        self.demo["checks"][item] = bool(gcmd.get_int("VALUE", minval=0, maxval=1))
        self.cmd_DEMO(gcmd)

    def cmd_DEMO_RECOVER(self, gcmd):
        self._demo_guard(gcmd, homed=True)
        if self.demo["phase"] != "interrupted" or not all(self.demo["checks"].values()) or gcmd.get_int("CONFIRM", 0) != 1:
            raise gcmd.error("Complete the demo checks before simulating recovery.")
        plan = self.demo["plan"]
        # Legacy/native clients without HOME_MODE must also have a usable route.
        mode = gcmd.get("HOME_MODE", "XY" if self._demo_home_available() else "KEEP").upper()
        if mode not in ("KEEP", "XY"):
            raise gcmd.error("Choose KEEP (existing home position) or XY (home X/Y).")
        if mode == "XY" and not self._demo_home_available():
            raise gcmd.error("Use HOME_MODE=KEEP for this demo. Your custom homing routine will not be run or bypassed.")
        if any(abs(self.toolhead.get_position()[i] - plan["origin"][i]) > .01 for i in range(3)):
            raise gcmd.error("The demo position changed. Exit the demo and start again.")
        fan = self.printer.lookup_object("fan", None)
        driver = getattr(fan, "fan", None)
        previous_fan = self._status("fan").get("speed", 0.)
        fan_changed = False
        self.demo["home_mode"] = mode.lower()
        self.busy = True
        try:
            self._demo_progress("lifting", "Lifting for clearance.")
            self._demo_move(plan["lift"], self.z_speed)
            if mode == "XY":
                self._demo_progress("homing", "Homing X and Y. Z stays at the clearance height.")
                self.gcode.run_script_from_command("G28 X Y")
                self.toolhead.wait_moves()
                if not self._demo_heaters_off():
                    raise ValueError("Homing enabled a heater. Stop the demo and review the homing configuration.")
                if abs(self.toolhead.get_position()[2] - plan["lift"][2]) > .01:
                    raise ValueError("Homing changed Z. Exit the demo and check the homing configuration.")
            else:
                self._demo_progress("keeping_position", "Using the existing home position. No homing command is sent in this demo path.")
                self.toolhead.dwell(1.)
                self.toolhead.wait_moves()
            self._demo_progress("parking", "Moving to park X=%.1f Y=%.1f." % tuple(plan["park"][:2]))
            self._demo_move(plan["park"], min(self.travel_speed, 30.))
            self._demo_progress("simulating_heat", "Simulating warm-up. No heaters are being turned on.")
            gcmd.respond_info("Demo: pretending to heat. Heaters stay off; the part fan will signal readiness.")
            self.toolhead.dwell(2.)
            self.toolhead.wait_moves()
            if driver and hasattr(driver, "set_speed_from_command"):
                self._demo_progress("signaling", "Simulated warm-up complete. Part fan: three full-power signals, two seconds each.")
                for _ in range(3):
                    for value in (1., 0.):
                        if self.demo["phase"] == "blocked":
                            raise ValueError(self.demo["error"])
                        fan_changed = True
                        driver.set_speed_from_command(value)
                        self.toolhead.dwell(2. if value else 1.)
                        self.toolhead.wait_moves()
                driver.set_speed_from_command(previous_fan)
                self.toolhead.wait_moves()
                fan_changed = False
            else:
                gcmd.respond_info("Demo warm-up complete. No standard part fan is configured for the signal.")
                self.demo["fan_warning"] = "No standard [fan] found; no fan signal was sent."
            self._demo_progress("returning", "Returning to the saved interruption point. No extrusion or real print will start.")
            self._demo_move(plan["above_origin"], min(self.travel_speed, 30.))
            self._demo_move(plan["origin"], self.z_speed)
            self.demo.update(phase="complete", message="Demo complete. Returned to the same XYZ; printing would resume here.")
        except Exception as exc:
            self.demo.update(phase="blocked", error=str(exc))
            self.gcode.respond_raw("// action:prompt_begin Phoenix demo stopped")
            self.gcode.respond_raw("// action:prompt_text " + str(exc))
            self.gcode.respond_raw("// action:prompt_footer_button Finish demo|_PHOENIX_DEMO_EXIT|secondary")
            self.gcode.respond_raw("// action:prompt_show")
            raise gcmd.error(str(exc))
        finally:
            if fan_changed:
                try:
                    driver.set_speed_from_command(previous_fan)
                except Exception:
                    logging.exception("Phoenix demo fan restore failed")
            self.busy = False
        self.cmd_DEMO(gcmd)

    def cmd_DEMO_EXIT(self, gcmd):
        if self.busy:
            raise gcmd.error("Wait for this demo step to finish, or use the printer emergency stop.")
        self.demo = {"phase": "off"}
        self.gcode.respond_raw("// action:prompt_end")

    def _setup_edit_allowed(self, gcmd):
        self._require(gcmd, ("idle",))
        if self.vsd.is_active() or self._status("print_stats").get("state") in ("printing", "paused"):
            raise gcmd.error("Finish the print before editing setup")
        if gcmd.get_int("CONFIRM", 0) != 1:
            raise gcmd.error("Review the setup and explicitly confirm with CONFIRM=1")

    def cmd_SETUP_PROFILE(self, gcmd):
        self._setup_edit_allowed(gcmd)
        if gcmd.get_int("USE_CURRENT", 0):
            homed = self.toolhead.get_status(self.reactor.monotonic())["homed_axes"]
            if not all(axis in homed for axis in "xy"):
                raise gcmd.error("XY is not homed. Position it using normal controls on an empty bed first.")
            self.toolhead.wait_moves()
            park = list(self.toolhead.get_position())[:2]
        else:
            park = [gcmd.get_float("X"), gcmd.get_float("Y")]
        clearance = gcmd.get_float("CLEARANCE", self.clearance, above=0.)
        old = self.park, self.clearance, self.profile_ready, self.profile_error
        try:
            self._validate_park(park, clearance)
            self.park, self.clearance, self.profile_ready, self.profile_error = park, clearance, True, None
            self._persist_setup_profile()
        except (OSError, ValueError) as exc:
            self.park, self.clearance, self.profile_ready, self.profile_error = old
            raise gcmd.error(str(exc))
        self._event("setup_changed")
        gcmd.respond_info("Park position saved. Phoenix will record automatically during your prints.")
        if gcmd.get_int("FINISH", 0):
            self.gcode.respond_raw("// action:prompt_end")
        elif gcmd.get_int("REFRESH", 0):
            self.cmd_SETUP(gcmd)

    def cmd_SETUP_SLICER(self, gcmd):
        self._setup_edit_allowed(gcmd)
        old = self.slicer_confirmed
        self.slicer_confirmed = True
        try:
            self._persist_setup_profile()
        except OSError as exc:
            self.slicer_confirmed = old
            raise gcmd.error(str(exc))
        self._event("setup_changed")
        if gcmd.get_int("REFRESH", 0):
            self.cmd_SETUP(gcmd)

    def _status(self, name):
        obj = self.printer.lookup_object(name, None)
        return obj.get_status(self.reactor.monotonic()) if obj else {}

    def _mesh(self):
        status = self._status("bed_mesh")
        result = {key: copy.deepcopy(status.get(key)) for key in
                  ("profile_name", "mesh_min", "mesh_max", "probed_matrix", "mesh_matrix")}
        mesh = self.printer.lookup_object("bed_mesh", None)
        # Runtime mesh offsets are not exposed in get_status; reject them instead
        # of silently loading an unshifted profile on recovery.
        if mesh and getattr(mesh, "tool_offset", 0.):
            raise ValueError("Runtime BED_MESH_OFFSET is not supported in Phoenix 0.1")
        z_mesh = getattr(mesh, "z_mesh", None)
        if z_mesh and any(getattr(z_mesh, "mesh_offsets", [0., 0.])):
            raise ValueError("Runtime mesh XY offsets are not supported in Phoenix 0.1")
        return result

    def _save_gcode_state(self):
        self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=__PHOENIX_CAPTURE")
        return copy.deepcopy(self.move.saved_states["__PHOENIX_CAPTURE"])

    def _snapshot(self):
        state = self._save_gcode_state()
        machine = list(self.toolhead.get_position())
        if len(machine) != 4:
            raise ValueError("Additional motion axes are not supported in Phoenix 0.1")
        extruder = self._status("extruder")
        limits = self.toolhead.get_status(self.reactor.monotonic())
        fans = {name: obj.get_status(self.reactor.monotonic())["speed"]
                for name, obj in self.printer.lookup_objects("fan_generic")}
        fans["fan"] = self._status("fan").get("speed", 0.)
        exclusions = self._status("exclude_object").get("excluded_objects", [])
        if exclusions:
            raise ValueError("Active object exclusion is not supported in Phoenix 0.1")
        retract = self.printer.lookup_object("firmware_retraction", None)
        if retract and getattr(retract, "is_retracted", False):
            # Do not record a hidden retract state; existing checkpoints remain usable.
            return None
        evidence = capture_motion_evidence(self.printer, self.reactor.monotonic())
        evidence["session_id"] = self.session_id
        return {
            "id": uuid.uuid4().hex[:12], "saved_at": time.time(),
            "offset": self.vsd.file_position, "machine_position": machine,
            "gcode_position": list(self.move.get_status()["gcode_position"]),
            "gcode_state": state, "layer_z": self.layer_z,
            "z_above_layer": (None if self.layer_z is None else
                              self.move.get_status()["gcode_position"][2] - self.layer_z),
            "position_kind": "commanded_not_measured", "mesh": self._mesh(),
            "motion_evidence": evidence,
            "extruder_target": extruder.get("target", 0.),
            "bed_target": self._status("heater_bed").get("target", 0.),
            "fans": fans,
            "pressure_advance": extruder.get("pressure_advance", 0.),
            "smooth_time": extruder.get("smooth_time", 0.04),
            "motion_limits": {key: limits[key] for key in
                              ("max_velocity", "max_accel", "square_corner_velocity",
                               "minimum_cruise_ratio", "max_accel_to_decel") if key in limits},
        }

    def _sample(self, eventtime):
        try:
            if not self.active:
                return eventtime + self.interval
            if self.writer.error:
                raise ValueError("Storage failed: " + self.writer.error)
            stats = self._status("print_stats")
            if stats.get("state") in ("complete", "cancelled"):
                self._finish(stats["state"])
                return eventtime + self.interval
            if stats.get("state") == "error":
                self.active = False
                self.phase, self.reason = "detected", "print_error"
                self._event("interrupted")
                return eventtime + self.interval
            # Sample only BETWEEN complete virtual-SD commands, never while a macro
            # has yielded mid-command. No reactor yields between this guard and copy.
            if (not self.vsd.is_active() or self.vsd.is_cmd_from_sd()
                    or self.gcode.get_mutex().test()):
                return eventtime + min(self.interval, 0.1)
            if self.active_file is not None and getattr(self.vsd, "current_file", None) is not self.active_file:
                raise ValueError("Virtual-SD source changed without a Phoenix start hook")
            with self.gcode.get_mutex():
                snapshot = self._snapshot()
            if snapshot and snapshot["offset"] < self.job["file"]["size"]:
                if (not self.job["checkpoints"] or
                        snapshot["offset"] != self.job["checkpoints"][-1]["offset"]):
                    self.job["checkpoints"].append(snapshot)
                    self.job["checkpoints"] = self.job["checkpoints"][-self.history_size:]
                else:
                    # Stationary SD offset does not mean stationary MCU time.
                    self.job["checkpoints"][-1] = snapshot
                # Keep the bounded raw trapq tail only on the newest checkpoint.
                # Older state checkpoints retain capture metadata, not repeated C queues.
                for older in self.job["checkpoints"][:-1]:
                    old_motion = older.get("motion_evidence", {})
                    if old_motion.pop("tracks", None) is not None:
                        old_motion["tracks_pruned"] = True
                self.writer.submit(self.job)
        except Exception as exc:
            logging.exception("Phoenix checkpoint failed")
            self._block(str(exc))
        return eventtime + self.interval

    def _finish(self, phase):
        self.active = False
        self.writer.flush()
        self.journal.terminal(phase)
        self.journal.clear_fence()
        self.phase, self.job = "idle", None
        self.validation_issue = None
        self._event(phase)

    def _require(self, gcmd, phases):
        if self.demo["phase"] != "off":
            raise gcmd.error("Finish the Phoenix demo before using setup, recovery or starting a print.")
        if self.busy:
            raise gcmd.error("Phoenix is still working. Wait for the current step to finish.")
        if self.phase not in phases:
            if self.phase == "idle":
                message = "There is no interrupted print to recover. Phoenix records automatically when you print."
            elif self.phase == "recording":
                message = "A print is already being recorded. Recovery controls are only for interrupted prints."
            elif self.phase == "blocked":
                message = "Recovery needs attention. " + (self.error or "Open PHOENIX_RECOVERY for details.")
            elif phases == ("prepared",):
                message = "Open PHOENIX_RECOVERY and finish the checks and parking step before resuming."
            elif phases == ("idle",):
                message = "Finish or discard the interrupted-print recovery before changing setup or starting another print."
            else:
                message = "This step is not available now. Open PHOENIX_RECOVERY to see the next action."
            raise gcmd.error(message)
        if not self.writer:
            raise gcmd.error(self.error or "Phoenix is not ready")

    def _compatible(self):
        settings = self._settings()
        if settings.get("printer", {}).get("kinematics") not in ("cartesian", "corexy"):
            raise ValueError("Phoenix 0.1 supports Cartesian and CoreXY printers")
        unsupported = ("dual_carriage", "skew_correction", "z_thermal_adjust", "bed_tilt",
                       "bed_mesh_offset", "extruder1", "manual_stepper")
        for section in settings:
            if any(section == key or section.startswith(key + " ") for key in unsupported):
                raise ValueError("Unsupported recovery configuration: " + section)
        if not settings.get("force_move", {}).get("enable_force_move", False):
            raise ValueError("Enable [force_move] enable_force_move: True for explicit recovery")

    def cmd_START(self, gcmd):
        if not self.vsd.is_cmd_from_sd() or not self.vsd.file_path():
            raise gcmd.error("PHOENIX_START must run inside a virtual-SD print")
        if self.busy or self.demo["phase"] != "off":
            self._require(gcmd, ("idle",))
        current = getattr(self.vsd, "current_file", None)
        # A second hook in the same open print is not a new print, even when
        # the slicer and PRINT_START both contain PHOENIX_START.
        if self.active and current is not None and current is self.active_file:
            return
        fresh_file = current is not None and current is not self.active_file
        replace_recovery = (fresh_file and self.writer is not None
                            and self.phase in ("recording", "detected", "prepared", "blocked"))
        if not replace_recovery:
            self._require(gcmd, ("idle",))
        try:
            self._compatible()
            # Copy all Klipper-owned state before yielding. The worker sees no APIs.
            path, root = self.vsd.file_path(), self.vsd.sdcard_dirname
            settings = copy.deepcopy(self._settings())
        except Exception as exc:
            raise gcmd.error(str(exc))
        metadata = {"id": uuid.uuid4().hex, "phase": "recording", "checkpoints": [],
                    "started_at": time.time(), "version": VERSION}
        self.active, self.busy, self.phase = False, True, "starting"
        self._event("recording_initializing")
        timings = {"replaced_recovery": replace_recovery}
        started = time.monotonic()
        logging.info("Phoenix startup begin: version=%s replace=%s", VERSION, replace_recovery)
        try:
            job = run_disk_task(self.reactor, lambda: initialize_recording(
                self.writer, self.journal, path, root, metadata, settings, replace_recovery, timings))
            if self.shutdown_seen:
                raise ValueError("Printer shut down while Phoenix initialized; recording was not activated")
            if getattr(self.vsd, "current_file", None) is not current:
                raise ValueError("Print file changed while Phoenix initialized; recording was not activated")
            self.job = job
            self.active, self.phase, self.layer_z = True, "recording", None
            self.active_file = current
            self.error = None
            self.validation_issue, self.reason = None, ""
            self.selected, self.plan = 0, None
            self.checks = {key: False for key in CHECKS}
            self._event("recording_started")
            if replace_recovery:
                self.gcode.respond_raw("// action:prompt_end")
                gcmd.respond_info("New print started. Previous Phoenix recovery was discarded automatically.")
        except Exception as exc:
            # Do not submit the old job over the worker's partially completed
            # transaction. Existing journal/fence logic handles restart recovery.
            self.active, self.phase, self.error = False, "blocked", str(exc)
            timings["error"] = str(exc)
            raise gcmd.error(str(exc))
        finally:
            self.busy = False
            timings["total_ms"] = round((time.monotonic()-started)*1000., 3)
            self.startup_timing = dict(timings)
            logging.info("Phoenix startup timing: %s", json.dumps(timings, sort_keys=True))

    def cmd_END(self, gcmd):
        if self.active:
            try:
                self.toolhead.wait_moves()
                self._finish("complete")
            except Exception as exc:
                raise gcmd.error(str(exc))

    def cmd_LAYER(self, gcmd):
        self.layer_z = gcmd.get_float("Z", minval=0.)

    def get_status(self, eventtime):
        points = self.job.get("checkpoints", []) if self.job else []
        summary = [{"id": p["id"], "index": index, "saved_at": p["saved_at"],
                    "offset": p["offset"], "machine_position": p["machine_position"][:3],
                    "gcode_position": p["gcode_position"][:3], "layer_z": p["layer_z"],
                    "z_above_layer": p["z_above_layer"]}
                   for index, p in enumerate(reversed(points))]
        demo = copy.deepcopy(self.demo)
        demo["home_xy_available"] = self._demo_home_available()
        demo["home_modes"] = ["keep", "xy"] if demo["home_xy_available"] else ["keep"]
        demo["park_source"] = "saved_profile" if self.profile_ready else "example"
        demo["homed_axes"] = self.toolhead.get_status(eventtime).get("homed_axes", "")
        demo["heaters_off"] = self._demo_heaters_off()
        demo["can_start"] = (demo["phase"] == "ready" and self.phase == "idle" and not self.busy
                             and not self.vsd.is_active() and demo["heaters_off"]
                             and all(axis in demo["homed_axes"] for axis in "xyz")
                             and self._status("print_stats").get("state") not in ("printing", "paused"))
        return {"api_version": 1, "version": VERSION, "phase": self.phase,
                "revision": self.revision, "busy": self.busy,
                "recovery_available": self.phase == "detected" and bool(points),
                "job_id": self.job.get("id") if self.job else None,
                "filename": self.job.get("file", {}).get("relative_path") if self.job else None,
                "file_size": self.job.get("file", {}).get("size") if self.job else None,
                "reason": self.reason, "error": self.error,
                "profile_ready": self.profile_ready, "checks": dict(self.checks),
                "setup": self._setup_status(eventtime),
                "demo": demo,
                "analysis": self._analysis(),
                "startup_timing": copy.deepcopy(self.startup_timing),
                "selected_index": self.selected, "checkpoints": summary,
                "assumed_position": points[-1]["machine_position"][:3] if points else None,
                "plan": copy.deepcopy(self.plan),
                "storage_error": self.writer.error if self.writer else self.error,
                "journal_fallback": self.journal.fallback if self.journal else False,
                "uncertainties": ["axes_assumed_unchanged", "queued_motion", "z_hop", "sample_gap"],
                "capabilities": {"z_homing": False, "z_hop": True,
                                 "checkpoint_selection": True, "arbitrary_line_selection": False,
                                 "automatic_resume": False}}

    def _analysis(self):
        points = self.job.get("checkpoints", []) if self.job else []
        latest = points[-1] if points else None
        motion = latest.get("motion_evidence", {}) if latest else {}
        decision = assess_evidence({"no_valid_checkpoint": self.phase in ("detected", "blocked") and not points,
                                    "storage_failed": bool(self.writer and self.writer.error),
                                    "state_unsupported": self.phase == "blocked" or bool(self.validation_issue)})
        return {"schema": 1, "confidence": decision, "applicable": bool(points) or self.phase == "blocked",
                "last_checkpoint": ({"id": latest["id"], "saved_at": latest["saved_at"],
                                     "offset": latest["offset"]} if latest else None),
                "assumed_position": latest["machine_position"] if latest else None,
                "estimated_stop_position": None, "physical_error_bounds_mm": None,
                "stop_time_known": False,
                "motion_recorded": bool(motion.get("tracks")),
                "motion_capture_error": motion.get("unavailable"),
                "validation_error": self.validation_issue,
                "candidate_resume_offset": points[-1-self.selected]["offset"] if points else None,
                "summary": ("No interrupted-print evidence." if not latest else
                            "Checkpoint position is commanded, not measured. The outage time and physical error bounds are unknown. Manual verification is required."),
                "risks": ["queued_motion", "unrecorded_tail", "unmeasured_axis_motion",
                          "z_hop", "pressure_advance_and_nozzle_pressure", "replayed_or_missing_extrusion"]}

    def cmd_ANALYZE(self, gcmd):
        analysis = self._analysis()
        if gcmd.get_int("DETAILS", 0):
            gcmd.respond_info(json.dumps(analysis, indent=2))
        else:
            gcmd.respond_info("Phoenix: %s. %s" % (analysis["confidence"]["class"], analysis["summary"]))

    def cmd_STATUS(self, gcmd):
        if gcmd.get_int("DETAILS", 0):
            gcmd.respond_info(json.dumps(self.get_status(self.reactor.monotonic()), indent=2))
            return
        if self.demo["phase"] != "off":
            gcmd.respond_info("A demonstration is open; no real print is being recovered. Continue or finish in PHOENIX_DEMO.")
            return
        messages = {"idle": "No interrupted print. Recording starts automatically with your print.",
                    "starting": "Initializing recovery recording. Disk work is running in the background.",
                    "recording": "Recording the current print. No manual action needed.",
                    "detected": "An interrupted print is available. Open PHOENIX_RECOVERY.",
                    "prepared": "Printer parked. Clean/prime, then continue from PHOENIX_RECOVERY.",
                    "blocked": "Recovery needs attention: " + (self.error or "Open PHOENIX_RECOVERY."),
                    "preparing": "Preparing the printer for recovery.", "heating": "Waiting for printing temperatures.",
                    "returning": "Returning to the selected print position."}
        gcmd.respond_info(messages.get(self.phase, "Open PHOENIX_RECOVERY to check progress."))

    def cmd_SELECT(self, gcmd):
        self._require(gcmd, ("detected",))
        index = gcmd.get_int("INDEX", minval=0)
        points = self.job["checkpoints"]
        if index >= len(points):
            raise gcmd.error("Checkpoint index out of range")
        if abs(points[-1]["machine_position"][2] - points[-1-index]["machine_position"][2]) > self.max_selection_z_delta:
            raise gcmd.error("Selected checkpoint is too far away in Z")
        self.selected = index
        self.checks = {key: False for key in CHECKS}
        self._event("selection_changed")

    def cmd_CHECK(self, gcmd):
        self._require(gcmd, ("detected",))
        item = gcmd.get("ITEM").lower()
        if item not in CHECKS:
            raise gcmd.error("Unknown checklist item")
        self.checks[item] = bool(gcmd.get_int("VALUE", minval=0, maxval=1))
        self._event("check_changed")
        if gcmd.get_int("REFRESH", 0):
            self.cmd_PROMPT(gcmd)

    def _validate_recovery(self):
        self.validation_issue = None
        try:
            self._validate_recovery_state()
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self.validation_issue = str(exc)
            raise

    def _validate_recovery_state(self):
        self._compatible()
        if self._analysis()["confidence"]["class"] == "unsafe":
            raise ValueError("Recovery evidence is unsafe. Inspect PHOENIX_ANALYZE before continuing.")
        if self.vsd.is_active() or self._status("print_stats").get("state") == "paused":
            raise ValueError("Another print is active or paused")
        if self._config_hash() != self.job["config_hash"]:
            raise ValueError("Printer configuration changed since the saved print")
        self.target = copy.deepcopy(self.job["checkpoints"][-1-self.selected])
        self.source_path = verify_file(self.job["file"], self.vsd.sdcard_dirname,
                                       self.target["offset"])
        expected = set(self._save_gcode_state())
        if set(self.target["gcode_state"]) != expected:
            raise ValueError("Klipper G-code state format changed; recovery refused")
        if not SAFE_NAME.fullmatch(self.target["mesh"].get("profile_name") or "none"):
            raise ValueError("Mesh profile name must contain only letters, numbers, _, . or -")
        status = self.toolhead.get_status(self.reactor.monotonic())
        self.plan = recovery_plan(self.job["checkpoints"][-1], self.target, self.clearance,
                                  self.park, (status["axis_minimum"], status["axis_maximum"]))

    def _restore_mesh(self):
        mesh = self.printer.lookup_object("bed_mesh", None)
        if mesh:
            profile = self.target["mesh"].get("profile_name")
            if profile:
                self.gcode.run_script_from_command("BED_MESH_PROFILE LOAD=" + profile)
            else:
                self.gcode.run_script_from_command("BED_MESH_CLEAR")
        if digest(self._mesh()) != digest(self.target["mesh"]):
            raise ValueError("Saved bed mesh is unavailable or changed; do not re-probe over the print")

    def _move_machine(self, xyz, speed):
        if self.phase == "blocked":
            raise ValueError(self.error)
        position = list(self.toolhead.get_position())
        position[:3] = xyz
        self.toolhead.move(position, speed)
        self.toolhead.wait_moves()
        self.move.reset_last_position()
        if self.phase == "blocked":
            raise ValueError(self.error)

    def cmd_PREPARE(self, gcmd):
        self._require(gcmd, ("detected",))
        if not self.profile_ready:
            raise gcmd.error("Open PHOENIX_SETUP to review/save the park profile (profile_ready is false)")
        if not all(self.checks.values()):
            raise gcmd.error("Complete every Phoenix checklist item first")
        self.busy = True
        motion_fenced = False
        try:
            self.writer.flush()
            self._validate_recovery()
            self._restore_mesh()
            self.journal.fence_motion()
            motion_fenced = True
            self.phase = "preparing"
            self._event("preparing")
            xyz = self.plan["assumed"]
            self.gcode.run_script_from_command("SET_KINEMATIC_POSITION X=%.9f Y=%.9f Z=%.9f" % tuple(xyz))
            self._move_machine(self.plan["lift"], self.z_speed)
            self.home_xy.run_gcode_from_command()
            # Homing overrides must not change Z or disable motors.
            if abs(self.toolhead.get_position()[2] - self.plan["lift"][2]) > 0.01:
                raise ValueError("XY homing macro changed Z")
            self._move_machine(self.plan["park"], self.travel_speed)
            self.phase = "prepared"
            self._event("prepared")
            gcmd.respond_info("Phoenix parked. Clean the blob, heat/purge as needed without moving axes. "
                              "Then PHOENIX_RESUME CLEANED=1 PATH_CLEAR=1. No Z homing.")
            if gcmd.get_int("REFRESH", 0):
                self.cmd_PROMPT(gcmd)
        except Exception as exc:
            if motion_fenced:
                self._block(str(exc))
            raise gcmd.error(str(exc))
        finally:
            self.busy = False

    def _restore_process(self):
        target = self.target
        if self.printer.lookup_object("heater_bed", None):
            self.gcode.run_script_from_command("M140 S%.3f" % target["bed_target"])
        self.gcode.run_script_from_command("M104 S%.3f" % target["extruder_target"])
        if self.printer.lookup_object("heater_bed", None) and target["bed_target"]:
            self.gcode.run_script_from_command("M190 S%.3f" % target["bed_target"])
        self.gcode.run_script_from_command("M109 S%.3f" % target["extruder_target"])
        self.gcode.run_script_from_command("SET_PRESSURE_ADVANCE ADVANCE=%.9f SMOOTH_TIME=%.9f" %
                                          (target["pressure_advance"], target["smooth_time"]))
        keys = {"max_velocity": "VELOCITY", "max_accel": "ACCEL",
                "square_corner_velocity": "SQUARE_CORNER_VELOCITY",
                "minimum_cruise_ratio": "MINIMUM_CRUISE_RATIO",
                "max_accel_to_decel": "ACCEL_TO_DECEL"}
        params = " ".join("%s=%.9f" % (keys[key], value)
                          for key, value in target["motion_limits"].items())
        if params:
            self.gcode.run_script_from_command("SET_VELOCITY_LIMIT " + params)
        for name, value in target["fans"].items():
            if name == "fan":
                if self.printer.lookup_object("fan", None):
                    self.gcode.run_script_from_command("M106 S%.6f" % (value * 255.))
            else:
                fan_name = name.split(" ", 1)[1]
                if not SAFE_NAME.fullmatch(fan_name):
                    raise ValueError("Unsupported generic fan name")
                self.gcode.run_script_from_command("SET_FAN_SPEED FAN=%s SPEED=%.9f" % (fan_name, value))

    def cmd_RESUME(self, gcmd):
        self._require(gcmd, ("prepared",))
        if gcmd.get_int("CLEANED", 0) != 1 or gcmd.get_int("PATH_CLEAR", 0) != 1:
            raise gcmd.error("Confirm CLEANED=1 PATH_CLEAR=1 before returning to the print")
        self.busy = True
        try:
            verify_file(self.job["file"], self.vsd.sdcard_dirname, self.target["offset"])
            if self.vsd.is_active():
                raise ValueError("Another print is active")
            if self._config_hash() != self.job["config_hash"]:
                raise ValueError("Configuration changed during preparation")
            if digest(self._mesh()) != digest(self.target["mesh"]):
                raise ValueError("Mesh changed during preparation")
            if "xyz" != self.toolhead.get_status(self.reactor.monotonic())["homed_axes"]:
                raise ValueError("Axes are no longer homed")
            pos = self.toolhead.get_position()
            if any(abs(pos[i] - self.plan["park"][i]) > 0.01 for i in range(3)):
                raise ValueError("Axes moved from the Phoenix park point; recovery refused")
            self.phase = "heating"
            self._event("heating")
            self._restore_process()
            if self.phase == "blocked":
                raise ValueError(self.error)
            self.phase = "returning"
            self._event("returning")
            self._move_machine(self.plan["above_target"], self.travel_speed)
            self._move_machine(self.plan["target"], self.z_speed)
            self.move.saved_states["__PHOENIX_RESUME"] = copy.deepcopy(self.target["gcode_state"])
            self.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=__PHOENIX_RESUME MOVE=0")
            # Use the virtual-SD loader, but never run PRINT_START or the file prefix.
            # Private API is isolated here and exercised by adapter tests.
            self.vsd._reset_file()
            self.vsd._load_file(gcmd, self.job["file"]["relative_path"], check_subdirs=True)
            self.vsd.file_position = self.target["offset"]
            self.vsd.next_file_position = self.target["offset"]
            pause = self.printer.lookup_object("pause_resume", None)
            if pause:
                self.gcode.run_script_from_command("CLEAR_PAUSE")
            # New baseline before removing the fence. Keep only selected and earlier
            # history so a second recovery cannot jump forward to the abandoned tail.
            self.job["checkpoints"] = self.job["checkpoints"][:len(self.job["checkpoints"])-self.selected]
            self.writer.flush()
            self.journal.save(self.job)
            self.journal.save(self.job)
            self.journal.clear_fence()
            self.selected = 0
            self.active, self.phase = True, "recording"
            self.active_file = getattr(self.vsd, "current_file", None)
            self.checks = {key: False for key in CHECKS}
            self.vsd.do_resume()
            self._event("resumed")
            self.gcode.respond_raw("// action:prompt_end")
        except Exception as exc:
            self._block(str(exc))
            raise gcmd.error(str(exc))
        finally:
            self.busy = False

    def cmd_DISCARD(self, gcmd):
        self._require(gcmd, ("idle", "detected", "blocked", "prepared"))
        if self.vsd.is_active():
            raise gcmd.error("Cannot discard while printing")
        if gcmd.get_int("CONFIRM", 0) != 1:
            raise gcmd.error("Use PHOENIX_DISCARD CONFIRM=1 to forget this recovery")
        try:
            self._finish("discarded")
            self.error = None
            self.gcode.respond_raw("// action:prompt_end")
        except Exception as exc:
            raise gcmd.error(str(exc))

    def cmd_PROMPT(self, gcmd):
        # Standard action:prompt protocol; no separate web server or UI dependency.
        send = lambda value: self.gcode.respond_raw("// action:prompt_" + value)
        send("begin Phoenix for Klipper")
        if self.phase == "detected":
            send("text Interrupted print detected. Power loss or restart may be the cause.")
            send("text No Z homing. Saved positions are estimates; Z-hop and queued moves may differ.")
            analysis = self._analysis()
            send("text Recovery confidence: " + analysis["confidence"]["class"] + ". Physical position error is unknown; no automatic resume.")
            if analysis["validation_error"]:
                send("text " + analysis["validation_error"])
            for key in CHECKS:
                mark = "[OK] " if self.checks[key] else "[ ] "
                # Prompt buttons use a single registered command; REFRESH is optional.
                command = "PHOENIX_CHECK ITEM=%s VALUE=%d REFRESH=1" % (key, not self.checks[key])
                send("button %s%s|%s|%s" % (mark, CHECK_TEXT[key], command,
                                               "success" if self.checks[key] else "secondary"))
            send("footer_button Prepare recovery|PHOENIX_PREPARE REFRESH=1|primary")
        elif self.phase == "prepared":
            send("text Clean the blob and prime filament at the park point. Do not move XYZ.")
            send("footer_button Cleaned; path clear; resume|PHOENIX_RESUME CLEANED=1 PATH_CLEAR=1|primary")
        else:
            message = self.error or ({"idle": "No interrupted print to recover. Recording starts automatically when you print.",
                                      "recording": "Your print is being recorded. No manual action is needed."}.get(
                                      self.phase, "Phoenix is preparing the printer. Wait for this step to finish."))
            send("text " + message)
        send("footer_button Close|PHOENIX_CLOSE_PROMPT|secondary")
        send("show")


def load_config(config):
    phoenix = Phoenix(config)
    phoenix.gcode.register_command("PHOENIX_CLOSE_PROMPT",
                                  lambda gcmd: phoenix.gcode.respond_raw("// action:prompt_end"))
    return phoenix
