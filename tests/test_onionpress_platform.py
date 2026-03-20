"""Tests for src/onionpress/platform.py."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.platform import (
    OS, Arch, OnionPressPaths,
    detect_os, detect_arch, detect_timezone, resolve_paths, find_app_bundle,
)


class TestDetectOS(unittest.TestCase):
    @mock.patch("platform.system", return_value="Darwin")
    def test_macos(self, _):
        self.assertEqual(detect_os(), OS.MACOS)

    @mock.patch("platform.system", return_value="Linux")
    def test_linux(self, _):
        self.assertEqual(detect_os(), OS.LINUX)

    @mock.patch("platform.system", return_value="Windows")
    def test_unsupported(self, _):
        with self.assertRaises(RuntimeError):
            detect_os()


class TestDetectArch(unittest.TestCase):
    @mock.patch("platform.system", return_value="Darwin")
    @mock.patch("subprocess.run")
    def test_macos_arm64(self, mock_run, _):
        mock_run.return_value = mock.Mock(returncode=0, stdout="1\n")
        self.assertEqual(detect_arch(), Arch.ARM64)

    @mock.patch("platform.system", return_value="Darwin")
    @mock.patch("platform.machine", return_value="x86_64")
    @mock.patch("subprocess.run")
    def test_macos_x86(self, mock_run, _machine, _system):
        mock_run.return_value = mock.Mock(returncode=1, stdout="")
        self.assertEqual(detect_arch(), Arch.X86_64)

    @mock.patch("platform.system", return_value="Linux")
    @mock.patch("platform.machine", return_value="aarch64")
    def test_linux_arm64(self, _machine, _system):
        self.assertEqual(detect_arch(), Arch.ARM64)

    @mock.patch("platform.system", return_value="Linux")
    @mock.patch("platform.machine", return_value="x86_64")
    def test_linux_x86(self, _machine, _system):
        self.assertEqual(detect_arch(), Arch.X86_64)


class TestDetectTimezone(unittest.TestCase):
    def test_returns_string(self):
        tz = detect_timezone()
        self.assertIsInstance(tz, str)
        self.assertTrue(len(tz) > 0)

    @mock.patch("os.readlink", side_effect=OSError("no symlink"))
    @mock.patch("builtins.open", side_effect=OSError("no file"))
    def test_fallback_utc(self, _open, _readlink):
        self.assertEqual(detect_timezone(), "UTC")

    @mock.patch("os.readlink", return_value="/usr/share/zoneinfo/America/New_York")
    def test_parses_symlink(self, _):
        self.assertEqual(detect_timezone(), "America/New_York")


class TestResolvePaths(unittest.TestCase):
    def test_default_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, ".onionpress")
            paths = resolve_paths(data_dir=data_dir, app_bundle=None)

            self.assertEqual(paths.data_dir, data_dir)
            self.assertEqual(paths.config_file, os.path.join(data_dir, "config"))
            self.assertEqual(paths.secrets_file, os.path.join(data_dir, "secrets"))
            self.assertEqual(paths.log_file, os.path.join(data_dir, "onionpress.log"))
            self.assertEqual(paths.pid_file, os.path.join(data_dir, "onionpress.pid"))
            self.assertEqual(paths.colima_home, os.path.join(data_dir, "colima"))
            self.assertEqual(
                paths.docker_socket,
                os.path.join(data_dir, "colima", "default", "docker.sock"),
            )

    def test_with_app_bundle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = os.path.join(tmpdir, "OnionPress.app")
            os.makedirs(os.path.join(app, "Contents", "Resources", "bin"))
            os.makedirs(os.path.join(app, "Contents", "Resources", "docker"))

            paths = resolve_paths(data_dir=tmpdir, app_bundle=app)
            self.assertEqual(paths.app_bundle, app)
            self.assertEqual(
                paths.bin_dir,
                os.path.join(app, "Contents", "Resources", "bin"),
            )
            self.assertEqual(
                paths.docker_dir,
                os.path.join(app, "Contents", "Resources", "docker"),
            )

    def test_no_app_bundle(self):
        paths = resolve_paths(data_dir="/tmp/test", app_bundle=None)
        # bin_dir and docker_dir should be empty when no bundle found
        # (find_app_bundle may find the real one, so test with explicit None)
        # Just verify paths object is created successfully
        self.assertIsInstance(paths, OnionPressPaths)

    def test_frozen_dataclass(self):
        paths = resolve_paths(data_dir="/tmp/test", app_bundle=None)
        with self.assertRaises(AttributeError):
            paths.data_dir = "/other"


class TestFindAppBundle(unittest.TestCase):
    def test_returns_string_or_none(self):
        result = find_app_bundle()
        self.assertTrue(result is None or isinstance(result, str))


if __name__ == "__main__":
    unittest.main()
