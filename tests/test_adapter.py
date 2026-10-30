"""Contract tests with Klipper-shaped fakes, not a hardware simulation."""
import copy
import tempfile
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

    def get_position(self):
        return list(self.position)

    def get_status(self, *args):
        return {"axis_minimum": [0., 0., 0.], "axis_maximum": [250., 250., 250.],
                "homed_axes": "xyz", "max_velocity": 200., "max_accel": 3000.}

    def move(self, position, speed):
        self.position = list(position)
        self.moves.append((list(position), speed))

    def wait_moves(self):
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


class Printer:
    def __init__(self, root):
        self.reactor = SimpleNamespace(monotonic=lambda: 100., register_timer=lambda *args: None)
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
        return p

    def test_boot_detection_never_moves_or_heats(self):
        self.interrupted()
        before = len(self.printer.gcode.commands)
        boot = Phoenix(self.config)
        boot._ready()
        self.assertEqual(boot.phase, "detected")
        self.assertEqual(self.printer.toolhead.moves, [])
        self.assertEqual(self.printer.gcode.commands[before:], [])

    def test_prepare_requires_all_checks(self):
        p = self.interrupted()
        p.checks["axes_unmoved"] = False
        with self.assertRaisesRegex(ValueError, "checklist"):
            p.cmd_PREPARE(Command())
        self.assertEqual(self.printer.toolhead.moves, [])

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

    def test_jog_after_prepare_blocks_resume(self):
        p = self.interrupted()
        p.cmd_PREPARE(Command())
        self.printer.toolhead.position[0] += 1.
        with self.assertRaisesRegex(ValueError, "park point"):
            p.cmd_RESUME(Command(CLEANED=1, PATH_CLEAR=1))
        self.assertEqual(p.phase, "blocked")
        self.assertFalse(self.printer.sd.active)

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
        self.assertTrue(any(line.startswith("// phoenix:event ") for line in self.printer.gcode.lines))

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
        self.printer.settings["stepper_z"] = {"position_max": 260}
        boot = Phoenix(self.config)
        boot._ready()
        self.assertFalse(boot.profile_ready)
        self.assertIn("configuration changed", boot.profile_error)

    def test_setup_is_blocked_during_recovery_and_active_print(self):
        p = self.interrupted()
        with self.assertRaises(ValueError):
            p.cmd_SETUP_PROFILE(Command(X=15, Y=25, CONFIRM=1))
        p.phase = "idle"
        self.printer.sd.active = True
        with self.assertRaises(ValueError):
            p.cmd_SETUP_SLICER(Command(CONFIRM=1))

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


if __name__ == "__main__":
    unittest.main()
