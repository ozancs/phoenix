import copy
import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from klippy.extras.phoenix_core import (Journal, assess_evidence, capture_motion_evidence,
                                       infer_homing_origin, match_segments, phase_position,
                                       trajectory_envelope, verify_file, file_identity, canonical, digest)
from research.gcode_index import index_gcode
import test_adapter
from test_adapter import Command


def phase(**changes):
    p = {"time": 0., "duration": 2., "velocity": 0., "acceleration": 2.,
         "start": [0., 0., 0., 0.], "direction": [1., 0., 0., .1], "segment": 0}
    p.update(changes)
    return p


class AnalysisTests(unittest.TestCase):
    def test_acceleration_not_constant_speed(self):
        self.assertEqual(phase_position(phase(), 1), [1., 0., 0., .1])
        self.assertEqual(phase_position(phase(), 2), [4., 0., 0., .4])

    def test_reversal_extremum_included(self):
        result = trajectory_envelope([phase(velocity=2., acceleration=-2.)], [0., 2.])
        self.assertEqual(result["planned_bounds"][0], [0., 1.])
        self.assertIsNone(result["physical_bounds"])

    def test_gap_and_unknown_tail_never_extrapolated(self):
        for window in ([-1, 1], [1, 3]):
            self.assertIsNone(trajectory_envelope([phase()], window)["estimate"])
        self.assertFalse(trajectory_envelope([phase(duration=.5), phase(time=1)], [0., 2.])["complete"])

    def test_clock_uncertainty_expands_envelope(self):
        narrow = trajectory_envelope([phase()], [.5, 1.])
        wider = trajectory_envelope([phase()], [.5, 1.], .1, [.02]*4)
        self.assertLess(wider["planned_bounds"][0][0], narrow["planned_bounds"][0][0])
        self.assertGreater(wider["physical_bounds"][0][1], wider["planned_bounds"][0][1])

    def test_invalid_values_rejected(self):
        for p in (phase(velocity=float('nan')), phase(duration=-1), phase(direction=[1])):
            with self.assertRaises(ValueError):
                trajectory_envelope([p], [0, 1])
        with self.assertRaises(ValueError):
            trajectory_envelope([phase()], [1, 0])

    def test_duplicate_coordinates_preserve_ordered_candidates(self):
        result = index_gcode(b"G1 X10 E1\nG1 X0 E2\nG1 X10 E3\n")
        bounds = [[4.9, 5.1], [0, 0], [0, 0]]
        found = match_segments(result["segments"], bounds)
        self.assertEqual([m["id"] for m in found], [0, 1, 2])
        found = match_segments(result["segments"], bounds, sequence_range=[1, 2], direction=[1, 0, 0])
        self.assertEqual([m["id"] for m in found], [2])

    def test_index_offsets_modes_g92_and_hops(self):
        data = "; ü\r\nG90\nM82\nG1 X10 E1\nG92 E0\nG91\nG1 X1 E.5\nG90\nM83\nG1 Z.8\nG1 E-1\n".encode()
        index = index_gcode(data)
        self.assertFalse(index["unsupported"])
        self.assertEqual(index["segments"][1]["end"], [11., 0., 0., 1.5])
        self.assertEqual(index["segments"][-1]["kind"], "retract")
        self.assertTrue(index["segments"][-2]["z_hop_possible"])
        for s in index["segments"]:
            self.assertTrue(data[s["offset"]:s["end_offset"]].startswith(b"G1"))

    def test_unknown_transforms_taint_following_index(self):
        result = index_gcode(b"G1 X1\nSET_GCODE_OFFSET Z=1\nG1 X2\nG2 X3 I1\n")
        self.assertEqual(len(result["unsupported"]), 2)
        self.assertTrue(result["segments"][0]["state_supported"])
        self.assertFalse(result["segments"][1]["state_supported"])

    def test_corexy_inverse_requires_both_motors(self):
        # From origin (20,30,10) to homed (0,0,0): A delta=50, B=-10.
        origin = infer_homing_origin([0, 0, 0], [4000, -800, 4000], [.0125, .0125, .0025], "corexy")
        self.assertEqual(origin, [20., 30., 10.])
        cartesian_wrong = infer_homing_origin([0, 0, 0], [4000, -800, 4000], [.0125, .0125, .0025])
        self.assertNotEqual(cartesian_wrong, origin)

    def test_high_requires_all_evidence_and_never_auto_resumes(self):
        keys = ("timing_complete", "stop_window_known", "physical_verified", "extrusion_verified",
                "state_verified", "unique_segment", "within_tolerance")
        evidence = dict.fromkeys(keys, True)
        self.assertEqual(assess_evidence(evidence)["class"], "high")
        self.assertFalse(assess_evidence(evidence)["automatic_resume"])
        for key in keys:
            reduced = dict(evidence, **{key: False})
            self.assertEqual(assess_evidence(reduced)["class"], "uncertain")
        self.assertEqual(assess_evidence(dict(evidence, z_dropped=True))["class"], "unsafe")

    def test_interrupted_atomic_replace_preserves_old_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            journal.save({"phase": "recording", "checkpoints": [1]})
            with patch("klippy.extras.phoenix_core.os.replace", side_effect=OSError("power cut")):
                with self.assertRaises(OSError):
                    journal.save({"phase": "recording", "checkpoints": [2]})
            self.assertEqual(Journal(directory).load()["checkpoints"], [1])

    def test_corruption_fallback_and_total_loss(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            journal.save({"checkpoints": [1]})
            journal.save({"checkpoints": [2]})
            (Path(directory)/"journal-0.json").write_bytes(b'{"body":')
            fallback = Journal(directory)
            self.assertTrue(fallback.fallback)
            self.assertEqual(fallback.load()["checkpoints"], [1])
            (Path(directory)/"journal-1.json").write_bytes(b'bad')
            with self.assertRaisesRegex(ValueError, "No valid"):
                Journal(directory)

    def test_same_length_gcode_mutation_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"print.gcode"
            path.write_bytes(b"G1 X1\nG1 X2\n")
            identity = file_identity(path, directory)
            path.write_bytes(b"G1 X9\nG1 X2\n")
            with self.assertRaisesRegex(ValueError, "changed"):
                verify_file(identity, directory, 6)

    def test_checksummed_but_invalid_generation_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            journal.save({"checkpoints": [1]})
            body = {"schema": 1, "sequence": "wrong-type", "payload": {"checkpoints": [2]}}
            (Path(directory)/"journal-0.json").write_bytes(canonical({"body": body, "sha256": digest(body)}))
            self.assertEqual(Journal(directory).load()["checkpoints"], [1])

    def test_capture_failure_is_explicit_not_a_physical_position(self):
        printer = SimpleNamespace(lookup_object=lambda name, default: None)
        result = capture_motion_evidence(printer, 100)
        self.assertIn("unavailable", result)
        self.assertFalse(result["stop_time_known"])

    def test_capture_bounded_copy_from_c_objects(self):
        row = SimpleNamespace(print_time=9., move_t=2., start_v=1., accel=0.,
                              start_x=0., start_y=0., start_z=0., x_r=1., y_r=0., z_r=0.)
        ffi = SimpleNamespace(new=lambda decl: [row]*128)
        calls = []
        lib = SimpleNamespace(trapq_extract_old=lambda trap, data, count, low, high:
                              calls.append(count) or 1)
        objects = {"motion_report": SimpleNamespace(dtrapqs={"toolhead": SimpleNamespace(trapq=object())}),
                   "mcu": SimpleNamespace(estimated_print_time=lambda t: 10.)}
        printer = SimpleNamespace(lookup_object=lambda name, default: objects.get(name, default))
        with patch.dict("sys.modules", {"chelper": SimpleNamespace(get_ffi=lambda: (ffi, lib))}):
            result = capture_motion_evidence(printer, 100)
        row.start_x = 99.
        self.assertEqual(result["tracks"]["toolhead"]["phases"][0]["start"][0], 0.)
        self.assertEqual(calls, [32])
        self.assertIsNone(result["clock_error_bound"])


class AnalysisAdapterTests(unittest.TestCase):
    setUp = test_adapter.AdapterTests.setUp
    interrupted = test_adapter.AdapterTests.interrupted

    def test_analyze_is_read_only_and_never_claims_measured_position(self):
        self.printer.sd.from_sd = True
        self.phoenix.cmd_START(Command())
        self.phoenix.job["checkpoints"].append(self.phoenix._snapshot())
        cmd = Command(DETAILS=1)
        before = list(self.printer.gcode.commands)
        self.phoenix.cmd_ANALYZE(cmd)
        analysis = json.loads(cmd.messages[-1])
        self.assertEqual(analysis["confidence"]["class"], "uncertain")
        self.assertIsNone(analysis["estimated_stop_position"])
        self.assertEqual(self.printer.gcode.commands, before)
        self.assertEqual(self.printer.toolhead.moves, [])

    def test_missing_motion_api_does_not_break_checkpoint(self):
        snapshot = self.phoenix._snapshot()
        self.assertIn("unavailable", snapshot["motion_evidence"])
        self.assertEqual(snapshot["offset"], self.printer.sd.file_position)

    def test_rejected_file_visible_as_unsafe_without_motion(self):
        p = self.interrupted()
        self.printer.sd.path.write_bytes(b"G1 X99\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            p.cmd_PREPARE(Command())
        self.assertEqual(p._analysis()["confidence"]["class"], "unsafe")
        self.assertIn("changed", p._analysis()["validation_error"])
        self.assertEqual(self.printer.toolhead.moves, [])

    def test_raw_tracks_are_pruned_only_from_older_checkpoints(self):
        p = self.interrupted()
        p.active, self.printer.sd.active = True, True
        p.job["checkpoints"][0]["motion_evidence"] = {"tracks": {"toolhead": {}}, "session_id": "old"}
        p._sample(100.)
        p.writer.flush()
        old = p.job["checkpoints"][0]["motion_evidence"]
        self.assertNotIn("tracks", old)
        self.assertTrue(old["tracks_pruned"])
        self.assertEqual(old["session_id"], "old")

    def test_terminal_journal_does_not_show_unsafe_recovery(self):
        self.phoenix.job = {"phase": "complete", "checkpoints": []}
        self.phoenix.phase = "idle"
        self.assertFalse(self.phoenix._analysis()["applicable"])
        self.assertNotEqual(self.phoenix._analysis()["confidence"]["class"], "unsafe")
