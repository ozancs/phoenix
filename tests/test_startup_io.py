"""Startup I/O threading and lifecycle tests; not a real MCU latency test."""
import threading
import unittest
from unittest.mock import patch

import test_adapter
from test_adapter import Command
import klippy.extras.phoenix_core as core


class StartupIOTests(unittest.TestCase):
    setUp = test_adapter.AdapterTests.setUp
    interrupted = test_adapter.AdapterTests.interrupted

    def test_hash_drain_and_fsync_run_off_main_with_callback_progress(self):
        p = self.interrupted()
        self.printer.sd.from_sd = True
        entered, released = threading.Event(), threading.Event()
        trace, callbacks = [], []
        identity, fsync, flush = core.file_identity, core.os.fsync, p.writer.flush

        def hash_file(*args):
            trace.append(("hash", threading.current_thread().name))
            entered.set()
            if not released.wait(2.):
                raise RuntimeError("Reactor did not progress during I/O")
            return identity(*args)

        def sync(fd):
            trace.append(("fsync", threading.current_thread().name))
            return fsync(fd)

        def drain():
            trace.append(("drain", threading.current_thread().name))
            return flush()

        def tick():
            callbacks.append(True)
            self.assertTrue(p.busy)
            self.assertFalse(p.active)
            self.assertEqual(p.phase, "starting")
            self.assertEqual(p.job["id"], "job1")
            p._sample(100.)  # Must not publish the old job during the transaction.
            if entered.is_set():
                released.set()

        self.printer.reactor.on_wait = tick
        with patch.object(core, "file_identity", hash_file), patch.object(core.os, "fsync", sync), patch.object(p.writer, "flush", drain):
            p.cmd_START(Command())
        self.assertTrue(callbacks)
        self.assertTrue(all(name == "phoenix-startup-io" for _, name in trace))
        self.assertEqual({stage for stage, _ in trace}, {"hash", "fsync", "drain"})
        self.assertEqual(p.phase, "recording")
        self.assertEqual(p.journal.load()["id"], p.job["id"])
        self.assertFalse(p.busy)
        self.assertIn("file_hash_ms", p.startup_timing)
        self.assertIn("old_recovery_close_ms", p.startup_timing)
        self.assertIn("journal_slot_2_ms", p.startup_timing)
        self.assertEqual(self.printer.gcode.commands, [])

    def test_failed_write_never_activates_recording(self):
        self.printer.sd.from_sd = True
        with patch.object(core.os, "fsync", side_effect=OSError("disk unavailable")):
            with self.assertRaisesRegex(ValueError, "disk unavailable"):
                self.phoenix.cmd_START(Command())
        self.assertFalse(self.phoenix.active)
        self.assertFalse(self.phoenix.busy)
        self.assertEqual(self.phoenix.phase, "blocked")
        self.assertIn("error", self.phoenix.startup_timing)
        self.assertEqual(self.printer.toolhead.moves, [])

    def run_changed_context(self, change, message):
        self.printer.sd.from_sd = True
        release = threading.Event()
        identity = core.file_identity

        def slow_hash(*args):
            if not release.wait(2.):
                raise RuntimeError("test completion timeout")
            return identity(*args)

        def tick():
            change()
            release.set()

        self.printer.reactor.on_wait = tick
        with patch.object(core, "file_identity", slow_hash):
            with self.assertRaisesRegex(ValueError, message):
                self.phoenix.cmd_START(Command())
        self.assertFalse(self.phoenix.active)
        self.assertFalse(self.phoenix.busy)

    def test_shutdown_while_hashing_cannot_reactivate_recording(self):
        self.run_changed_context(self.phoenix._shutdown, "shut down")

    def test_replaced_source_while_hashing_is_rejected(self):
        self.run_changed_context(lambda: setattr(self.printer.sd, "current_file", object()), "file changed")

    def test_duplicate_hook_does_not_repeat_io(self):
        self.printer.sd.from_sd = True
        self.phoenix.cmd_START(Command())
        with patch.object(core, "file_identity", side_effect=AssertionError("duplicate hash")):
            self.phoenix.cmd_START(Command())
