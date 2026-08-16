"""Tests for src/onionpress/log_capture.py — the container-log capture cursor.

Root cause being fixed (2026-08-16 incident forensics): the menubar capture
worker reattached ``docker logs -f --since <wall-clock>`` where the cursor
had only second granularity and ``--since`` is inclusive, so every line
docker recorded during the boundary second was re-read verbatim on each
reattach — the doubled LAST RESORT blocks in container-tor-2026-08-16-001.log.

The fix keys the cursor on docker's own ``--timestamps`` token (exact,
nanosecond) plus a count of lines seen bearing that token, and skips exactly
that many token-identical lines after a ``--since`` reattach.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress.log_capture import CaptureCursor


TS1 = "2026-08-16T02:14:16.123456789Z"
TS2 = "2026-08-16T02:14:16.223456789Z"
TS3 = "2026-08-16T02:14:17.5Z"  # Go RFC3339Nano trims trailing zeros


class TestAttachArgs(unittest.TestCase):
    def test_first_attach_tails(self):
        c = CaptureCursor()
        self.assertEqual(c.attach_args(), ["--timestamps", "--tail", "100"])

    def test_reattach_uses_docker_token_not_wall_clock(self):
        c = CaptureCursor()
        c.attach_args()
        c.accept(f"{TS1} hello\n")
        self.assertEqual(c.attach_args(), ["--timestamps", "--since", TS1])


class TestAccept(unittest.TestCase):
    def test_strips_timestamp_prefix(self):
        c = CaptureCursor()
        c.attach_args()
        self.assertEqual(c.accept(f"{TS1} [notice] Bootstrapped 100%\n"),
                         "[notice] Bootstrapped 100%\n")

    def test_line_without_timestamp_passes_through_untouched(self):
        # Defensive: docker error lines ("Error grabbing logs: ...") carry
        # no timestamp prefix. They must be written as-is, and must not
        # corrupt the cursor.
        c = CaptureCursor()
        c.attach_args()
        c.accept(f"{TS1} real line\n")
        self.assertEqual(c.accept("Error grabbing logs: unexpected EOF\n"),
                         "Error grabbing logs: unexpected EOF\n")
        self.assertEqual(c.last_ts, TS1)

    def test_first_attach_never_skips(self):
        c = CaptureCursor()
        c.attach_args()
        for i in range(3):
            self.assertIsNotNone(c.accept(f"{TS1} line {i}\n"))


class TestReattachOverlap(unittest.TestCase):
    """The inclusive-instant overlap: --since re-sends lines AT the cursor."""

    def test_boundary_lines_skipped_once(self):
        c = CaptureCursor()
        c.attach_args()
        c.accept(f"{TS1} a\n")
        c.accept(f"{TS2} b\n")
        # Stream breaks; reattach re-sends everything >= TS2 (inclusive).
        self.assertEqual(c.attach_args(), ["--timestamps", "--since", TS2])
        self.assertIsNone(c.accept(f"{TS2} b\n"))          # the duplicate
        self.assertEqual(c.accept(f"{TS3} c\n"), "c\n")     # fresh line

    def test_incident_shape_whole_block_resent(self):
        # 2026-08-16: a ~10-line LAST RESORT block (all written by docker in
        # the same boundary window) was re-read verbatim. With token-exact
        # cursors only genuinely token-identical lines can overlap — feed a
        # multi-line same-instant burst and require every duplicate skipped.
        c = CaptureCursor()
        c.attach_args()
        block = [f"{TS2} LAST RESORT line {i}\n" for i in range(4)]
        for line in block:
            c.accept(line)
        c.attach_args()
        for line in block:
            self.assertIsNone(c.accept(line), f"duplicate not skipped: {line!r}")
        self.assertEqual(c.accept(f"{TS3} after\n"), "after\n")

    def test_unseen_lines_at_boundary_instant_are_kept(self):
        # The stream broke mid-instant: we saw 2 of the 3 lines docker
        # recorded at TS2. On reattach all 3 arrive; only the first 2
        # (the ones we wrote) may be skipped.
        c = CaptureCursor()
        c.attach_args()
        c.accept(f"{TS2} one\n")
        c.accept(f"{TS2} two\n")
        c.attach_args()
        self.assertIsNone(c.accept(f"{TS2} one\n"))
        self.assertIsNone(c.accept(f"{TS2} two\n"))
        self.assertEqual(c.accept(f"{TS2} three\n"), "three\n")

    def test_new_token_ends_overlap_early(self):
        # Docker rotated/pruned the boundary lines away: reattach delivers
        # only newer lines. The skip budget must not swallow them.
        c = CaptureCursor()
        c.attach_args()
        c.accept(f"{TS1} old\n")
        c.attach_args()
        self.assertEqual(c.accept(f"{TS3} new\n"), "new\n")
        # And a later line that happens to reuse no token is unaffected.
        self.assertEqual(c.accept(f"{TS3} newer\n"), "newer\n")

    def test_skip_budget_scoped_to_one_attachment(self):
        # A second reattach re-arms the skip from the CURRENT cursor, not
        # the stale one.
        c = CaptureCursor()
        c.attach_args()
        c.accept(f"{TS1} a\n")
        c.attach_args()
        self.assertIsNone(c.accept(f"{TS1} a\n"))
        c.accept(f"{TS2} b\n")
        c.attach_args()
        self.assertEqual(c.attach_args()[-1], TS2)
        self.assertIsNone(c.accept(f"{TS2} b\n"))
        self.assertEqual(c.accept(f"{TS3} c\n"), "c\n")


if __name__ == "__main__":
    unittest.main()
