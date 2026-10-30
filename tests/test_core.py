import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from klippy.extras.phoenix_core import (Journal, JournalWriter, file_identity,
                                       recovery_plan, verify_file)


def snapshot(z=20., x=100., y=100., offset=12):
    return {"id": "point", "saved_at": 1000., "offset": offset,
            "machine_position": [x, y, z, 50.], "gcode_position": [x, y, z, 50.],
            "layer_z": 20., "z_above_layer": z - 20.,
            "gcode_state": {"absolute_coord": True, "allow_absolute_extrude": True,
                            "base_position": [0., 0., 0., 0.],
                            "last_position": [x, y, z, 50.],
                            "homing_position": [0., 0., 0., 0.],
                            "speed": 100., "speed_factor": 1./60., "extrude_factor": 1.},
            "mesh": {"profile_name": None, "mesh_min": None, "mesh_max": None,
                     "probed_matrix": None, "mesh_matrix": None},
            "extruder_target": 210., "bed_target": 60., "fans": {"fan": 0.4},
            "pressure_advance": 0.035, "smooth_time": 0.04,
            "motion_limits": {"max_velocity": 200., "max_accel": 3000.}}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_roundtrip_and_newest_generation(self):
        store = Journal(self.root)
        store.save({"phase": "recording", "checkpoints": [snapshot()]})
        store.save({"phase": "recording", "checkpoints": [snapshot(21.)]})
        self.assertEqual(Journal(self.root).load()["checkpoints"][0]["machine_position"][2], 21.)

    def test_torn_newest_falls_back_with_flag(self):
        store = Journal(self.root)
        store.save({"point": 1})
        store.save({"point": 2})
        (self.root / "journal-0.json").write_text('{"broken":')
        reopened = Journal(self.root)
        self.assertEqual(reopened.load(), {"point": 1})
        self.assertTrue(reopened.fallback)

    def test_checksum_rejects_modified_payload(self):
        store = Journal(self.root)
        store.save({"point": 1})
        path = self.root / "journal-1.json"
        envelope = json.loads(path.read_text())
        envelope["body"]["payload"]["point"] = 99
        path.write_text(json.dumps(envelope))
        with self.assertRaisesRegex(ValueError, "No valid"):
            Journal(self.root)

    def test_completed_job_cannot_reappear_from_backup(self):
        store = Journal(self.root)
        store.save({"phase": "recording"})
        store.terminal("complete")
        (self.root / ("journal-%d.json" % (store.sequence % 2))).write_text("bad")
        self.assertEqual(Journal(self.root).load()["phase"], "complete")

    def test_motion_fence_survives_reopen(self):
        store = Journal(self.root)
        store.fence_motion()
        self.assertTrue(Journal(self.root).fence.exists())
        store.clear_fence()
        self.assertFalse(store.fence.exists())

    def test_writer_copies_and_flushes_latest(self):
        store = Journal(self.root)
        writer = JournalWriter(store)
        value = {"point": 1}
        writer.submit(value)
        value["point"] = 2
        writer.flush()
        self.assertEqual(store.load(), {"point": 1})

    def test_failed_disk_write_preserves_previous(self):
        store = Journal(self.root)
        store.save({"point": 1})
        with patch("klippy.extras.phoenix_core.os.replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                store.save({"point": 2})
        self.assertEqual(Journal(self.root).load(), {"point": 1})

    def test_utf8_crlf_offsets_and_changed_file(self):
        path = self.root / "parça.gcode"
        prefix = "; Türkçe\r\nG90\r\n".encode()
        path.write_bytes(prefix + b"G1 X10\r\n")
        identity = file_identity(path, self.root)
        self.assertEqual(verify_file(identity, self.root, len(prefix)), path)
        with self.assertRaisesRegex(ValueError, "line boundary"):
            verify_file(identity, self.root, len(prefix)-2)
        path.write_bytes(prefix + b"G1 X11\r\n")
        with self.assertRaisesRegex(ValueError, "changed"):
            verify_file(identity, self.root, len(prefix))

    def test_path_escape_and_eof_are_rejected(self):
        with tempfile.TemporaryDirectory() as other:
            path = Path(other) / "outside.gcode"
            path.write_text("G90\n")
            with self.assertRaises(ValueError):
                file_identity(path, self.root)
        path = self.root / "test.gcode"
        path.write_bytes(b"G90\n")
        with self.assertRaisesRegex(ValueError, "outside"):
            verify_file(file_identity(path, self.root), self.root, 4)


class PlanTests(unittest.TestCase):
    def plan(self, latest=None, selected=None, clearance=5.):
        return recovery_plan(latest or snapshot(20.6, x=130.), selected or snapshot(),
                             clearance, [10., 10.], ([0., 0., 0.], [250., 250., 250.]))

    def test_older_selection_does_not_change_assumed_position(self):
        plan = self.plan()
        self.assertEqual(plan["assumed"], [130., 100., 20.6])
        self.assertEqual(plan["target"], [100., 100., 20.])
        self.assertEqual(plan["above_target"], [100., 100., 25.6])

    def test_hop_target_above_latest_is_cleared(self):
        plan = self.plan(snapshot(20.), snapshot(20.6))
        self.assertEqual(plan["lift"][2], 25.6)

    def test_insufficient_headroom_rejected_not_clamped(self):
        with self.assertRaisesRegex(ValueError, "travel limits"):
            self.plan(snapshot(249.))

    def test_no_diagonal_descent(self):
        plan = self.plan()
        self.assertEqual(plan["above_target"][:2], plan["target"][:2])
        self.assertEqual(plan["park"][2], plan["above_target"][2])

    def test_nonfinite_or_invalid_state_rejected(self):
        for value in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                self.plan(snapshot(value))
        bad = snapshot()
        bad["gcode_state"]["extrude_factor"] = 0
        with self.assertRaises(ValueError):
            self.plan(selected=bad)

    def test_plan_does_not_mutate_snapshot(self):
        point = snapshot()
        before = copy.deepcopy(point)
        self.plan(point, point)
        self.assertEqual(point, before)


if __name__ == "__main__":
    unittest.main()
