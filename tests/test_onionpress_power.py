"""Tests for src/onionpress/power.py — CaffeineManager."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.power import CaffeineManager


def _make_manager(config_value="normal"):
    """Build a CaffeineManager with a temp app_support and a captured log."""
    tmpdir = tempfile.mkdtemp()
    logs = []
    cfg = mock.Mock(return_value=config_value)
    cm = CaffeineManager(tmpdir, logs.append, cfg)
    return cm, logs, tmpdir, cfg


class TestIsRunning(unittest.TestCase):
    def test_false_when_never_started(self):
        cm, *_ = _make_manager()
        self.assertFalse(cm.is_running())

    def test_true_when_process_alive(self):
        cm, *_ = _make_manager()
        proc = mock.Mock()
        proc.poll.return_value = None
        cm._process = proc
        self.assertTrue(cm.is_running())

    def test_false_when_process_exited(self):
        cm, *_ = _make_manager()
        proc = mock.Mock()
        proc.poll.return_value = 0
        cm._process = proc
        self.assertFalse(cm.is_running())


class TestStart(unittest.TestCase):
    def test_normal_mode_is_noop(self):
        cm, logs, _, _ = _make_manager(config_value="normal")
        with mock.patch("onionpress.power.subprocess.Popen") as popen:
            cm.start()
        popen.assert_not_called()
        self.assertIsNone(cm._process)

    def test_on_battery_uses_s_flag(self):
        cm, logs, tmpdir, _ = _make_manager(config_value="on-battery")
        fake_proc = mock.Mock(pid=4242)
        fake_proc.poll.return_value = None
        with mock.patch("onionpress.power.subprocess.Popen",
                        return_value=fake_proc) as popen:
            cm.start()
        popen.assert_called_once()
        args = popen.call_args[0][0]
        self.assertEqual(args, ["caffeinate", "-s"])
        self.assertTrue(any("AC power" in l for l in logs))
        # PID file was written
        with open(os.path.join(tmpdir, "caffeinate.pid")) as f:
            self.assertEqual(f.read(), "4242")

    def test_never_uses_i_flag(self):
        cm, _, _, _ = _make_manager(config_value="never")
        fake_proc = mock.Mock(pid=100)
        fake_proc.poll.return_value = None
        with mock.patch("onionpress.power.subprocess.Popen",
                        return_value=fake_proc) as popen:
            cm.start()
        args = popen.call_args[0][0]
        self.assertEqual(args, ["caffeinate", "-i"])

    def test_legacy_yes_means_on_battery(self):
        cm, _, _, _ = _make_manager(config_value="yes")
        fake_proc = mock.Mock(pid=1, poll=mock.Mock(return_value=None))
        with mock.patch("onionpress.power.subprocess.Popen",
                        return_value=fake_proc) as popen:
            cm.start()
        self.assertEqual(popen.call_args[0][0], ["caffeinate", "-s"])

    def test_legacy_no_means_normal(self):
        cm, _, _, _ = _make_manager(config_value="no")
        with mock.patch("onionpress.power.subprocess.Popen") as popen:
            cm.start()
        popen.assert_not_called()

    def test_idempotent_when_already_running(self):
        cm, _, _, _ = _make_manager(config_value="never")
        existing = mock.Mock()
        existing.poll.return_value = None
        cm._process = existing
        with mock.patch("onionpress.power.subprocess.Popen") as popen:
            cm.start()
        popen.assert_not_called()

    def test_popen_failure_is_logged_not_raised(self):
        cm, logs, _, _ = _make_manager(config_value="never")
        with mock.patch("onionpress.power.subprocess.Popen",
                        side_effect=OSError("ENOENT")):
            cm.start()  # should not raise
        self.assertTrue(any("Failed to start caffeinate" in l for l in logs))


class TestStop(unittest.TestCase):
    def test_noop_when_not_running(self):
        cm, logs, _, _ = _make_manager()
        cm.stop()  # should not raise
        self.assertEqual(logs, [])

    def test_terminate_path(self):
        cm, logs, tmpdir, _ = _make_manager()
        proc = mock.Mock()
        cm._process = proc
        # Pre-create a PID file to confirm it's cleaned up
        with open(os.path.join(tmpdir, "caffeinate.pid"), "w") as f:
            f.write("123")
        cm.stop()
        proc.terminate.assert_called_once()
        self.assertIsNone(cm._process)
        self.assertFalse(os.path.exists(os.path.join(tmpdir, "caffeinate.pid")))
        self.assertTrue(any("Stopped caffeinate" in l for l in logs))

    def test_falls_back_to_kill_on_terminate_failure(self):
        cm, logs, _, _ = _make_manager()
        proc = mock.Mock()
        proc.terminate.side_effect = OSError("EPERM")
        cm._process = proc
        cm.stop()
        proc.kill.assert_called_once()
        self.assertIsNone(cm._process)


class TestCleanupStale(unittest.TestCase):
    def test_no_pid_file_is_noop(self):
        cm, _, _, _ = _make_manager()
        cm._cleanup_stale()  # should not raise

    def test_kills_orphan_caffeinate(self):
        cm, logs, tmpdir, _ = _make_manager()
        with open(os.path.join(tmpdir, "caffeinate.pid"), "w") as f:
            f.write("9999")
        ps_result = mock.Mock(returncode=0, stdout="caffeinate\n")
        with mock.patch("onionpress.power.subprocess.run",
                        return_value=ps_result) as sp_run, \
             mock.patch("onionpress.power.os.kill") as os_kill:
            cm._cleanup_stale()
        os_kill.assert_called_once_with(9999, 15)
        self.assertFalse(os.path.exists(os.path.join(tmpdir, "caffeinate.pid")))
        self.assertTrue(any("Cleaned up orphaned caffeinate" in l for l in logs))

    def test_does_not_kill_recycled_pid(self):
        """If the PID points at something other than caffeinate, don't kill it."""
        cm, logs, tmpdir, _ = _make_manager()
        with open(os.path.join(tmpdir, "caffeinate.pid"), "w") as f:
            f.write("9999")
        ps_result = mock.Mock(returncode=0, stdout="vim\n")
        with mock.patch("onionpress.power.subprocess.run",
                        return_value=ps_result), \
             mock.patch("onionpress.power.os.kill") as os_kill:
            cm._cleanup_stale()
        os_kill.assert_not_called()
        # Stale PID file is still removed
        self.assertFalse(os.path.exists(os.path.join(tmpdir, "caffeinate.pid")))

    def test_removes_pid_file_on_read_failure(self):
        cm, _, tmpdir, _ = _make_manager()
        pid_path = os.path.join(tmpdir, "caffeinate.pid")
        with open(pid_path, "w") as f:
            f.write("not-a-number")
        cm._cleanup_stale()
        self.assertFalse(os.path.exists(pid_path))


if __name__ == "__main__":
    unittest.main()
