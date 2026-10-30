import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.setup import (AUTOSAVE, ConfigTree, ROOT, SSH_ORIGIN, apply_changes,
                           deploy_command, main, plan_config, printer_state)


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.config.mkdir()
        (self.config / "printer.cfg").write_text("[printer]\nkinematics: corexy\n[include macros.cfg]\n")
        (self.config / "macros.cfg").write_text(
            "[gcode_macro PRINT_START]\ngcode:\n    G28\n    M109 S210\n\n"
            "[gcode_macro PRINT_END]\ngcode:\n    M104 S0\n    G1 X0\n")
        (self.config / "moonraker.conf").write_text("[server]\nport: 7125\n")

    def apply(self, **kwargs):
        changes, originals, notes = plan_config(self.config, **kwargs)
        backup = apply_changes(changes, originals, self.root / "backups")
        return backup, notes

    def test_installs_include_force_hooks_and_updater_once(self):
        backup, _ = self.apply(origin=SSH_ORIGIN)
        tree = ConfigTree(self.config / "printer.cfg")
        self.assertIsNotNone(tree.one("phoenix"))
        self.assertIsNotNone(tree.one("force_move"))
        macro = (self.config / "macros.cfg").read_text()
        self.assertLess(macro.index("M109 S210"), macro.index("PHOENIX_START"))
        self.assertLess(macro.index("PHOENIX_END"), macro.index("M104 S0"))
        self.assertIn(SSH_ORIGIN, (self.config / "moonraker.conf").read_text())
        self.assertTrue((backup / "manifest.json").is_file())
        self.assertEqual(plan_config(self.config, origin=SSH_ORIGIN)[0], {})

    def test_existing_force_option_merged_in_included_file(self):
        path = self.config / "macros.cfg"
        path.write_text(path.read_text() + "\n[force_move]\nenable_force_move: False # keep this comment\n")
        self.apply()
        self.assertIn("enable_force_move: True # keep this comment", path.read_text())
        self.assertEqual(len(ConfigTree(self.config / "printer.cfg").sections("force_move")), 1)

    def test_existing_unincluded_phoenix_preserved(self):
        (self.config / "phoenix.cfg").write_text("[phoenix]\npark_x: 42\nprofile_ready: True\n")
        self.apply()
        self.assertIn("park_x: 42", (self.config / "phoenix.cfg").read_text())

    def test_wildcard_does_not_gain_duplicate_include(self):
        (self.config / "printer.cfg").write_text("[include macros.cfg]\n[include phoenix*.cfg]\n")
        self.apply()
        self.assertNotIn("[include phoenix.cfg]", (self.config / "printer.cfg").read_text())
        self.assertEqual(plan_config(self.config)[0], {})

    def test_autosave_block_preserved_and_include_placed_before_it(self):
        path = self.config / "printer.cfg"
        tail = AUTOSAVE + "\n#*# DO NOT EDIT THIS BLOCK\n#*# [extruder]\n#*# pid_Kp = 20\n"
        path.write_text(path.read_text() + tail)
        self.apply()
        self.assertTrue(path.read_text().endswith(tail))
        self.assertLess(path.read_text().index("[include phoenix.cfg]"), path.read_text().index(AUTOSAVE))

    def test_duplicate_force_sections_refuse_all_writes(self):
        path = self.config / "printer.cfg"
        path.write_text(path.read_text() + "[force_move]\nenable_force_move: True\n")
        macros = self.config / "macros.cfg"
        macros.write_text(macros.read_text() + "[force_move]\nenable_force_move: True\n")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            plan_config(self.config)
        self.assertFalse((self.config / "phoenix.cfg").exists())

    def test_include_cycle_rejected(self):
        (self.config / "macros.cfg").write_text("[include printer.cfg]\n")
        with self.assertRaisesRegex(ValueError, "cycle"):
            plan_config(self.config)

    def test_missing_macros_report_action_not_false_success(self):
        (self.config / "macros.cfg").write_text("# No print macros\n")
        _, _, notes = plan_config(self.config)
        self.assertEqual(len([note for note in notes if note.startswith("TODO PHOENIX_")]), 2)

    def test_ambiguous_macros_require_explicit_choice(self):
        path = self.config / "macros.cfg"
        path.write_text(path.read_text() + "[gcode_macro START_PRINT]\ngcode:\n    G28\n")
        _, _, notes = plan_config(self.config)
        self.assertTrue(any("ambiguous" in note for note in notes))
        self.apply(start_macro="START_PRINT")
        tree = ConfigTree(self.config / "printer.cfg")
        p, start, end = tree.one("gcode_macro START_PRINT")
        self.assertIn("PHOENIX_START", tree.text[p][start:end])

    def test_inline_gcode_refuses_guessing(self):
        (self.config / "macros.cfg").write_text("[gcode_macro PRINT_START]\ngcode: G28\n")
        with self.assertRaisesRegex(ValueError, "Inline"):
            plan_config(self.config)

    def test_stale_plan_refuses_overwrite(self):
        changes, originals, _ = plan_config(self.config)
        (self.config / "printer.cfg").write_text("# user edited during setup\n")
        with self.assertRaisesRegex(ValueError, "changed during setup"):
            apply_changes(changes, originals, self.root / "backups")
        self.assertFalse((self.config / "phoenix.cfg").exists())

    def test_backup_contains_exact_original_bytes(self):
        path = self.config / "printer.cfg"
        old = path.read_bytes().replace(b"\n", b"\r\n")
        path.write_bytes(old)
        backup, _ = self.apply()
        entries = json.loads((backup / "manifest.json").read_text())
        record = next(entry for entry in entries if entry["path"] == str(path))
        self.assertEqual(Path(record["backup"]).read_bytes(), old)

    def test_partial_write_failure_rolls_back(self):
        from scripts import setup
        changes, originals, _ = plan_config(self.config)
        real_write = setup.atomic_write
        count = [0]
        def fail_once(path, data):
            if path in changes:
                count[0] += 1
                if count[0] == 2:
                    raise OSError("disk failure")
            real_write(path, data)
        with patch.object(setup, "atomic_write", side_effect=fail_once):
            with self.assertRaises(OSError):
                apply_changes(changes, originals, self.root / "backups")
        for path, original in originals.items():
            if original is None:
                self.assertFalse(path.exists())
            else:
                self.assertEqual(path.read_bytes().decode(), original)

    def test_unknown_state_is_not_assumed_idle(self):
        with patch("scripts.setup.moonraker_call", side_effect=OSError("offline")):
            self.assertEqual(printer_state("http://localhost"), "unknown")
        with patch("scripts.setup.moonraker_call", return_value={"status": {"print_stats": {"state": "paused"}}}):
            self.assertEqual(printer_state("http://localhost"), "paused")

    def test_deploy_key_command_is_repo_scoped_and_host_verification_on(self):
        path = self.root / "key with spaces"
        path.write_text("placeholder")
        command = deploy_command(path)
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("BatchMode=yes", command)
        self.assertIn("IdentitiesOnly=yes", command)

    def test_check_mode_has_no_side_effects_or_network(self):
        klipper = self.root / "klipper"
        (klipper / "klippy/extras").mkdir(parents=True)
        args = ["--check", "--config-dir", str(self.config), "--klipper-dir", str(klipper)]
        with patch("scripts.setup.git", return_value=SSH_ORIGIN) as git_call, patch("builtins.print"):
            self.assertEqual(main(args), 0)
        self.assertEqual(git_call.call_count, 1)
        self.assertFalse((self.config / "phoenix.cfg").exists())
        self.assertEqual(list((klipper / "klippy/extras").iterdir()), [])

    def cli_args(self):
        klipper = self.root / "klipper"
        (klipper / "klippy/extras").mkdir(parents=True)
        return ["--yes", "--config-dir", str(self.config), "--klipper-dir", str(klipper)]

    def test_running_print_refuses_application(self):
        args = self.cli_args()
        with patch("scripts.setup.git", return_value=SSH_ORIGIN), patch("scripts.setup.printer_state", return_value="printing"), patch("builtins.print"):
            with self.assertRaisesRegex(ValueError, "running/paused"):
                main(args)
        self.assertFalse((self.config / "phoenix.cfg").exists())

    def test_private_auth_failure_precedes_any_config_changes(self):
        args = self.cli_args()
        key = self.root / "deploy"
        key.write_text("not a real key")
        def fake_git(repo, *args, **kwargs):
            if args[0] == "ls-remote":
                raise subprocess.CalledProcessError(128, "git")
            return SSH_ORIGIN
        with patch("scripts.setup.git", side_effect=fake_git), patch("scripts.setup.printer_state", return_value="standby"), patch("builtins.print"):
            with self.assertRaisesRegex(ValueError, "Repo access failed"):
                main(args + ["--deploy-key", str(key)])
        self.assertFalse((self.config / "phoenix.cfg").exists())

    def test_unknown_state_never_restarts_even_with_restart_flag(self):
        args = self.cli_args()
        with patch("scripts.setup.git", return_value=SSH_ORIGIN), patch("scripts.setup.printer_state", return_value="unknown"), patch("pathlib.Path.symlink_to"), patch("scripts.setup.moonraker_call") as rpc, patch("builtins.print"):
            self.assertEqual(main(args + ["--restart"]), 0)
        rpc.assert_not_called()
        self.assertTrue((self.config / "phoenix.cfg").exists())

    def test_idle_restart_uses_existing_moonraker_api(self):
        args = self.cli_args()
        with patch("scripts.setup.git", return_value=SSH_ORIGIN), patch("scripts.setup.printer_state", return_value="standby"), patch("pathlib.Path.symlink_to"), patch("scripts.setup.moonraker_call") as rpc, patch("builtins.print"):
            self.assertEqual(main(args + ["--restart"]), 0)
        self.assertEqual(rpc.call_count, 2)
        self.assertEqual(rpc.call_args_list[0].args[2], {"script": "RESTART"})
        self.assertEqual(rpc.call_args_list[1].args[2], {"service": "moonraker"})


if __name__ == "__main__":
    unittest.main()
