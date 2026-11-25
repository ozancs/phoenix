"""Contract tests with Klipper-shaped fakes, not a hardware simulation."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from klippy.extras.phoenix import CHECKS, Phoenix
from klippy.extras.phoenix_core import file_identity
from test_core import snapshot


class Command:
    def __init__(self, **params):
        self.params, self.messages = params, []

    def get(self, key, default=None):
        return self.params.get(key, default)

    def get_int(self, key, default=None, minval=None, maxval=None):
        value = int(self.params.get(key, default))
        if minval is not None and value < minval or maxval is not None and value > maxval:
            raise ValueError("Parameter out of range")
        return value

    def get_float(self, key, default=None, **kwargs):
        return float(self.params.get(key, default))

    def error(self, value):
        return ValueError(value)

    def respond_info(self, message):
        self.messages.append(message)


class Mutex:
    locked = False

    def test(self):
        return self.locked

    def __enter__(self):
        self.locked = True

    def __exit__(self, *args):
        self.locked = False


class GCode:
    def __init__(self, printer):
        self.printer = printer
        self.commands, self.lines, self.handlers = [], [], {}
        self.mutex = Mutex()
        self.on_command = None

    def register_command(self, name, handler, **kwargs):
        self.handlers[name] = handler

    def create_gcode_command(self, name, line, params):
        return Command(**params)

    def respond_raw(self, message):
        self.lines.append(message)

    respond_info = respond_raw

    def error(self, message):
        return ValueError(message)

    def get_mutex(self):
        return self.mutex

    def run_script_from_command(self, script):
        self.commands.append(script)
        command = script.split()[0]
        params = dict(part.split("=", 1) for part in script.split()[1:] if "=" in part)
        if command == "SAVE_GCODE_STATE":
            self.printer.move.saved_states[params["NAME"]] = copy.deepcopy(self.printer.move.state)
        elif command == "SET_KINEMATIC_POSITION":
            self.printer.toolhead.position[:3] = [float(params[a]) for a in "XYZ"]
            self.printer.toolhead.homed_axes = "xyz"
        elif command == "RESTORE_GCODE_STATE":
            self.printer.move.restored = copy.deepcopy(self.printer.move.saved_states[params["NAME"]])
        if self.on_command:
            self.on_command(script)


class Move:
    def __init__(self):
        self.saved_states = {}
        self.state = snapshot()["gcode_state"]
        self.restored = None

    def get_status(self, *args):
        return {"gcode_position": self.state["last_position"]}

    def reset_last_position(self):
        pass


class Toolhead:
    def __init__(self):
        self.position = [0., 0., 0., 0.]
        self.moves = []
        self.homed_axes = "xyz"

    def get_position(self):
        return list(self.position)

    def get_status(self, *args):
        return {"axis_minimum": [0., 0., 0.], "axis_maximum": [250., 250., 250.],
                "homed_axes": self.homed_axes, "max_velocity": 200., "max_accel": 3000.}

    def move(self, position, speed):
        self.position = list(position)
        self.moves.append((list(position), speed))

    def wait_moves(self):
        pass

    def dwell(self, seconds):
        pass


class SD:
    def __init__(self, root):
        self.sdcard_dirname = str(root)
        self.path = root / "example.gcode"
        self.path.write_bytes(b"G90\nG1 X100\nG1 X120\nPHOENIX_END\n")
        self.file_position = 12
        self.next_file_position = 12
        self.active = self.from_sd = False
        self.loaded = None
        self.current_file = object()

    def get_status(self, *args):
        return {}

    def file_path(self):
        return str(self.path)

    def is_active(self):
        return self.active

    def is_cmd_from_sd(self):
        return self.from_sd

    def _reset_file(self):
        self.file_position = 0

    def _load_file(self, gcmd, filename, check_subdirs=False):
        self.loaded = filename
        self.current_file = object()

    def do_resume(self):
        self.active = True


class TestCompletion:
    """Host fake: pump an optional callback while disk work completes.

    Real Klipper uses a greenlet reactor, not this threading.Event implementation.
    """
    def __init__(self, reactor):
        self.reactor = reactor
        self.ready = threading.Event()
        self.result = None

    def wait(self):
        while not self.ready.wait(.001):
            if self.reactor.on_wait:
                self.reactor.on_wait()
        return self.result


class TestReactor:
    on_wait = None

    def monotonic(self):
        return 100.

    def register_timer(self, *args):
        pass

    def completion(self):
        return TestCompletion(self)

    def async_complete(self, completion, result):
        completion.result = result
        completion.ready.set()


class Printer:
    def __init__(self, root):
        self.reactor = TestReactor()
        self.move, self.toolhead, self.sd = Move(), Toolhead(), SD(root)
        self.gcode = GCode(self)
        self.settings = {"printer": {"kinematics": "corexy"},
                         "force_move": {"enable_force_move": True}}
        self.stats = {"state": "standby"}
        self.objects = {
            "gcode": self.gcode, "gcode_move": self.move, "toolhead": self.toolhead,
            "virtual_sdcard": self.sd,
            "configfile": SimpleNamespace(get_status=lambda t: {"settings": self.settings}),
            "print_stats": SimpleNamespace(get_status=lambda t: self.stats),
            "extruder": SimpleNamespace(get_status=lambda t: {"target": 210.}),
            "gcode_macro": SimpleNamespace(load_template=lambda *a: SimpleNamespace(run_gcode_from_command=lambda: None)),
        }

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def load_object(self, config, name):
        return self.objects[name]

    def lookup_objects(self, prefix):
        return [(name, obj) for name, obj in self.objects.items() if name.startswith(prefix + " ")]

    def register_event_handler(self, *args):
        pass


class Config:
    def __init__(self, printer, directory):
        self.printer = printer
        self.values = {"state_path": str(directory), "profile_ready": True,
                       "park_x": 10., "park_y": 10.}

    def get_printer(self):
        return self.printer

    def get(self, key, default=None, **kwargs):
        return self.values.get(key, default)

    getfloat = getint = getboolean = get


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.printer = Printer(self.root)
        self.config = Config(self.printer, self.root / "state")
        self.phoenix = Phoenix(self.config)
        self.phoenix._ready()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.phoenix.writer.flush)

    def interrupted(self):
        p = self.phoenix
        p.job = {"id": "job1", "phase": "recording", "version": "0.1.0",
                 "file": file_identity(self.printer.sd.path, self.root),
                 "config_hash": p._config_hash(),
                 "checkpoints": [snapshot(20., x=100., offset=12),
                                 snapshot(20.6, x=130., offset=20)]}
        p.journal.save(p.job)
        p.phase = "detected"
        p.checks = {key: True for key in CHECKS}
        self.printer.toolhead.homed_axes = ""  # Klipper restarted: nothing homed.
        return p

    def test_boot_detection_never_moves_or_heats(self):
        self.interrupted()
        before = len(self.printer.gcode.commands)
        boot = Phoenix(self.config)
        boot._ready()
        self.assertEqual(boot.phase, "detected")
        self.assertEqual(self.printer.toolhead.moves, [])
        self.assertEqual(self.printer.gcode.commands[before:], [])

    def test_error_before_first_checkpoint_blocks_empty_recovery(self):
        p = self.phoenix
        self.printer.sd.from_sd = True
        p.cmd_START(Command())
        self.printer.stats["state"] = "error"
        p._sample(100.)
        self.assertFalse(p.active)
        self.assertEqual(p.phase, "blocked")
        self.assertFalse(p.get_status(100.)["recovery_available"])
        self.assertIn("before Phoenix saved", p.error)
        with self.assertRaisesRegex(ValueError, "before Phoenix saved"):
            p.cmd_SELECT(Command(INDEX=0))
        p.writer.flush()
        self.assertEqual(p.journal.load()["phase"], "blocked")
        self.assertEqual(self.printer.toolhead.moves, [])
        self.printer.sd.current_file = object()
        p.cmd_START(Command())
        self.assertEqual(p.phase, "recording")
        self.assertIsNone(p.error)

    def test_prepare_requires_all_checks(self):
        p = self.interrupted()
        p.checks["axes_unmoved"] = False
        with self.assertRaisesRegex(ValueError, "checklist"):
            p.cmd_PREPARE(Command())
        self.assertEqual(self.printer.toolhead.moves, [])

    def test_status_exposes_each_checkpoint_temperature_without_heating(self):
        p = self.interrupted()
        p.job["checkpoints"][0].update(extruder_target=235., bed_target=80.)
        p.job["checkpoints"][1].update(extruder_target=245., bed_target=85.)
        before = list(self.printer.gcode.commands)
        status = p.get_status(100.)
        self.assertEqual(status["checkpoints"][0]["extruder_target"], 245.)
        self.assertEqual(status["checkpoints"][1]["extruder_target"], 235.)
        self.assertEqual(status["checkpoints"][1]["bed_target"], 80.)
        p.cmd_SELECT(Command(INDEX=1))
        self.assertEqual(p.get_status(100.)["selected_index"], 1)
        self.assertEqual(self.printer.gcode.commands, before)
        del p.job["checkpoints"][0]["extruder_target"]
        self.assertIsNone(p.get_status(100.)["checkpoints"][1]["extruder_target"])

    def test_unreviewed_profile_blocks_motion(self):
        p = self.interrupted()
        p.profile_ready = False
        with self.assertRaisesRegex(ValueError, "profile_ready"):
            p.cmd_PREPARE(Command())
        self.assertEqual(self.printer.toolhead.moves, [])

    def test_older_selection_keeps_latest_assumption_and_returns_original_target(self):
        p = self.interrupted()
        p.cmd_SELECT(Command(INDEX=1))
        self.assertFalse(any(p.checks.values()))
        p.checks = {key: True for key in CHECKS}
        p.cmd_PREPARE(Command())
        self.assertEqual(p.plan["assumed"], [130., 100., 20.6])
        self.assertEqual(self.printer.toolhead.moves[0][0][:3], [130., 100., 25.6])
        self.assertEqual(p.phase, "prepared")
        p.cmd_RESUME(Command(CLEANED=1, PATH_CLEAR=1))
        self.assertEqual(self.printer.toolhead.moves[-1][0][:3], [100., 100., 20.])
        self.assertEqual(self.printer.sd.file_position, 12)
        self.assertTrue(self.printer.sd.active)
        self.assertEqual(self.printer.move.restored, snapshot()["gcode_state"])
        self.assertFalse(p.journal.fence.exists())
        self.assertFalse(any(line.startswith("G28") for line in self.printer.gcode.commands))
        self.assertEqual(len(p.job["checkpoints"]), 1)

    def test_recovery_restart_is_blocked_after_motion(self):
        p = self.interrupted()
        p.cmd_PREPARE(Command())
        boot = Phoenix(self.config)
        boot._ready()
        self.assertEqual(boot.phase, "blocked")

    def test_changed_file_rejected_before_motion(self):
        p = self.interrupted()
        self.printer.sd.path.write_bytes(b"G1 X999\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            p.cmd_PREPARE(Command())
        self.assertEqual(self.printer.toolhead.moves, [])
        self.assertEqual(p.phase, "detected")

    def test_changed_config_rejected_before_motion(self):
        p = self.interrupted()
        self.printer.settings["stepper_z"] = {"rotation_distance": 8.}
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            p.cmd_PREPARE(Command())
        self.assertEqual(self.printer.toolhead.moves, [])

    def test_jog_after_prepare_refuses_resume_but_keeps_recovery(self):
        p = self.interrupted()
        p.cmd_PREPARE(Command())
        self.printer.toolhead.position[0] += 1.
        with self.assertRaisesRegex(ValueError, "park point"):
            p.cmd_RESUME(Command(CLEANED=1, PATH_CLEAR=1))
        # Position is still known (homed, motors on): refuse, do not destroy recovery.
        self.assertEqual(p.phase, "prepared")
        self.assertTrue(p.journal.fence.exists())
        self.assertFalse(self.printer.sd.active)
        self.printer.toolhead.position[0] -= 1.
        p.cmd_RESUME(Command(CLEANED=1, PATH_CLEAR=1))
        self.assertTrue(self.printer.sd.active)

    def test_motor_disable_during_heat_prevents_return(self):
        p = self.interrupted()
        p.cmd_PREPARE(Command())
        before = len(self.printer.toolhead.moves)
        self.printer.gcode.on_command = lambda line: p._motor_off() if line.startswith("M109") else None
        with self.assertRaisesRegex(ValueError, "disabled"):
            p.cmd_RESUME(Command(CLEANED=1, PATH_CLEAR=1))
        self.assertEqual(len(self.printer.toolhead.moves), before)
        self.assertFalse(self.printer.sd.active)

    def test_z_homing_is_rejected(self):
        p = self.interrupted()
        p.phase = "preparing"
        with self.assertRaisesRegex(ValueError, "Z homing"):
            p._homing_begin(SimpleNamespace(get_axes=lambda: [2]), [])

    def test_sampling_skips_inflight_macro_and_busy_mutex(self):
        p = self.interrupted()
        p.active = True
        self.printer.sd.active = True
        self.printer.stats["state"] = "printing"
        self.printer.sd.from_sd = True
        p._sample(100.)
        self.assertEqual(len(p.job["checkpoints"]), 2)
        self.printer.sd.from_sd = False
        self.printer.gcode.mutex.locked = True
        p._sample(101.)
        self.assertEqual(len(p.job["checkpoints"]), 2)

    def test_boundary_sample_pairs_offset_and_state(self):
        p = self.interrupted()
        p.active = True
        self.printer.sd.active = True
        self.printer.stats["state"] = "printing"
        self.printer.sd.file_position = 12
        self.printer.toolhead.position = [90., 80., 20.4, 55.]
        p._sample(100.)
        p.writer.flush()
        saved = p.journal.load()["checkpoints"][-1]
        self.assertEqual(saved["offset"], 12)
        self.assertEqual(saved["machine_position"], [90., 80., 20.4, 55.])

    def test_prompt_check_refresh_and_console_compatibility(self):
        p = self.interrupted()
        p.cmd_CHECK(Command(ITEM="bed_attached", VALUE=0, REFRESH=1))
        self.assertFalse(p.checks["bed_attached"])
        self.assertIn("// action:prompt_show", self.printer.gcode.lines)
        self.assertGreater(p.revision, 1)
        self.assertFalse(any(line.startswith("// phoenix:event ") for line in self.printer.gcode.lines))

    def test_status_supports_reconnect_and_no_sensor_claim(self):
        p = self.interrupted()
        status = p.get_status(100.)
        self.assertTrue(status["recovery_available"])
        self.assertEqual(status["api_version"], 1)
        self.assertFalse(status["capabilities"]["z_homing"])
        self.assertTrue(status["capabilities"]["z_hop"])
        self.assertEqual(status["assumed_position"], [130., 100., 20.6])

    def test_setup_profile_persists_without_movement_or_restart(self):
        p = self.phoenix
        p.cmd_SETUP_PROFILE(Command(X=15, Y=25, CLEARANCE=7, CONFIRM=1))
        self.assertEqual(self.printer.toolhead.moves, [])
        boot = Phoenix(self.config)
        boot._ready()
        self.assertEqual(boot.park, [15., 25.])
        self.assertEqual(boot.clearance, 7.)
        self.assertTrue(boot.profile_ready)
        self.assertFalse(any(line in ("RESTART", "SAVE_CONFIG") for line in self.printer.gcode.commands))

    def test_setup_profile_config_change_requires_review(self):
        self.phoenix.cmd_SETUP_PROFILE(Command(X=15, Y=25, CONFIRM=1))
        original_status = self.printer.toolhead.get_status
        self.printer.toolhead.get_status = lambda t: dict(
            original_status(t), axis_maximum=[250., 250., 260.])
        boot = Phoenix(self.config)
        boot._ready()
        self.assertFalse(boot.profile_ready)
        self.assertIn("Travel limits", boot.profile_error)
        self.assertEqual(boot.park, [15., 25.])

    def test_park_approval_ignores_unrelated_config_but_recovery_does_not(self):
        p = self.phoenix
        p.cmd_SETUP_PROFILE(Command(X=15, Y=25, CONFIRM=1))
        previous = p._config_hash()
        self.printer.settings.update({"extruder": {"pid_kp": 23},
                                      "bed_mesh saved": {"points": "1,2,3"},
                                      "gcode_macro PRINT_START": {"gcode": "G28"},
                                      "phoenix": {"save_interval": 3},
                                      "printer": {"kinematics": "corexy", "max_accel": 5000},
                                      "stepper_z": {"homing_speed": 10},
                                      "homing_override": {"gcode": "G28 X Y"}})
        boot = Phoenix(self.config)
        boot._ready()
        self.assertTrue(boot.profile_ready)
        self.assertEqual(boot.park, [15., 25.])
        self.assertNotEqual(previous, boot._config_hash())

    def test_legacy_profile_upgrade_and_changed_config_preserve_point(self):
        p = self.phoenix
        p.cmd_SETUP_PROFILE(Command(X=15, Y=25, CONFIRM=1))
        envelope = json.loads(p._profile_path().read_text())
        profile = envelope["profile"]
        profile.update(schema=1, config_hash=p._config_hash())
        from klippy.extras.phoenix_core import digest
        envelope["sha256"] = digest(profile)
        legacy = json.dumps(envelope)
        p._profile_path().write_text(legacy)
        boot = Phoenix(self.config)
        boot._ready()
        self.assertTrue(boot.profile_ready)
        self.assertEqual(json.loads(p._profile_path().read_text())["profile"]["schema"], 3)
        p._profile_path().write_text(legacy)
        self.printer.settings["extruder"] = {"pid_kp": 23}
        boot = Phoenix(self.config)
        boot._ready()
        self.assertTrue(boot.profile_ready)
        self.assertEqual(boot.park, [15., 25.])
        self.assertIsNone(boot.profile_error)

    def test_setup_is_blocked_during_recovery_and_active_print(self):
        p = self.interrupted()
        with self.assertRaises(ValueError):
            p.cmd_SETUP_PROFILE(Command(X=15, Y=25, CONFIRM=1))
        p.phase = "idle"
        self.printer.sd.active = True
        with self.assertRaises(ValueError):
            p.cmd_SETUP_SLICER(Command(CONFIRM=1))

    def test_park_kinematics_change_requires_review(self):
        self.phoenix.cmd_SETUP_PROFILE(Command(X=15, Y=25, CONFIRM=1))
        self.printer.settings["printer"]["kinematics"] = "cartesian"
        boot = Phoenix(self.config)
        boot._ready()
        self.assertFalse(boot.profile_ready)
        self.assertIn("kinematics changed", boot.profile_error)

    def test_legacy_schema_two_still_rejects_out_of_bounds(self):
        from klippy.extras.phoenix_core import digest
        p = self.phoenix
        p.cmd_SETUP_PROFILE(Command(X=15, Y=25, CONFIRM=1))
        envelope = json.loads(p._profile_path().read_text())
        envelope["profile"].update(schema=2, park=[999., 25.])
        envelope["sha256"] = digest(envelope["profile"])
        p._profile_path().write_text(json.dumps(envelope))
        boot = Phoenix(self.config)
        boot._ready()
        self.assertFalse(boot.profile_ready)
        self.assertIn("exceeds travel limits", boot.profile_error)

    def test_setup_detects_hooks_but_not_comment_examples(self):
        self.printer.settings["gcode_macro PRINT_START"] = {"gcode": "# PHOENIX_START\nG28"}
        self.assertFalse(self.phoenix._setup_status()["hooks"]["start"])
        self.printer.settings["gcode_macro PRINT_START"]["gcode"] += "\nPHOENIX_START"
        self.assertEqual(self.phoenix._setup_status()["hooks"]["start"], ["PRINT_START"])

    def test_setup_out_of_range_never_enables_profile(self):
        p = self.phoenix
        p.profile_ready = False
        with self.assertRaises(ValueError):
            p.cmd_SETUP_PROFILE(Command(X=999, Y=25, CONFIRM=1))
        self.assertFalse(p.profile_ready)
        self.assertFalse(p._profile_path().exists())

    def test_duplicate_start_hook_keeps_existing_job(self):
        self.printer.sd.from_sd = True
        p = self.phoenix
        p.cmd_START(Command())
        job_id = p.job["id"]
        p.cmd_START(Command())
        self.assertEqual(p.job["id"], job_id)

    def test_new_virtual_sd_file_does_not_inherit_old_job(self):
        self.printer.sd.from_sd = True
        p = self.phoenix
        p.cmd_START(Command())
        previous = p.job["id"]
        self.printer.sd.current_file = object()
        p.cmd_START(Command())
        self.assertNotEqual(p.job["id"], previous)

    def setup_hooks(self):
        self.printer.settings["gcode_macro PRINT_START"] = {"gcode": "PHOENIX_START"}
        self.printer.settings["gcode_macro PRINT_END"] = {"gcode": "PHOENIX_END"}

    def test_new_print_replaces_pending_recovery_without_motion(self):
        p = self.interrupted()
        p.selected, p.plan = 1, {"old": True}
        self.printer.sd.from_sd = True
        p.cmd_START(Command())
        self.assertEqual(p.phase, "recording")
        self.assertNotEqual(p.job["id"], "job1")
        self.assertEqual(p.selected, 0)
        self.assertIsNone(p.plan)
        self.assertFalse(any(p.checks.values()))
        self.assertEqual(p.journal.load()["id"], p.job["id"])
        self.assertEqual(self.printer.toolhead.moves, [])
        self.assertEqual(self.printer.gcode.commands, [])
        self.assertIn("// action:prompt_end", self.printer.gcode.lines)

    def test_fresh_print_replaces_blocked_recovery_fence(self):
        p = self.interrupted()
        p.phase, p.error = "blocked", "Previous recovery interrupted"
        p.journal.fence_motion()
        self.printer.sd.from_sd = True
        p.cmd_START(Command())
        self.assertEqual(p.phase, "recording")
        self.assertFalse(p.journal.fence.exists())
        self.assertIsNone(p.error)

    def test_console_start_cannot_discard_pending_recovery(self):
        p = self.interrupted()
        with self.assertRaisesRegex(ValueError, "virtual-SD"):
            p.cmd_START(Command())
        self.assertEqual(p.job["id"], "job1")
        self.assertEqual(p.journal.load()["id"], "job1")

    def test_same_open_file_after_error_is_not_a_fresh_print(self):
        p = self.interrupted()
        p.active_file = self.printer.sd.current_file
        self.printer.sd.from_sd = True
        p.cmd_START(Command())
        self.assertFalse(p.active)
        self.assertEqual(p.reason, "recovery_off")

    def test_invalid_new_print_disables_recovery_without_cancelling(self):
        p = self.interrupted()
        self.printer.sd.from_sd = True
        self.printer.settings["printer"]["kinematics"] = "delta"
        p.cmd_START(Command())
        self.assertEqual(p.journal.load()["phase"], "recovery_off")
        self.assertIsNone(p.job)
        self.assertIn("recovery off for this print", p.error)

    def start_sd(self):
        self.printer.sd.from_sd = True
        self.phoenix.cmd_START(Command())
        return self.phoenix

    def test_unused_manual_stepper_allows_recording(self):
        self.printer.settings["manual_stepper aux_stepper"] = {}
        self.printer.objects["manual_stepper aux_stepper"] = SimpleNamespace(axis_gcode_id=None)
        self.assertTrue(self.start_sd().active)

    def test_skew_section_and_saved_profile_without_correction_are_allowed(self):
        self.printer.settings["skew_correction"] = {}
        self.printer.settings["skew_correction saved"] = {"xy_skew": .01}
        self.printer.objects["skew_correction"] = SimpleNamespace(
            xy_factor=0., xz_factor=0., yz_factor=0., current_profile_name="saved")
        self.assertTrue(self.start_sd().active)

    def test_each_active_skew_factor_disables_recovery_not_print(self):
        for axis in ("xy", "xz", "yz"):
            with self.subTest(axis=axis):
                self.printer.sd.current_file = object()
                self.printer.objects["skew_correction"] = SimpleNamespace(**{axis+"_factor": .001})
                p = self.start_sd()
                self.assertFalse(p.active)
                self.assertIn("Active skew", p.error)
                self.assertEqual(self.printer.gcode.commands, [])

    def test_registered_manual_axis_disables_recovery(self):
        self.printer.objects["manual_stepper aux"] = SimpleNamespace(axis_gcode_id=0)
        p = self.start_sd()
        self.assertFalse(p.active)
        self.assertIn("GCODE_AXIS", p.error)

    def install_manual_guard(self):
        calls = []
        handlers = {"aux": lambda command: calls.append(command)}
        self.printer.gcode.mux_commands = {"MANUAL_STEPPER": ("STEPPER", handlers)}
        self.phoenix._install_runtime_guards()
        return handlers["aux"], calls

    def test_manual_move_mid_print_continues_and_closes_both_journals(self):
        command, calls = self.install_manual_guard()
        p = self.start_sd()
        cmd = Command(MOVE=10)
        command(cmd)
        self.assertEqual(calls, [cmd])
        self.assertFalse(p.active)
        self.assertEqual(p.journal.load()["phase"], "recovery_off")
        for path in p.journal.directory.glob("journal-*.json"):
            self.assertEqual(json.loads(path.read_text())["body"]["payload"]["phase"], "recovery_off")
        revision = p.revision
        p.cmd_START(Command())
        self.assertEqual(revision, p.revision)
        self.printer.sd.current_file = object()
        p.cmd_START(Command())
        self.assertTrue(p.active)

    def test_manual_move_before_start_hook_disables_only_this_file(self):
        command, calls = self.install_manual_guard()
        self.printer.sd.from_sd = True
        command(Command(SET_POSITION=0))
        p = self.start_sd()
        self.assertFalse(p.active)
        self.assertEqual(len(calls), 1)

    def test_idle_auxiliary_move_does_not_disable_next_print(self):
        command, _ = self.install_manual_guard()
        command(Command(MOVE=10))
        self.assertTrue(self.start_sd().active)

    def test_manual_enable_alone_does_not_disable_recording(self):
        command, calls = self.install_manual_guard()
        p = self.start_sd()
        command(Command(ENABLE=1))
        self.assertTrue(p.active)
        self.assertEqual(len(calls), 1)

    def test_skew_command_is_preserved_and_detected_between_samples(self):
        skew = SimpleNamespace(xy_factor=0., xz_factor=0., yz_factor=0.)
        self.printer.objects["skew_correction"] = skew
        calls = []
        def original(cmd):
            calls.append(cmd)
            skew.xy_factor = .01
        self.printer.gcode.ready_gcode_handlers = {"SET_SKEW": original}
        self.phoenix._install_runtime_guards()
        p = self.start_sd()
        cmd = Command(XY="1,2,3")
        self.printer.gcode.ready_gcode_handlers["SET_SKEW"](cmd)
        self.assertEqual(calls, [cmd])
        self.assertFalse(p.active)
        self.assertIn("Active skew", p.error)

    def test_manual_axis_registration_command_disables_without_interrupting(self):
        command, calls = self.install_manual_guard()
        p = self.start_sd()
        command(Command(GCODE_AXIS="A"))
        self.assertFalse(p.active)
        self.assertEqual(len(calls), 1)
        p.cmd_END(Command())
        self.assertEqual(p.phase, "idle")

    def test_skew_clear_does_not_disable_recording(self):
        skew = SimpleNamespace(xy_factor=0., xz_factor=0., yz_factor=0.)
        self.printer.objects["skew_correction"] = skew
        self.printer.gcode.ready_gcode_handlers = {"SET_SKEW": lambda cmd: None}
        self.phoenix._install_runtime_guards()
        p = self.start_sd()
        self.printer.gcode.ready_gcode_handlers["SET_SKEW"](Command(CLEAR=1))
        self.assertTrue(p.active)

    def test_restart_after_recovery_disabled_does_not_offer_old_print(self):
        p = self.interrupted()
        self.printer.settings["printer"]["kinematics"] = "delta"
        self.start_sd()
        boot = Phoenix(self.config)
        boot._ready()
        self.addCleanup(boot.writer.flush)
        self.assertEqual(boot.phase, "idle")
        self.assertFalse(boot.get_status(100.)["recovery_available"])

    def test_sampler_detects_runtime_axis_registration(self):
        p = self.start_sd()
        self.printer.objects["manual_stepper aux"] = SimpleNamespace(axis_gcode_id=4)
        with self.assertLogs(level="ERROR"):
            p._sample(100.)
        self.assertFalse(p.active)
        self.assertIn("GCODE_AXIS", p.error)

    def test_unhomed_setup_shows_home_before_any_save_or_jog(self):
        self.setup_hooks()
        p = self.phoenix
        p.profile_ready = False
        self.printer.toolhead.homed_axes = ""
        self.printer.gcode.lines.clear()
        command = Command()
        p.cmd_SETUP(command)
        output = "\n".join(self.printer.gcode.lines)
        self.assertIn("home printer", output)
        self.assertNotIn("Save park", output)
        self.assertNotIn("SETUP_JOG AXIS", output)
        self.assertEqual(command.messages, [])
        self.assertFalse(p._setup_status()["can_use_current"])

    def test_ready_setup_has_simple_finish_screen(self):
        self.setup_hooks()
        self.printer.gcode.lines.clear()
        self.phoenix.cmd_SETUP(Command())
        output = "\n".join(self.printer.gcode.lines)
        self.assertIn("Setup complete", output)
        self.assertIn("Finish|PHOENIX_CLOSE_PROMPT", output)
        self.assertNotIn("SETUP_JOG AXIS", output)

    def test_jog_lifts_then_moves_xy_without_saving(self):
        p = self.phoenix
        self.setup_hooks()
        self.printer.toolhead.position = [20., 20., 0., 0.]
        p.cmd_SETUP_JOG(Command(AXIS="X", DISTANCE=10, CONFIRM=1))
        self.assertEqual(self.printer.toolhead.moves[0][0][:3], [20., 20., 5.])
        self.assertEqual(self.printer.toolhead.moves[1][0][:3], [30., 20., 5.])
        self.assertFalse(p._profile_path().exists())

    def test_out_of_range_jog_causes_no_lift_or_move(self):
        self.printer.toolhead.position = [249., 20., 0., 0.]
        with self.assertRaisesRegex(ValueError, "travel limits"):
            self.phoenix.cmd_SETUP_JOG(Command(AXIS="X", DISTANCE=10, CONFIRM=1))
        self.assertEqual(self.printer.toolhead.moves, [])

    def test_home_requires_empty_bed_confirmation_and_idle(self):
        p = self.phoenix
        with self.assertRaisesRegex(ValueError, "Remove prints"):
            p.cmd_SETUP_HOME(Command(CONFIRM=1))
        self.assertNotIn("G28", self.printer.gcode.commands)
        p = self.interrupted()
        with self.assertRaisesRegex(ValueError, "recovery"):
            p.cmd_SETUP_HOME(Command(CONFIRM=1, BED_CLEAR=1))
        self.assertNotIn("G28", self.printer.gcode.commands)

    def test_home_is_explicit_setup_only_action(self):
        self.setup_hooks()
        p = self.phoenix
        p.cmd_SETUP_HOME(Command(CONFIRM=1, BED_CLEAR=1))
        self.assertIn("G28", self.printer.gcode.commands)
        self.assertFalse(p.busy)
        self.assertTrue(any("Save park and finish" in line for line in self.printer.gcode.lines))

    def test_save_and_finish_closes_prompt_without_json(self):
        self.setup_hooks()
        command = Command(USE_CURRENT=1, CONFIRM=1, FINISH=1)
        self.phoenix.cmd_SETUP_PROFILE(command)
        self.assertEqual(self.printer.gcode.lines[-1], "// action:prompt_end")
        self.assertEqual(len(command.messages), 1)
        self.assertNotIn("{", command.messages[0])

    def test_idle_check_explains_no_interrupted_print(self):
        with self.assertRaisesRegex(ValueError, "no interrupted print"):
            self.phoenix.cmd_CHECK(Command())

    def test_status_is_human_readable_unless_details_requested(self):
        command = Command()
        self.phoenix.cmd_STATUS(command)
        self.assertNotIn("{", command.messages[0])
        details = Command(DETAILS=1)
        self.phoenix.cmd_STATUS(details)
        self.assertIn('"api_version"', details.messages[0])


if __name__ == "__main__":
    unittest.main()
