"""Fault injection at the actual registered print hooks and reactor boundary."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_adapter
from test_adapter import Command


class PrintIsolationTests(unittest.TestCase):
    setUp = test_adapter.AdapterTests.setUp
    interrupted = test_adapter.AdapterTests.interrupted

    def start(self):
        self.printer.sd.from_sd = True
        self.printer.gcode.handlers["PHOENIX_START"](Command())
        return self.phoenix

    def test_bad_layer_parameters_do_not_escape_registered_hook(self):
        p = self.start()
        for value in ("bad", "nan", "inf", -1, None):
            with self.subTest(value=value):
                self.printer.gcode.handlers["PHOENIX_LAYER"](Command(Z=value))
                self.assertFalse(p.active)
                self.assertIn("Invalid PHOENIX_LAYER", p.error)
        self.assertEqual(self.printer.toolhead.moves, [])

    def test_recovery_and_setup_commands_in_print_are_noops(self):
        p = self.start()
        for name, handler in self.printer.gcode.handlers.items():
            if name in ("PHOENIX_START", "PHOENIX_END", "PHOENIX_LAYER",
                        "PHOENIX_STATUS", "PHOENIX_ANALYZE"):
                continue
            handler(Command())
            self.assertTrue(p.active, name)
        self.assertEqual(self.printer.toolhead.moves, [])
        self.assertEqual(self.printer.gcode.commands, [])

    def test_secondary_cleanup_failures_cannot_escape_sample_timer(self):
        p = self.start()
        with patch.object(p, "_compatible", side_effect=RuntimeError("capture failed")), \
             patch.object(p, "_restore_idle_timeout", side_effect=RuntimeError("timeout failed")), \
             patch.object(p, "_event", side_effect=RuntimeError("event failed")), \
             patch.object(self.printer.gcode, "respond_info", side_effect=RuntimeError("console failed")), \
             self.assertLogs(level="ERROR"):
            result = p._sample(100.)
        self.assertEqual(result, 100. + p.interval)
        self.assertFalse(p.active)
        self.assertEqual(p.journal.load()["phase"], "recovery_off")

    def test_manual_observer_failure_still_calls_original_handler(self):
        p = self.start()
        calls = []
        self.printer.gcode.mux_commands = {"MANUAL_STEPPER": ("STEPPER", {"aux": calls.append})}
        p._install_runtime_guards()
        cmd = Command()
        with patch.object(cmd, "get", side_effect=RuntimeError("observer failed")):
            self.printer.gcode.mux_commands["MANUAL_STEPPER"][1]["aux"](cmd)
        self.assertEqual(calls, [cmd])
        self.assertFalse(p.active)

    def test_original_manual_safety_error_is_not_swallowed(self):
        p = self.start()
        def original(cmd):
            raise ValueError("Move out of range")
        self.printer.gcode.mux_commands = {"MANUAL_STEPPER": ("STEPPER", {"aux": original})}
        p._install_runtime_guards()
        with self.assertRaisesRegex(ValueError, "Move out of range"):
            self.printer.gcode.mux_commands["MANUAL_STEPPER"][1]["aux"](Command(MOVE=999))

    def test_skew_observer_runtime_error_does_not_escape_original_command(self):
        p = self.start()
        calls = []
        self.printer.gcode.ready_gcode_handlers = {"SET_SKEW": calls.append}
        p._install_runtime_guards()
        cmd = Command()
        with patch.object(p, "_compatible", side_effect=RuntimeError("status failed")):
            self.printer.gcode.ready_gcode_handlers["SET_SKEW"](cmd)
        self.assertEqual(calls, [cmd])
        self.assertFalse(p.active)

    def test_end_disk_work_runs_off_reactor_without_waiting_for_motion(self):
        p = self.start()
        trace = []
        original = p.journal.terminal
        def terminal(phase):
            trace.append(threading.current_thread().name)
            original(phase)
        with patch.object(p.journal, "terminal", terminal), \
             patch.object(self.printer.toolhead, "wait_moves", side_effect=AssertionError("motion drain")):
            self.printer.gcode.handlers["PHOENIX_END"](Command())
        self.assertEqual(trace, ["phoenix-startup-io"])
        self.assertEqual(p.phase, "idle")

    def test_capture_does_not_dispatch_gcode_or_command_error_events(self):
        p = self.phoenix
        with patch.object(self.printer.gcode, "run_script_from_command", side_effect=AssertionError("dispatch")):
            saved = p._save_gcode_state()
        self.assertEqual(saved, self.printer.move.state)
        self.assertIsNot(saved, self.printer.move.state)

    def test_new_print_homing_does_not_trigger_old_recovery_guard(self):
        p = self.interrupted()
        p.phase = "prepared"
        self.printer.sd.from_sd = True
        p._homing_begin(SimpleNamespace(get_axes=lambda: [0, 1, 2]), [])
        self.assertEqual(p.phase, "idle")
        self.assertEqual(p.journal.load()["phase"], "discarded")
        self.assertTrue(self.start().active)

    def test_z_homing_during_actual_recovery_still_refused(self):
        p = self.interrupted()
        p.phase = "prepared"
        with self.assertRaisesRegex(ValueError, "Z homing"):
            p._homing_begin(SimpleNamespace(get_axes=lambda: [2]), [])
        self.assertEqual(p.phase, "blocked")
