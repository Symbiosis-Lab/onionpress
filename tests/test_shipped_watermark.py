"""Tests for the shipped-watermark retention behaviour in log_rotation."""

import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import log_rotation
from onionpress.log_rotation import RotatingLog, extract_log_type, mark_shipped


class TestExtractLogType(unittest.TestCase):
    def test_basic_dated(self):
        self.assertEqual(
            extract_log_type("onionpress-2026-04-21-001.log"),
            "onionpress",
        )

    def test_gz(self):
        self.assertEqual(
            extract_log_type("wordpress-access-2026-04-21-001.log.gz"),
            "wordpress-access",
        )

    def test_takeover_with_digit_in_type(self):
        self.assertEqual(
            extract_log_type(
                "container-onionheaven-takeover-5-2026-04-21-001.log"
            ),
            "container-onionheaven-takeover-5",
        )

    def test_legacy_launcher(self):
        self.assertEqual(extract_log_type("launcher.log"), "launcher")

    def test_unknown_returns_none(self):
        self.assertIsNone(extract_log_type("arbitrary-file.txt"))
        self.assertIsNone(extract_log_type(".shipped.json"))


class TestMarkShipped(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_initial_write(self):
        mark_shipped(self.tmpdir, "onionpress", "onionpress-2026-04-21-001.log.gz")
        data = log_rotation.read_shipped(self.tmpdir)
        self.assertEqual(data["onionpress"],
                         "onionpress-2026-04-21-001.log.gz")

    def test_monotonic_only(self):
        mark_shipped(self.tmpdir, "onionpress", "onionpress-2026-04-21-001.log.gz")
        # Older filename must NOT overwrite the newer watermark.
        mark_shipped(self.tmpdir, "onionpress", "onionpress-2026-04-20-001.log.gz")
        data = log_rotation.read_shipped(self.tmpdir)
        self.assertEqual(data["onionpress"],
                         "onionpress-2026-04-21-001.log.gz")

    def test_multi_type(self):
        mark_shipped(self.tmpdir, "onionpress", "onionpress-2026-04-21-001.log.gz")
        mark_shipped(self.tmpdir, "wordpress-access",
                     "wordpress-access-2026-04-21-001.log.gz")
        data = log_rotation.read_shipped(self.tmpdir)
        self.assertEqual(set(data.keys()), {"onionpress", "wordpress-access"})


class TestUnshippedRetention(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_file(self, name, size, mtime):
        path = os.path.join(self.tmpdir, name)
        with open(path, "wb") as f:
            f.write(b"x" * size)
        os.utime(path, (mtime, mtime))
        return path

    def test_unshipped_preserved_under_soft_cap(self):
        """Soft-cap phase must skip files newer than watermark."""
        # max_total_size = 500 bytes, hard_ceiling = 2500 bytes
        log = RotatingLog(self.tmpdir, "onionpress",
                          max_size=10_000_000, max_total_size=500)
        old_mtime = time.time() - 3600

        # Three unshipped files, total 900 bytes (> soft cap, < hard ceiling)
        self._write_file("onionpress-2026-04-19-001.log.gz", 300, old_mtime)
        self._write_file("onionpress-2026-04-20-001.log.gz", 300, old_mtime + 1)
        self._write_file("onionpress-2026-04-21-001.log.gz", 300, old_mtime + 2)

        # No watermark set → all unshipped.
        log._enforce_total_size()

        # All three should survive — nothing was shipped, so nothing can
        # be deleted in phase 1; and total (900) < hard ceiling (2500).
        remaining = sorted(os.listdir(self.tmpdir))
        remaining = [r for r in remaining if r.endswith(".log.gz")]
        self.assertEqual(len(remaining), 3,
                         f"Expected all 3 unshipped to survive, got {remaining}")

    def test_shipped_deleted_first(self):
        log = RotatingLog(self.tmpdir, "onionpress",
                          max_size=10_000_000, max_total_size=500)
        old_mtime = time.time() - 3600

        self._write_file("onionpress-2026-04-19-001.log.gz", 300, old_mtime)
        self._write_file("onionpress-2026-04-20-001.log.gz", 300, old_mtime + 1)
        self._write_file("onionpress-2026-04-21-001.log.gz", 300, old_mtime + 2)

        # Mark two oldest as shipped
        mark_shipped(self.tmpdir, "onionpress",
                     "onionpress-2026-04-20-001.log.gz")

        log._enforce_total_size()

        remaining = sorted(
            r for r in os.listdir(self.tmpdir) if r.endswith(".log.gz")
        )
        # Newest unshipped file must survive; older shipped ones deleted
        # to get under soft cap.
        self.assertIn("onionpress-2026-04-21-001.log.gz", remaining)

    def test_hard_ceiling_trims_unshipped(self):
        log = RotatingLog(self.tmpdir, "onionpress",
                          max_size=10_000_000, max_total_size=100)
        # hard ceiling = 500
        old_mtime = time.time() - 3600

        # 6 files × 200 bytes = 1200 bytes, all unshipped
        for i in range(6):
            self._write_file(f"onionpress-2026-04-{15+i:02d}-001.log.gz",
                             200, old_mtime + i)

        log._enforce_total_size()

        remaining = [r for r in os.listdir(self.tmpdir)
                     if r.endswith(".log.gz")]
        total = sum(os.path.getsize(os.path.join(self.tmpdir, r))
                    for r in remaining)
        # Hard-ceiling (500) must trim even unshipped files.
        self.assertLessEqual(total, 500)


if __name__ == "__main__":
    unittest.main()
