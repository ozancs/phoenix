import copy
import random
import unittest
from types import SimpleNamespace

import test_adapter
from test_adapter import Command
from klippy.extras.phoenix_core import demo_plan


class DemoTests(unittest.TestCase):
    setUp = test_adapter.AdapterTests.setUp

    def cold(self):
        self.printer.objects["extruder"] = SimpleNamespace(get_status=lambda t: {"target": 0.})

    def start(self):
        self.cold()
        self.phoenix.cmd_DEMO(Command())
        self.phoenix.cmd_DEMO_START(Command(BED_CLEAR=1, CONFIRM=1))

    def checks(self):
        for item in ("position_ok", "path_clear"):
            self.phoenix.cmd_DEMO_CHECK(Command(ITEM=item, VALUE=1))

    def test_opening_demo_never_moves(self):
        self.cold()
        self.phoenix.cmd_DEMO(Command())
        self.assertEqual(self.phoenix.demo["phase"], "ready")
        self.assertEqual(self.printer.toolhead.moves, [])

    def test_unhomed_demo_requests_normal_homing_not_macro_execution(self):
        self.cold()
        self.printer.toolhead.homed_axes = ""
        self.phoenix.cmd_DEMO(Command())
        self.assertTrue(any("First home" in line for line in self.printer.gcode.lines))
        self.assertFalse(any("DEMO_START BED_CLEAR" in line for line in self.printer.gcode.lines))
        with self.assertRaisesRegex(ValueError, "Home the printer"):
            self.phoenix.cmd_DEMO_START(Command(BED_CLEAR=1, CONFIRM=1))
        self.assertEqual(self.printer.toolhead.moves, [])

    def test_hot_targets_and_real_recovery_block_demo(self):
        self.phoenix.cmd_DEMO(Command())
        self.assertTrue(any("Turn off heater targets" in line for line in self.printer.gcode.lines))
        self.assertFalse(any("DEMO_START BED_CLEAR" in line for line in self.printer.gcode.lines))
        with self.assertRaisesRegex(ValueError, "heater targets"):
            self.phoenix.cmd_DEMO_START(Command(BED_CLEAR=1, CONFIRM=1))
        self.cold()
        self.phoenix.phase = "detected"
        with self.assertRaisesRegex(ValueError, "interrupted print"):
            self.phoenix.cmd_DEMO(Command())

    def test_demo_needs_empty_bed_confirmation(self):
        self.cold()
        self.phoenix.cmd_DEMO(Command())
        with self.assertRaisesRegex(ValueError, "bed and travel path"):
            self.phoenix.cmd_DEMO_START(Command(CONFIRM=1))
        self.assertEqual(self.printer.toolhead.moves, [])

    def test_full_demo_returns_to_same_point_and_restores_fan(self):
        changes = []
        dwells = []
        self.printer.toolhead.dwell = dwells.append
        driver = SimpleNamespace(set_speed_from_command=lambda speed: changes.append(speed))
        self.printer.objects["fan"] = SimpleNamespace(fan=driver, get_status=lambda t: {"speed": .35})
        self.start()
        origin = list(self.printer.toolhead.position)
        journal_before = self.phoenix.journal.load()
        self.checks()
        self.phoenix.cmd_DEMO_RECOVER(Command(CONFIRM=1))
        self.assertEqual(self.phoenix.demo["phase"], "complete")
        self.assertEqual(self.printer.toolhead.position, origin)
        self.assertEqual(changes, [1., 0., 1., 0., 1., 0., .35])
        self.assertEqual(self.phoenix.journal.load(), journal_before)
        self.assertIsNone(self.printer.sd.loaded)
        self.assertFalse(self.printer.sd.active)
        self.assertFalse(self.phoenix.active)
        self.assertEqual(self.printer.gcode.commands, ["G28 X Y"])
        self.assertEqual(dwells, [2., 2., 1., 2., 1., 2., 1.])
        for message in ("Lifting", "Homing X and Y", "Moving to park", "Simulating warm-up", "Part fan", "Returning"):
            self.assertTrue(any(message in line for line in self.printer.gcode.lines), message)
        self.assertTrue(all(move[0][3] == origin[3] for move in self.printer.toolhead.moves))

    def test_position_change_blocks_demo_return(self):
        self.start()
        self.checks()
        self.printer.toolhead.position[0] += 1
        before = len(self.printer.toolhead.moves)
        with self.assertRaisesRegex(ValueError, "position changed"):
            self.phoenix.cmd_DEMO_RECOVER(Command(CONFIRM=1))
        self.assertEqual(len(self.printer.toolhead.moves), before)

    def test_demo_exit_does_not_move_or_alter_setup(self):
        self.start()
        before = copy.deepcopy(self.printer.toolhead.moves)
        park = list(self.phoenix.park)
        self.phoenix.cmd_DEMO_EXIT(Command())
        self.assertEqual(self.phoenix.demo["phase"], "off")
        self.assertEqual(self.printer.toolhead.moves, before)
        self.assertEqual(self.phoenix.park, park)

    def test_motor_disable_blocks_return(self):
        self.start()
        self.checks()
        self.phoenix._motor_off()
        self.assertEqual(self.phoenix.demo["phase"], "blocked")
        with self.assertRaises(ValueError):
            self.phoenix.cmd_DEMO_RECOVER(Command(CONFIRM=1))

    def test_setup_actions_disabled_while_demo_open(self):
        self.cold()
        self.phoenix.cmd_DEMO(Command())
        self.assertFalse(self.phoenix._setup_status()["can_edit"])
        with self.assertRaisesRegex(ValueError, "Finish the Phoenix demo"):
            self.phoenix.cmd_SETUP_PROFILE(Command(X=10, Y=10, CONFIRM=1))

    def test_demo_plan_with_varied_and_negative_limits(self):
        rng = random.Random(42)
        for _ in range(200):
            low = [rng.uniform(-100, 0) for _ in range(3)]
            high = [v + rng.uniform(2, 500) for v in low]
            current = [rng.uniform(a, b) for a, b in zip(low, high)]
            plan = demo_plan(current, (low, high), 5.)
            for point in plan.values():
                for i in range(3):
                    self.assertGreaterEqual(point[i], low[i])
                    self.assertLessEqual(point[i], high[i])
            self.assertEqual(plan["origin"][:2], plan["above_origin"][:2])
        with self.assertRaises(ValueError):
            demo_plan([0, 0, 0], ([0, 0, 0], [100, 100, 0]), 5.)

    def test_fan_error_restores_previous_speed_and_prevents_return(self):
        changes = []
        def speed(value):
            changes.append(value)
            if len(changes) == 2:
                raise ValueError("Fan unavailable")
        self.printer.objects["fan"] = SimpleNamespace(
            fan=SimpleNamespace(set_speed_from_command=speed), get_status=lambda t: {"speed": .2})
        self.start()
        self.checks()
        with self.assertRaisesRegex(ValueError, "Fan unavailable"):
            self.phoenix.cmd_DEMO_RECOVER(Command(CONFIRM=1))
        self.assertEqual(changes[-1], .2)
        self.assertEqual(self.phoenix.demo["phase"], "blocked")
        self.assertEqual(self.printer.toolhead.position[:3], self.phoenix.demo["plan"]["park"])

    def test_upper_z_limit_does_not_generate_out_of_range_lift(self):
        self.printer.toolhead.position = [50., 50., 250., 0.]
        self.start()
        self.assertTrue(all(move[0][2] <= 250 for move in self.printer.toolhead.moves))

    def test_demo_uses_reviewed_park_and_homes_before_parking(self):
        self.phoenix.profile_ready = True
        self.phoenix.park = [210., 180.]
        self.start()
        self.assertEqual(self.phoenix.demo["plan"]["park"][:2], [210., 180.])
        def homing(script):
            self.assertEqual(script, "G28 X Y")
            self.assertEqual(self.printer.toolhead.position[:3], self.phoenix.demo["plan"]["lift"])
            self.phoenix._homing_begin(SimpleNamespace(get_axes=lambda: [0, 1]), [])
            self.printer.toolhead.position[:2] = [0., 0.]
        self.printer.gcode.on_command = homing
        self.checks()
        self.phoenix.cmd_DEMO_RECOVER(Command(CONFIRM=1))
        self.assertEqual(self.phoenix.demo["phase"], "complete")
        self.assertTrue(any(move[0][:2] == [210., 180.] for move in self.printer.toolhead.moves))

    def test_invalid_saved_park_rejected_before_motion(self):
        self.phoenix.profile_ready = True
        self.phoenix.park = [999., 1.]
        self.cold()
        with self.assertRaisesRegex(ValueError, "outside travel limits"):
            self.phoenix.cmd_DEMO(Command())
        self.assertEqual(self.printer.toolhead.moves, [])

    def test_custom_homing_defaults_to_existing_position_and_completes(self):
        self.start()
        self.checks()
        self.printer.settings["homing_override"] = {"gcode": "G28"}
        self.phoenix.cmd_DEMO(Command())
        self.assertTrue(any("Continue using current home" in line for line in self.printer.gcode.lines))
        self.phoenix.cmd_DEMO_RECOVER(Command(CONFIRM=1))
        self.assertEqual(self.printer.gcode.commands, [])
        self.assertEqual(self.phoenix.demo["phase"], "complete")
        self.assertEqual(self.phoenix.demo["home_mode"], "keep")
        self.assertEqual(self.printer.toolhead.position[:3], self.phoenix.demo["plan"]["origin"])

    def test_keep_position_option_works_without_custom_homing(self):
        self.start()
        self.checks()
        self.phoenix.cmd_DEMO_RECOVER(Command(CONFIRM=1, HOME_MODE="KEEP"))
        self.assertEqual(self.printer.gcode.commands, [])
        self.assertEqual(self.phoenix.demo["phase"], "complete")

    def test_custom_homing_explicit_xy_rejected_without_motion(self):
        self.start()
        self.checks()
        self.printer.settings["gcode_macro g28"] = {"gcode": "G28.1"}
        before = copy.deepcopy(self.printer.toolhead.moves)
        with self.assertRaisesRegex(ValueError, "HOME_MODE=KEEP"):
            self.phoenix.cmd_DEMO_RECOVER(Command(CONFIRM=1, HOME_MODE="XY"))
        self.assertEqual(self.printer.toolhead.moves, before)
        self.assertEqual(self.printer.gcode.commands, [])

    def test_keep_mode_cannot_bypass_missing_home_or_moved_position(self):
        self.start()
        self.checks()
        self.printer.toolhead.homed_axes = "xy"
        with self.assertRaisesRegex(ValueError, "Home the printer"):
            self.phoenix.cmd_DEMO_RECOVER(Command(CONFIRM=1, HOME_MODE="KEEP"))
        self.printer.toolhead.homed_axes = "xyz"
        self.printer.toolhead.position[0] += 1
        with self.assertRaisesRegex(ValueError, "position changed"):
            self.phoenix.cmd_DEMO_RECOVER(Command(CONFIRM=1, HOME_MODE="KEEP"))

    def test_z_homing_during_demo_homing_blocks_return(self):
        self.start()
        self.checks()
        self.printer.gcode.on_command = lambda _: self.phoenix._homing_begin(
            SimpleNamespace(get_axes=lambda: [2]), [])
        with self.assertRaisesRegex(ValueError, "must not home Z"):
            self.phoenix.cmd_DEMO_RECOVER(Command(CONFIRM=1))
        self.assertEqual(self.phoenix.demo["phase"], "blocked")

    def test_setup_status_tracks_homing_then_saves_current_xy(self):
        self.printer.toolhead.homed_axes = ""
        self.assertFalse(self.phoenix.get_status(100.)["setup"]["can_use_current"])
        self.printer.toolhead.homed_axes = "xyz"
        self.printer.toolhead.position[:2] = [210., 180.]
        self.assertTrue(self.phoenix.get_status(101.)["setup"]["can_use_current"])
        self.phoenix.cmd_SETUP_PROFILE(Command(USE_CURRENT=1, CONFIRM=1))
        self.assertEqual(self.phoenix.get_status(102.)["setup"]["park"], [210., 180.])
        self.assertTrue(self.phoenix.get_status(102.)["setup"]["profile_ready"])
