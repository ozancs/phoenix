"""Exercise recovery against the real Klipper mesh implementation in CI."""
import ast
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import test_adapter


class MeshRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parents[1]
        candidates = [root / "runtime/klipper/klippy/extras/bed_mesh.py",
                      root / "runtime/klipper-reference/bed_mesh.py"]
        source = next((p for p in candidates if p.exists()), None)
        if source is None:
            raise unittest.SkipTest("Klipper checkout required; exercised in simulation CI")
        tree = ast.parse(source.read_text(encoding="utf-8"))
        tree.body = [node for node in tree.body
                     if not isinstance(node, ast.ImportFrom) or not node.level]
        cls.module = ModuleType("klippy.extras.bed_mesh")
        exec(compile(tree, str(source), "exec"), cls.module.__dict__)

    def setUp(self):
        self.adapter = test_adapter.AdapterTests()
        self.adapter.setUp()
        self.addCleanup(self.adapter.doCleanups)
        self.p = self.adapter.phoenix
        self.addCleanup(patch.stopall)
        patch.dict(sys.modules, {"klippy.extras.bed_mesh": self.module}).start()

    def bed(self, zmesh):
        bed = SimpleNamespace(z_mesh=zmesh, tool_offset=0., installed=[])
        def status(_):
            z = bed.z_mesh
            if z is None:
                return {"profiles": {}}
            params = z.get_mesh_params()
            return dict(profile_name=z.get_profile_name(),
                        mesh_min=[params["min_x"], params["min_y"]],
                        mesh_max=[params["max_x"], params["max_y"]],
                        probed_matrix=z.get_probed_matrix(),
                        mesh_matrix=z.get_mesh_matrix(), profiles={})
        def install(z):
            bed.installed.append(z)
            bed.z_mesh = z
        bed.get_status, bed.set_mesh = status, install
        self.adapter.printer.objects["bed_mesh"] = bed
        return bed

    def recorded(self, algo):
        params = dict(min_x=20., max_x=180., min_y=30., max_y=190.,
                      x_count=4, y_count=4, mesh_x_pps=2, mesh_y_pps=2,
                      algo=algo, tension=.2)
        if algo == "direct":
            params.update(mesh_x_pps=0, mesh_y_pps=0)
        mesh = self.module.ZMesh(params, "adaptive-test")
        mesh.build_mesh([[.0123456789*x*x - .0087654321*y for x in range(4)]
                         for y in range(4)])
        mesh.set_zero_reference(71.3, 82.7)
        bed = self.bed(mesh)
        self.p.target = {"mesh": json.loads(json.dumps(self.p._mesh()))}
        bed.z_mesh = None  # Restart loses the transient adaptive profile.
        return bed, mesh

    def test_adaptive_round_trip_preserves_compensation_without_commands(self):
        for algo in ("lagrange", "bicubic", "direct"):
            with self.subTest(algo=algo):
                bed, original = self.recorded(algo)
                before = list(self.adapter.printer.gcode.commands)
                self.p._restore_mesh()
                self.assertTrue(self.p._mesh_matches())
                for x, y in ((0, 0), (71.3, 82.7), (115.55, 140.7), (250, 250)):
                    self.assertEqual(original.calc_z(x, y), bed.z_mesh.calc_z(x, y))
                self.assertEqual(before, self.adapter.printer.gcode.commands)
                self.assertEqual(self.adapter.printer.toolhead.moves, [])

    def test_corrupt_mesh_refused_before_install(self):
        bed, _ = self.recorded("bicubic")
        self.p.target["mesh"]["raw_mesh_matrix"][0][0] += 1
        with self.assertRaisesRegex(ValueError, "reproduced"):
            self.p._restore_mesh()
        self.assertEqual(bed.installed, [])

    def test_invalid_grid_refused_before_install(self):
        bed, _ = self.recorded("lagrange")
        self.p.target["mesh"]["raw_mesh_matrix"][0].pop()
        with self.assertRaisesRegex(ValueError, "invalid"):
            self.p._restore_mesh()
        self.assertEqual(bed.installed, [])

    def test_old_missing_profile_has_actionable_error(self):
        bed = self.bed(None)
        self.p.target = {"mesh": {"profile_name": "adaptive-lost"}}
        with self.assertRaisesRegex(ValueError, "older recovery record"):
            self.p._restore_mesh()
        self.assertEqual(bed.installed, [])

    def test_legacy_comparison_ignores_new_metadata(self):
        bed, original = self.recorded("bicubic")
        bed.z_mesh = original
        for key in ("mesh_params", "raw_probed_matrix", "raw_mesh_matrix"):
            self.p.target["mesh"].pop(key)
        self.assertTrue(self.p._mesh_matches())
