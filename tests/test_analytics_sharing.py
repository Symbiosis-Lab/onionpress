"""Tests for src/onionpress/analytics_sharing.py."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.analytics_sharing import _LOG_NAME_RE


class TestLogNameRegex(unittest.TestCase):
    def _assert_match(self, name):
        self.assertIsNotNone(
            _LOG_NAME_RE.match(name), f"expected {name!r} to match"
        )

    def _assert_no_match(self, name):
        self.assertIsNone(
            _LOG_NAME_RE.match(name), f"expected {name!r} not to match"
        )

    def test_dated_log_accepted(self):
        for prefix in (
            "onionpress",
            "wordpress-access",
            "wordpress-visitors",
            "container-onionpress-tor",
            "container-onionheaven",
            "clearnet",
            "launcher",
        ):
            self._assert_match(f"{prefix}-2026-04-21-001.log")

    def test_dated_gz_accepted(self):
        for prefix in (
            "onionpress",
            "wordpress-access",
            "wordpress-visitors",
            "container-onionpress-tor",
            "container-onionheaven",
            "clearnet",
            "launcher",
        ):
            self._assert_match(f"{prefix}-2026-04-21-001.log.gz")

    def test_takeover_numbered_variant(self):
        self._assert_match(
            "container-onionheaven-takeover-5-2026-04-21-001.log"
        )
        self._assert_match(
            "container-onionheaven-takeover-42-2026-04-21-001.log.gz"
        )

    def test_launcher_legacy_symlink_name(self):
        self._assert_match("launcher.log")

    def test_launcher_legacy_symlink_gz_rejected(self):
        # Only the dated rotation form is allowed to be gzipped.
        self._assert_no_match("launcher.log.gz")

    def test_path_traversal_rejected(self):
        self._assert_no_match("../etc/passwd")
        self._assert_no_match("../onionpress-2026-04-21-001.log")
        self._assert_no_match("subdir/onionpress-2026-04-21-001.log")

    def test_unexpected_extensions_rejected(self):
        self._assert_no_match("onionpress-2026-04-21-001.log.bak")
        self._assert_no_match("onionpress-2026-04-21-001.txt")
        self._assert_no_match("onionpress-2026-04-21-001.log.gz.bak")

    def test_bad_date_format_rejected(self):
        self._assert_no_match("onionpress-2026-4-21-001.log")
        self._assert_no_match("onionpress-26-04-21-001.log")
        self._assert_no_match("onionpress-2026-04-21-1.log")

    def test_unknown_prefix_rejected(self):
        self._assert_no_match("evil-2026-04-21-001.log")
        self._assert_no_match("secrets-2026-04-21-001.log.gz")


if __name__ == "__main__":
    unittest.main()
