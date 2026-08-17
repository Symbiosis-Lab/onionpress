#!/usr/bin/env python3
"""Behavioural tests for the launcher's ensure_menubar_running helper.

`quit` takes the MenubarApp down; `start` only ever brought containers back.
Any caller that scripts quit+start — moss's Restart recovery is exactly that
pair — therefore left the app off permanently, and the MenubarApp is the sole
writer of status.json and the sole sender of OnionHeaven's /online heartbeat.
The 2026-08-16 consequence: a frozen 19-hour-old reachability verdict and an
OnionHeaven takeover that could never be released.

These run the REAL helper, extracted from app/MacOS/onionpress by name and
sourced on its own. Extraction rather than running the whole launcher is what
makes this cross-platform: the launcher's top level is macOS-only (sysctl,
PlistBuddy, `stat -f`, `arch -arm64`) and has side effects — mkdir, a home
directory migration — that a unit test has no business triggering. The helper
itself uses only pgrep/nohup/rm, which behave the same on Linux, so the CI
that runs on ubuntu gets real coverage of the logic instead of a text match.

tests/test_install_invariants.py guards the call site and its ordering, which
is the half this file cannot see.
"""

import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LAUNCHER_SRC = os.path.join(PROJECT_ROOT, "app", "MacOS", "onionpress")

# The matcher the helper (and launcher.sh, and the quit arm) uses to decide
# whether a MenubarApp is already alive.
MENUBAR_MATCH = "MenubarApp/Contents/MacOS/OnionPress"


def _extract_function(name):
    """Return the source of shell function `name` from the launcher.

    Relies on the file's own layout convention: a function opens at column 0
    as `name() {` and closes at column 0 with `}`.
    """
    with open(LAUNCHER_SRC, "r", encoding="utf-8") as f:
        src = f.read()
    match = re.search(
        r"^%s\(\)\s*\{\n.*?^\}\n" % re.escape(name), src, re.M | re.S
    )
    if not match:
        raise AssertionError(f"{name}() not found in {LAUNCHER_SRC}")
    return match.group(0)


class TestEnsureMenubarRunning(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="onionpress-menubar-revival-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.resources = os.path.join(self.tmp, "Resources")
        self.data_dir = os.path.join(self.tmp, "data")
        self.log_file = os.path.join(self.tmp, "launcher.log")
        os.makedirs(self.data_dir)

        self.menubar_bin = os.path.join(
            self.resources, "MenubarApp", "Contents", "MacOS", "OnionPress"
        )
        self.marker = os.path.join(self.tmp, "launched")
        self.pidfile = os.path.join(self.data_dir, "menubar.pid")

        self.helper = _extract_function("ensure_menubar_running")

        # A real MenubarApp on a developer's Mac matches the same pgrep as
        # our stub would, so the "nothing running" cases are not decidable.
        if self._menubar_process_alive():
            self.skipTest("a real MenubarApp is running for this user")

    def _menubar_process_alive(self):
        return subprocess.run(
            ["pgrep", "-u", str(os.getuid()), "-f", MENUBAR_MATCH],
            capture_output=True,
        ).returncode == 0

    def _install_stub(self, body):
        os.makedirs(os.path.dirname(self.menubar_bin), exist_ok=True)
        with open(self.menubar_bin, "w") as f:
            f.write(body)
        os.chmod(self.menubar_bin, 0o755)

    def _run_helper(self):
        """Source the real helper with the launcher's globals defined.

        Run from a FILE, not `bash -c`: the helper's own source contains the
        pgrep matcher, so `bash -c` would put that string in the harness's
        command line and the helper would match itself and no-op. Production
        runs `bash /…/onionpress start`, whose command line carries no such
        string, so a file keeps the test faithful as well as correct.
        """
        script = textwrap.dedent(f"""\
            set -e
            RESOURCES_DIR={self.resources!r}
            DATA_DIR={self.data_dir!r}
            LOG_FILE={self.log_file!r}
            log() {{ echo "[log] $1" >> "$LOG_FILE"; }}

            {self.helper}

            ensure_menubar_running
        """)
        runner = os.path.join(self.tmp, "run-helper.sh")
        with open(runner, "w") as f:
            f.write(script)
        return subprocess.run(
            ["bash", runner], capture_output=True, text=True, timeout=30
        )

    def _wait_for_marker(self, timeout=10.0):
        """The helper backgrounds the app, so the marker lands after it
        returns."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if os.path.exists(self.marker):
                return True
            time.sleep(0.05)
        return False

    def _kill_stubs(self):
        subprocess.run(
            ["pkill", "-u", str(os.getuid()), "-f", MENUBAR_MATCH],
            capture_output=True,
        )

    def test_no_bundle_is_a_noop(self):
        """A source checkout or CI has no built MenubarApp — revival must be
        a no-op there, not an error that fails `start`."""
        proc = self._run_helper()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(self.marker))

    def test_launches_the_app_when_it_is_not_running(self):
        """The whole point: containers may be fine, but a dead MenubarApp
        must be brought back."""
        self._install_stub(f"#!/bin/sh\ntouch {self.marker!r}\n")

        proc = self._run_helper()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(
            self._wait_for_marker(),
            "ensure_menubar_running did not launch the MenubarApp",
        )

    def test_does_not_launch_a_second_copy(self):
        """The MenubarApp re-enters `onionpress start` on every launch
        (auto_start -> start_service), so an unguarded spawn would have the
        app start a second copy of itself."""
        self._install_stub(f"#!/bin/sh\ntouch {self.marker!r}\nsleep 30\n")
        self.addCleanup(self._kill_stubs)

        # A live process whose command line matches the helper's pgrep — the
        # stub's own path contains the matcher, so running it is enough.
        alive = subprocess.Popen(
            [self.menubar_bin],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(alive.wait)
        self.addCleanup(alive.kill)
        self.assertTrue(self._wait_for_marker(), "stub never started")
        os.remove(self.marker)

        proc = self._run_helper()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(
            self._wait_for_marker(timeout=2.0),
            "a second MenubarApp was launched over a live one",
        )

    def test_clears_a_stale_pid_file_before_launching(self):
        """`quit` escalates to SIGKILL, which bypasses the app's own
        _remove_pid_file. The leftover menubar.pid is not inert — the
        launcher's upload-analytics arm reads it as liveness."""
        self._install_stub(f"#!/bin/sh\ntouch {self.marker!r}\n")
        with open(self.pidfile, "w") as f:
            f.write("2991\n")  # the dead PID from the 2026-08-16 incident

        proc = self._run_helper()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self._wait_for_marker())
        self.assertFalse(
            os.path.exists(self.pidfile),
            "a stale menubar.pid survived the relaunch",
        )

    def test_the_child_does_not_hold_the_callers_pipe_open(self):
        """moss runs the launcher as a subprocess and reads its output to
        EOF. A backgrounded child inheriting that pipe keeps it open, so the
        caller would block for as long as the MenubarApp lives — which is
        forever, by design. subprocess.run() below reads to EOF, so an
        inherited pipe shows up here as a timeout, not a wrong assertion.
        """
        self._install_stub(
            f"#!/bin/sh\ntouch {self.marker!r}\necho noise\nsleep 30\n"
        )
        self.addCleanup(self._kill_stubs)

        proc = self._run_helper()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(self._wait_for_marker())
        self.assertNotIn(
            "noise", proc.stdout,
            "the MenubarApp's output reached the caller's pipe instead of "
            "the log file",
        )
        with open(self.log_file) as f:
            self.assertIn("noise", f.read(),
                          "the MenubarApp's output should land in the log")


if __name__ == "__main__":
    unittest.main()
