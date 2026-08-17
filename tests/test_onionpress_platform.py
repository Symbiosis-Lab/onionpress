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
    is_moss_managed, is_quiet_launch,
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
    def _assert_platform_independent_paths(self, paths, data_dir):
        """Paths derived purely from data_dir — same on every OS."""
        self.assertEqual(paths.data_dir, data_dir)
        self.assertEqual(paths.config_file, os.path.join(data_dir, "config"))
        self.assertEqual(paths.secrets_file, os.path.join(data_dir, "secrets"))
        self.assertEqual(paths.log_file, os.path.join(data_dir, "onionpress.log"))
        self.assertEqual(paths.pid_file, os.path.join(data_dir, "onionpress.pid"))
        self.assertEqual(paths.colima_home, os.path.join(data_dir, "colima"))

    @mock.patch("onionpress.platform.detect_os", return_value=OS.MACOS)
    def test_default_paths_macos(self, _):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, ".onionpress")
            paths = resolve_paths(data_dir=data_dir, app_bundle=None)
            self._assert_platform_independent_paths(paths, data_dir)
            self.assertEqual(
                paths.docker_socket,
                os.path.join(data_dir, "colima", "default", "docker.sock"),
            )

    @mock.patch("onionpress.platform.detect_os", return_value=OS.LINUX)
    @mock.patch("os.path.exists", return_value=True)
    @mock.patch("os.getuid", return_value=1000)
    def test_default_paths_linux_rootless(self, _uid, _exists, _os):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, ".onionpress")
            paths = resolve_paths(data_dir=data_dir, app_bundle=None)
            self._assert_platform_independent_paths(paths, data_dir)
            self.assertEqual(paths.docker_socket, "/run/user/1000/docker.sock")

    @mock.patch("onionpress.platform.detect_os", return_value=OS.LINUX)
    @mock.patch("os.path.exists", return_value=False)
    def test_default_paths_linux_rootful_fallback(self, _exists, _os):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = os.path.join(tmpdir, ".onionpress")
            paths = resolve_paths(data_dir=data_dir, app_bundle=None)
            self._assert_platform_independent_paths(paths, data_dir)
            self.assertEqual(paths.docker_socket, "/var/run/docker.sock")

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


class TestIsMossManaged(unittest.TestCase):
    """is_moss_managed: the moss-staged copy is recognized by its location."""

    def test_moss_staged_bundle_is_managed(self):
        self.assertTrue(is_moss_managed(
            "/Users/alice/.moss/stacks/onionpress/OnionPress.app"))

    def test_applications_install_is_not_managed(self):
        self.assertFalse(is_moss_managed("/Applications/OnionPress.app"))

    def test_renamed_bundle_under_stacks_is_still_managed(self):
        # Detection is by location, not bundle name — the user may have
        # renamed the app in Finder.
        self.assertTrue(is_moss_managed(
            "/Users/alice/.moss/stacks/onionpress/MyOnion.app"))

    def test_moss_without_stacks_segment_is_not_managed(self):
        # ".moss" alone (e.g. a site folder's .moss build dir) is not the
        # stack staging area.
        self.assertFalse(is_moss_managed(
            "/Users/alice/Sites/blog/.moss/OnionPress.app"))

    def test_stacks_dir_not_under_dot_moss_is_not_managed(self):
        self.assertFalse(is_moss_managed(
            "/Users/alice/stacks/onionpress/OnionPress.app"))

    def test_empty_and_none_are_not_managed(self):
        self.assertFalse(is_moss_managed(""))
        self.assertFalse(is_moss_managed(None))

    def test_trailing_slash_and_dot_segments_are_normalized(self):
        self.assertTrue(is_moss_managed(
            "/Users/alice/.moss/stacks/onionpress/./OnionPress.app/"))


class TestIsQuietLaunch(unittest.TestCase):
    """is_quiet_launch: env override wins; else moss-managed decides."""

    MOSS_BUNDLE = "/Users/alice/.moss/stacks/onionpress/OnionPress.app"
    STANDALONE_BUNDLE = "/Applications/OnionPress.app"

    def test_moss_staged_copy_is_quiet_by_default(self):
        self.assertTrue(is_quiet_launch(self.MOSS_BUNDLE, environ={}))

    def test_standalone_copy_is_loud_by_default(self):
        self.assertFalse(is_quiet_launch(self.STANDALONE_BUNDLE, environ={}))

    def test_env_forces_quiet_on_standalone_copy(self):
        for value in ("1", "true", "YES", " on "):
            self.assertTrue(is_quiet_launch(
                self.STANDALONE_BUNDLE, environ={"ONIONPRESS_QUIET": value}),
                msg=f"ONIONPRESS_QUIET={value!r} should force quiet")

    def test_env_forces_loud_on_moss_copy(self):
        for value in ("0", "false", "NO", "off"):
            self.assertFalse(is_quiet_launch(
                self.MOSS_BUNDLE, environ={"ONIONPRESS_QUIET": value}),
                msg=f"ONIONPRESS_QUIET={value!r} should force loud")

    def test_unrecognized_env_value_falls_back_to_location(self):
        self.assertTrue(is_quiet_launch(
            self.MOSS_BUNDLE, environ={"ONIONPRESS_QUIET": "banana"}))
        self.assertFalse(is_quiet_launch(
            self.STANDALONE_BUNDLE, environ={"ONIONPRESS_QUIET": "banana"}))

    def test_default_environ_is_process_environment(self):
        with mock.patch.dict(os.environ, {"ONIONPRESS_QUIET": "1"}):
            self.assertTrue(is_quiet_launch(self.STANDALONE_BUNDLE))


if __name__ == "__main__":
    unittest.main()
