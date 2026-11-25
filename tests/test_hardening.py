"""0.4.12 recovery hardening: each test fails on 0.4.11 behaviour."""
import re
import unittest
from types import SimpleNamespace

from klippy.extras.phoenix import CHECKS, Phoenix, VERSION
import test_adapter
from test_adapter import Command
from test_core import snapshot


class RecoveryHardeningTests(unittest.TestCase):
    def setUp(self):
        self.base = test_adapter.AdapterTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.printer = self.base.printer
        self.config = self.base.config
        self.phoenix = self.base.phoenix

    def interrupted(self):
        return self.base.interrupted()

    def printing(self):
        p = self.interrupted()
        p.active = True
        self.printer.sd.active = True
        self.printer.stats["state"] = "printing"
        return p

    def test_journal_keeps_one_mesh_for_many_checkpoints(self):
        p = self.printing()
        for offset, x in ((12, 90.), (20, 95.), (24, 99.)):
            self.printer.sd.file_position = offset
            self.printer.toolhead.position = [x, 80., 20.4, 55.]
            p._sample(100.)
        p.writer.flush()
        saved = p.journal.load()
        new = [point for point in saved["checkpoints"] if "mesh_id" in point]
        self.assertEqual(len(new), 3)
        self.assertTrue(all("mesh" not in point for point in new))
        self.assertEqual(len(saved["meshes"]), 1)

    def test_lower_layer_point_cannot_be_selected(self):
        p = self.interrupted()
        lower = snapshot(19.8, x=90., offset=12)
        lower["layer_z"] = 19.8
        p.job["checkpoints"].insert(0, lower)
        with self.assertRaisesRegex(ValueError, "lower layer"):
            p.cmd_SELECT(Command(INDEX=2))
        self.assertFalse(p.get_status(100.)["checkpoints"][2]["selectable"])
        p.cmd_SELECT(Command(INDEX=1))  # Same layer, below a Z-hop: allowed.
        self.assertEqual(p.selected, 1)

    def test_lower_layer_without_layer_marks_uses_gcode_z(self):
        p = self.interrupted()
        for point in p.job["checkpoints"]:
            point["layer_z"] = None
        p.job["checkpoints"][1]["gcode_position"][2] = 20.
        p.job["checkpoints"][0]["gcode_position"][2] = 19.8
        with self.assertRaisesRegex(ValueError, "lower layer"):
            p.cmd_SELECT(Command(INDEX=1))

    def test_pause_position_used_after_power_loss(self):
        p = self.printing()
        self.printer.stats["state"] = "paused"
        self.printer.sd.active = False
        self.printer.toolhead.position = [240., 240., 40.6, 50.]
        p._sample(100.)
        p.writer.flush()
        self.assertEqual(p.journal.load()["paused_position"][:3], [240., 240., 40.6])
        self.printer.stats["state"] = "standby"
        boot = Phoenix(self.config)
        boot._ready()
        self.addCleanup(boot.writer.flush)
        self.printer.toolhead.homed_axes = ""
        boot.checks = {key: True for key in CHECKS}
        boot.cmd_PREPARE(Command())
        self.assertEqual(boot.plan["assumed"], [240., 240., 40.6])
        self.assertEqual(boot.plan["lift"][2], 45.6)
        self.assertEqual(boot.plan["position_source"]["z"], "paused")

    def test_pause_position_cleared_when_printing_resumes(self):
        p = self.printing()
        self.printer.stats["state"] = "paused"
        self.printer.sd.active = False
        self.printer.toolhead.position = [240., 240., 40.6, 50.]
        p._sample(100.)
        self.printer.stats["state"] = "printing"
        self.printer.sd.active = True
        p._sample(101.)
        self.assertIsNone(p.job["paused_position"])

    def test_pause_cleared_even_when_next_snapshot_is_skipped(self):
        exclude = {"excluded_objects": ["PART_B"], "current_object": "PART_B"}
        self.printer.objects["exclude_object"] = SimpleNamespace(get_status=lambda t: exclude)
        p = self.printing()
        self.printer.stats["state"] = "paused"
        self.printer.sd.active = False
        self.printer.toolhead.position = [240., 240., 40.6, 50.]
        p._sample(100.)
        self.printer.stats["state"] = "printing"
        self.printer.sd.active = True
        p._sample(101.)  # Inside an excluded object: no checkpoint taken.
        p.writer.flush()
        self.assertIsNone(p.journal.load()["paused_position"])
        self.assertNotEqual(p.job.get("max_z"), 40.6)  # A pause lift is not printed height.

    def test_status_reports_the_position_prepare_will_use(self):
        p = self.interrupted()
        p.job["paused_position"] = [240., 240., 40.6, 50.]
        self.assertEqual(p.get_status(100.)["assumed_position"], [240., 240., 40.6])
        self.assertEqual(p._analysis()["assumed_position"][:3], [240., 240., 40.6])

    def test_homed_axes_use_live_position(self):
        p = self.interrupted()
        self.printer.toolhead.homed_axes = "xy"
        self.printer.toolhead.position = [5., 6., 0., 0.]
        p.cmd_PREPARE(Command())
        self.assertEqual(p.plan["assumed"], [5., 6., 20.6])
        self.assertEqual(p.plan["position_source"], {"x": "live", "y": "live", "z": "recorded"})

    def test_idle_timeout_extended_while_parked_and_restored(self):
        idle = SimpleNamespace(idle_timeout=600.)
        self.printer.objects["idle_timeout"] = idle
        p = self.interrupted()
        p.cmd_PREPARE(Command())
        self.assertEqual(idle.idle_timeout, 3600.)
        p.cmd_RESUME(Command(CLEANED=1, PATH_CLEAR=1))
        self.assertEqual(idle.idle_timeout, 600.)

    def test_idle_timeout_restored_on_discard_and_block(self):
        idle = SimpleNamespace(idle_timeout=600.)
        self.printer.objects["idle_timeout"] = idle
        p = self.interrupted()
        p.cmd_HEAT(Command())
        self.assertEqual(idle.idle_timeout, 3600.)
        p.cmd_DISCARD(Command(CONFIRM=1))
        self.assertEqual(idle.idle_timeout, 600.)
        p = self.interrupted()
        p.cmd_PREPARE(Command())
        p._motor_off()
        self.assertEqual(idle.idle_timeout, 600.)

    def test_resume_heats_bed_before_nozzle_and_restores_chamber(self):
        self.printer.objects["heater_bed"] = SimpleNamespace(get_status=lambda t: {"target": 0.})
        p = self.interrupted()
        p.job["checkpoints"][-1]["heaters"] = {"chamber": 45.}
        p.cmd_PREPARE(Command())
        before = len(self.printer.gcode.commands)
        p.cmd_RESUME(Command(CLEANED=1, PATH_CLEAR=1))
        heat = [c.split()[0] for c in self.printer.gcode.commands[before:]
                if c.split()[0] in ("M140", "M190", "M104", "M109", "SET_HEATER_TEMPERATURE")]
        self.assertEqual(heat, ["M140", "SET_HEATER_TEMPERATURE", "M190", "M104", "M109"])

    def test_heat_command_uses_saved_targets_without_motion(self):
        self.printer.objects["heater_bed"] = SimpleNamespace(get_status=lambda t: {"target": 0.})
        p = self.interrupted()
        before = len(self.printer.gcode.commands)
        p.cmd_HEAT(Command())
        self.assertEqual(self.printer.gcode.commands[before:], ["M140 S60.000", "M104 S210.000"])
        self.assertEqual(self.printer.toolhead.moves, [])

    def test_prime_only_when_parked_and_restores_state(self):
        p = self.interrupted()
        with self.assertRaisesRegex(ValueError, "parking"):
            p.cmd_PRIME(Command(LENGTH=10))
        p.cmd_PREPARE(Command())
        position = self.printer.toolhead.get_position()
        before = len(self.printer.gcode.commands)
        p.cmd_PRIME(Command(LENGTH=10))
        self.assertEqual(self.printer.gcode.commands[before:],
                         ["SAVE_GCODE_STATE NAME=__PHOENIX_PRIME", "M83", "G1 E10.000 F240",
                          "RESTORE_GCODE_STATE NAME=__PHOENIX_PRIME"])
        self.assertEqual(self.printer.toolhead.get_position(), position)

    def test_excluded_object_skips_checkpoint_and_is_restored(self):
        exclude = {"excluded_objects": ["PART_B"], "current_object": "PART_B"}
        self.printer.objects["exclude_object"] = SimpleNamespace(get_status=lambda t: exclude)
        p = self.printing()
        self.printer.sd.file_position = 20
        before = p.job["checkpoints"][-1]
        p._sample(100.)
        self.assertIs(p.job["checkpoints"][-1], before)  # Virtual position not recorded.
        self.assertNotEqual(p.phase, "blocked")
        exclude["current_object"] = "PART_A"
        p._sample(101.)
        self.assertEqual(p.job["checkpoints"][-1]["excluded_objects"], ["PART_B"])
        p.active, p.phase = False, "detected"
        self.printer.sd.active = False
        self.printer.stats["state"] = "standby"
        self.printer.toolhead.homed_axes = ""
        p.checks = {key: True for key in CHECKS}
        p.cmd_PREPARE(Command())
        p.cmd_RESUME(Command(CLEANED=1, PATH_CLEAR=1))
        self.assertIn("EXCLUDE_OBJECT NAME=PART_B", self.printer.gcode.commands)

    def test_prepare_error_reopens_prompt_with_message(self):
        p = self.interrupted()
        p.job["file"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, re.escape("Phoenix %s: G-code file changed" % VERSION)):
            p.cmd_PREPARE(Command(REFRESH=1))
        self.assertIn("// action:prompt_text G-code file changed; recovery refused", self.printer.gcode.lines)
        self.assertTrue(any("Discard recovery" in line for line in self.printer.gcode.lines))

    def test_blocked_prompt_offers_discard(self):
        p = self.interrupted()
        p._block("Motors were disabled")
        p.cmd_PROMPT(Command())
        self.assertTrue(any("PHOENIX_PROMPT DISCARD=1" in line for line in self.printer.gcode.lines))
        p.cmd_PROMPT(Command(DISCARD=1))
        self.assertTrue(any("PHOENIX_DISCARD CONFIRM=1" in line for line in self.printer.gcode.lines))

    def test_startup_homing_is_warned(self):
        self.printer.settings["delayed_gcode autohome"] = {"initial_duration": "1", "gcode": "G28"}
        self.printer.settings["delayed_gcode leds"] = {"initial_duration": "1", "gcode": "SET_LED"}
        risks = self.phoenix._startup_risks()
        self.assertEqual(len(risks), 1)
        self.assertIn("autohome", risks[0])
