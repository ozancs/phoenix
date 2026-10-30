"""Phoenix for Klipper: user-guided, best-effort power-loss recovery.

Copyright (C) 2026 Phoenix for Klipper contributors
SPDX-License-Identifier: GPL-3.0-or-later
"""
import copy
import json
import logging
import re
import time
import uuid

from .phoenix_core import (Journal, JournalWriter, digest, file_identity,
                           recovery_plan, verify_file)

VERSION = "0.1.0"
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
        self.journal = self.writer = None
        for name in ("START", "END", "STATUS", "SELECT", "CHECK", "PREPARE",
                     "RESUME", "DISCARD", "PROMPT", "LAYER"):
            self.gcode.register_command("PHOENIX_" + name, getattr(self, "cmd_" + name),
                                        desc="Phoenix " + name.lower())
        self.printer.register_event_handler("klippy:ready", self._ready)
        self.printer.register_event_handler("klippy:shutdown", self._shutdown)
        self.printer.register_event_handler("stepper_enable:motor_off", self._motor_off)
        self.printer.register_event_handler("homing:home_rails_begin", self._homing_begin)

    def _ready(self):
        self.toolhead = self.printer.lookup_object("toolhead")
        self.configfile = self.printer.lookup_object("configfile")
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

    def _motor_off(self, *args):
        if self.phase in ("preparing", "prepared", "heating", "returning"):
            self._block("Motors were disabled during recovery; position is no longer trusted.")

    def _homing_begin(self, homing_state, rails):
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
        self.gcode.respond_raw("// phoenix:event " + json.dumps({
            "api_version": 1, "event": event, "phase": self.phase,
            "job_id": self.job.get("id") if self.job else None,
            "revision": self.revision,
        }, separators=(",", ":")))

    def _settings(self):
        return self.configfile.get_status(self.reactor.monotonic())["settings"]

    def _config_hash(self):
        return digest(self._settings())

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
        return {
            "id": uuid.uuid4().hex[:12], "saved_at": time.time(),
            "offset": self.vsd.file_position, "machine_position": machine,
            "gcode_position": list(self.move.get_status()["gcode_position"]),
            "gcode_state": state, "layer_z": self.layer_z,
            "z_above_layer": (None if self.layer_z is None else
                              self.move.get_status()["gcode_position"][2] - self.layer_z),
            "position_kind": "commanded_not_measured", "mesh": self._mesh(),
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
            with self.gcode.get_mutex():
                snapshot = self._snapshot()
            if snapshot and snapshot["offset"] < self.job["file"]["size"]:
                if (not self.job["checkpoints"] or
                        snapshot["offset"] != self.job["checkpoints"][-1]["offset"]):
                    self.job["checkpoints"].append(snapshot)
                    self.job["checkpoints"] = self.job["checkpoints"][-self.history_size:]
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
        self._event(phase)

    def _require(self, gcmd, phases):
        if self.busy or self.phase not in phases:
            raise gcmd.error("Phoenix action is unavailable in phase " + self.phase)
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
        self._require(gcmd, ("idle",))
        if not self.vsd.is_cmd_from_sd() or not self.vsd.file_path():
            raise gcmd.error("PHOENIX_START must run inside a virtual-SD print")
        try:
            self._compatible()
            identity = file_identity(self.vsd.file_path(), self.vsd.sdcard_dirname)
            self.job = {"id": uuid.uuid4().hex, "phase": "recording", "file": identity,
                        "config_hash": self._config_hash(), "checkpoints": [],
                        "started_at": time.time(), "version": VERSION}
            self.writer.flush()
            # Replace both slots so fallback cannot select a previous job.
            self.journal.save(self.job)
            self.journal.save(self.job)
            self.journal.clear_fence()
            self.active, self.phase, self.layer_z = True, "recording", None
            self.error = None
            self._event("recording_started")
        except Exception as exc:
            raise gcmd.error(str(exc))

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
        return {"api_version": 1, "version": VERSION, "phase": self.phase,
                "revision": self.revision, "busy": self.busy,
                "recovery_available": self.phase == "detected" and bool(points),
                "job_id": self.job.get("id") if self.job else None,
                "filename": self.job.get("file", {}).get("relative_path") if self.job else None,
                "file_size": self.job.get("file", {}).get("size") if self.job else None,
                "reason": self.reason, "error": self.error,
                "profile_ready": self.profile_ready, "checks": dict(self.checks),
                "selected_index": self.selected, "checkpoints": summary,
                "assumed_position": points[-1]["machine_position"][:3] if points else None,
                "plan": copy.deepcopy(self.plan),
                "storage_error": self.writer.error if self.writer else self.error,
                "journal_fallback": self.journal.fallback if self.journal else False,
                "uncertainties": ["axes_assumed_unchanged", "queued_motion", "z_hop", "sample_gap"],
                "capabilities": {"z_homing": False, "z_hop": True,
                                 "checkpoint_selection": True, "arbitrary_line_selection": False,
                                 "automatic_resume": False}}

    def cmd_STATUS(self, gcmd):
        gcmd.respond_info(json.dumps(self.get_status(self.reactor.monotonic()), indent=2))

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
        self._compatible()
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
            raise gcmd.error("Configure and review park position/path, then set profile_ready: True")
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
            send("text " + (self.error or "Phoenix phase: " + self.phase))
        send("footer_button Close|PHOENIX_CLOSE_PROMPT|secondary")
        send("show")


def load_config(config):
    phoenix = Phoenix(config)
    phoenix.gcode.register_command("PHOENIX_CLOSE_PROMPT",
                                  lambda gcmd: phoenix.gcode.respond_raw("// action:prompt_end"))
    return phoenix
