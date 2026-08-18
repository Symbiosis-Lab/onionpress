#!/usr/bin/env python3
"""Behavioural tests for the launcher's is_quiet_launch helper.

Quiet launch was implemented in Python only (onionpress.platform), which
covered the MenubarApp and missed the half moss actually calls: `onionpress
start` is a shell script, and on 2026-08-18 it raised "OnionPress is already
starting up" as an osascript modal in the middle of a moss install. moss had
run that `start` itself; the window was one moss did not design, shown to
someone who had asked moss for something, with nobody at the keyboard to
dismiss it.

So the shell now knows the same fact, and these tests exist to keep the two
implementations from drifting: every case is asserted against BOTH the shell
helper and onionpress.platform.is_quiet_launch, from one table.

Like tests/test_menubar_revival.py, this extracts the real function by name
and sources it alone — the launcher's top level is macOS-only and has side
effects (mkdir, a home-directory migration) that a unit test has no business
triggering, and extraction is what lets the Linux CI run this for real
instead of matching text.

tests/test_install_invariants.py::TestMossManagedQuietLaunch guards the call
site — that no dialog in the launcher is left ungated — which is the half
this file cannot see.
"""

import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
LAUNCHER_SRC = os.path.join(PROJECT_ROOT, "app", "MacOS", "onionpress")

sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from onionpress.platform import is_quiet_launch as py_is_quiet_launch


def _extract_function(name):
    """Return the source of shell function `name` from the launcher.

    Same layout convention as test_menubar_revival.py: a function opens at
    column 0 as `name() {` and closes at column 0 with `}`.
    """
    with open(LAUNCHER_SRC, "r", encoding="utf-8") as f:
        src = f.read()
    match = re.search(
        r"^%s\(\)\s*\{\n.*?^\}\n" % re.escape(name), src, re.M | re.S
    )
    if not match:
        raise AssertionError(f"{name}() not found in {LAUNCHER_SRC}")
    return match.group(0)


def _sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


# (bundle path, ONIONPRESS_QUIET or None, expected quiet?)
#
# Absolute paths only. In production APP_BUNDLE descends from
# `cd "$(dirname "${BASH_SOURCE[0]}")" && pwd`, so the shell has already
# normalised it — no `.` or `//` segments can reach the helper, which is why
# it can match on the path text directly where the Python normpaths first.
CASES = [
    ("/Applications/OnionPress.app", None, False),
    ("/Users/dev/.moss/stacks/onionpress/OnionPress.app", None, True),

    # `.moss` alone is not enough — moss keeps other things beside stacks/,
    # and only the staged stack copy is the one moss drives headlessly.
    ("/Users/dev/.moss/plugins/onionpress/OnionPress.app", None, False),
    # ...and neither is `stacks` alone, nor an undotted `moss`.
    ("/Users/dev/stacks/onionpress/OnionPress.app", None, False),
    ("/Users/dev/moss/stacks/onionpress/OnionPress.app", None, False),
    # Adjacency at the tail still counts.
    ("/Users/dev/.moss/stacks", None, True),
    # A space in the path is ordinary on a Mac (Google Drive, iCloud).
    ("/Users/dev/My Files/.moss/stacks/onionpress/OnionPress.app", None, True),

    # The override wins in both directions, case- and space-insensitively.
    ("/Applications/OnionPress.app", "1", True),
    ("/Applications/OnionPress.app", "true", True),
    ("/Applications/OnionPress.app", "YES", True),
    ("/Applications/OnionPress.app", " on ", True),
    ("/Users/dev/.moss/stacks/onionpress/OnionPress.app", "0", False),
    ("/Users/dev/.moss/stacks/onionpress/OnionPress.app", "false", False),
    ("/Users/dev/.moss/stacks/onionpress/OnionPress.app", "Off", False),

    # Anything else is not an override — fall through to the path.
    ("/Applications/OnionPress.app", "", False),
    ("/Applications/OnionPress.app", "maybe", False),
    ("/Users/dev/.moss/stacks/onionpress/OnionPress.app", "maybe", True),
]


class TestLauncherQuietLaunch(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.helper = _extract_function("is_quiet_launch")

    def _shell_says_quiet(self, bundle, override):
        """Run the real shell helper and report its exit status as a bool."""
        env = {k: v for k, v in os.environ.items() if k != "ONIONPRESS_QUIET"}
        if override is not None:
            env["ONIONPRESS_QUIET"] = override
        script = textwrap.dedent("""\
            set -e
            APP_BUNDLE={bundle}

            {helper}

            if is_quiet_launch; then echo quiet; else echo loud; fi
        """).format(bundle=_sh_quote(bundle), helper=self.helper)
        with tempfile.NamedTemporaryFile(
            "w", suffix=".sh", delete=False,
        ) as f:
            f.write(script)
            runner = f.name
        self.addCleanup(os.unlink, runner)
        proc = subprocess.run(
            ["bash", runner], capture_output=True, text=True, timeout=30,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip() == "quiet"

    def test_shell_helper_matches_the_table(self):
        for bundle, override, expected in CASES:
            with self.subTest(bundle=bundle, override=override):
                self.assertEqual(
                    self._shell_says_quiet(bundle, override), expected,
                    "app/MacOS/onionpress is_quiet_launch() disagrees for "
                    "bundle=%r ONIONPRESS_QUIET=%r" % (bundle, override),
                )

    def test_python_agrees_with_the_shell_on_every_case(self):
        """The two implementations decide the same thing or the fix is half
        done — a launcher that thinks it is loud while the MenubarApp thinks
        it is quiet is the same class of bug as having no gate at all."""
        for bundle, override, expected in CASES:
            with self.subTest(bundle=bundle, override=override):
                environ = {} if override is None else {
                    "ONIONPRESS_QUIET": override}
                self.assertEqual(
                    py_is_quiet_launch(bundle, environ), expected,
                    "onionpress.platform.is_quiet_launch disagrees with the "
                    "shell for bundle=%r ONIONPRESS_QUIET=%r"
                    % (bundle, override),
                )

    def test_bundle_is_derived_from_the_script_location(self):
        """APP_BUNDLE must be the bundle, not the launcher's directory.

        The helper's whole input is one variable, so getting it from the
        wrong level of the bundle would silently make the gate a no-op
        (Contents/MacOS is two levels below the .app). It also has to come
        from the script's own path — the copy decides, not the caller — and
        `cd && pwd` is what makes it absolute and normalised.
        """
        with open(LAUNCHER_SRC, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertRegex(
            src, r'SCRIPT_DIR="\$\(\s*cd .*&& pwd \)"',
            "SCRIPT_DIR must still be resolved with `cd … && pwd`; "
            "is_quiet_launch relies on APP_BUNDLE being absolute.",
        )
        self.assertRegex(
            src, r'APP_BUNDLE="\$\(dirname "\$APP_DIR"\)"',
            "APP_BUNDLE must be dirname($APP_DIR) — i.e. the .app itself, "
            "given APP_DIR is Contents/ and SCRIPT_DIR is Contents/MacOS.",
        )


if __name__ == "__main__":
    unittest.main()
