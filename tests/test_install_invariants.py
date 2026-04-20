#!/usr/bin/env python3
"""Static source-level invariants that guard against install-flow regressions
we've hit before.

These are text/AST checks, not behavioral tests — they can't prove the
install flow works, only that the specific call sequences that went wrong
in past install tests are still wired up the way they should be. Each check
below has a comment pointing at the incident it's guarding against.
"""

import ast
import os
import re
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read(rel_path):
    with open(os.path.join(PROJECT_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


class TestMacOSBuildBundlesKeyManager(unittest.TestCase):
    """The macOS shell launcher invokes $SCRIPTS_DIR/key_manager.py directly
    (app/MacOS/onionpress:504,638,702). If that file isn't bundled, vanity
    generation silently falls back to a random address AND OnionHeaven
    registration hits "sign_failed" forever — both observed on an install
    test where build-dmg-simple.sh copied menubar.py but not key_manager.py.
    """

    def test_build_script_copies_key_manager_into_scripts_dir(self):
        script = _read("build/build-dmg-simple.sh")
        self.assertRegex(
            script,
            r'cp\s+"\$PROJECT_DIR/src/onionpress/key_manager\.py"\s+'
            r'"\$APP_PATH/Contents/Resources/scripts/"',
            "build-dmg-simple.sh must copy src/onionpress/key_manager.py "
            "into Contents/Resources/scripts/ — the macOS shell launcher "
            "invokes it from there.",
        )

    def test_build_script_verifies_file_not_substring(self):
        """The old check was `grep -rq "key_manager" MenubarApp` — that
        passes even when the actual file the shell launcher needs is
        missing, because the string appears in py2app's bundled bytecode.
        The real check has to be `test -f` against the launcher's path
        and must exit non-zero on failure.
        """
        script = _read("build/build-dmg-simple.sh")
        self.assertNotRegex(
            script,
            r'grep\s+-rq\s+"key_manager"',
            "Substring grep can pass when the file is missing. Use "
            "`test -f` against Contents/Resources/scripts/key_manager.py.",
        )
        self.assertRegex(
            script,
            r'\[\s*-f\s+"\$APP_PATH/Contents/Resources/scripts/key_manager\.py"\s*\]',
            "Need a `test -f` check for the bundled key_manager.py.",
        )
        # The verification must fail the build, not just log a warning.
        # Look for an `exit 1` inside the key_manager check's else-branch.
        km_block = re.search(
            r'if\s+\[\s*-f\s+"\$APP_PATH/Contents/Resources/scripts/key_manager\.py"\s*\];\s*then'
            r'(.*?)fi',
            script,
            re.DOTALL,
        )
        self.assertIsNotNone(km_block, "Could not find key_manager.py test -f block")
        self.assertIn(
            "exit 1",
            km_block.group(1),
            "The key_manager.py check must `exit 1` on miss, not just warn — "
            "otherwise broken bundles ship.",
        )


class TestMakefilePrecheckUsesCorrectPath(unittest.TestCase):
    """The Makefile's `make test` target asserts required source files
    exist before a build. After the move to the `onionpress` package the
    precheck still pointed at the pre-move path `src/key_manager.py`, so
    it couldn't catch a missing file — confirmed by the bug that this
    test suite exists to guard against.
    """

    def test_precheck_points_at_package_path(self):
        mk = _read("Makefile")
        self.assertRegex(
            mk,
            r'test\s+-f\s+src/onionpress/key_manager\.py',
            "Makefile `test` target must precheck the real source path "
            "src/onionpress/key_manager.py.",
        )
        self.assertNotRegex(
            mk,
            r'test\s+-f\s+src/key_manager\.py\b',
            "Stale flat-path check for src/key_manager.py — file was moved "
            "into src/onionpress/ and this check silently passes.",
        )


class TestSubsiteGetsOnionPressTheme(unittest.TestCase):
    """RECURRING regression: after `wp site create` creates the primary
    subsite at /<onionname>/, the new subsite gets WordPress's default
    theme (twentytwentyfive), not OnionPress. The earlier
    install_onionpress_theme() shell helper only activates on blog_id=1,
    so without an explicit per-subsite activation users see the onionpress
    theme flash briefly and then — once the subsite is created and
    onionpress-root-redirect.php starts bouncing / → /<onionname>/ — the
    default theme takes over. This test parses the AST of
    `_provision_primary_subsite` and asserts both calls are present.
    """

    def setUp(self):
        src = _read("src/menubar.py")
        tree = ast.parse(src)
        self.func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_provision_primary_subsite":
                self.func = node
                break
        self.assertIsNotNone(
            self.func,
            "Could not find _provision_primary_subsite in src/menubar.py — "
            "has it been renamed? Update this test.",
        )

    def _subprocess_run_calls(self):
        """Return every subprocess.run(...) call inside the function as a
        list of the string literals found in its first positional arg."""
        calls = []
        for node in ast.walk(self.func):
            if not isinstance(node, ast.Call):
                continue
            # Match subprocess.run(...) and .run(...)
            func = node.func
            name = None
            if isinstance(func, ast.Attribute) and func.attr == "run":
                name = func.attr
            if name != "run" or not node.args:
                continue
            arg = node.args[0]
            if not isinstance(arg, ast.List):
                continue
            literals = []
            for elt in arg.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    literals.append(elt.value)
                elif isinstance(elt, ast.JoinedStr):  # f-string
                    pieces = []
                    for v in elt.values:
                        if isinstance(v, ast.Constant):
                            pieces.append(str(v.value))
                        elif isinstance(v, ast.FormattedValue):
                            pieces.append("{__fmt__}")
                    literals.append("".join(pieces))
            calls.append(literals)
        return calls

    def test_creates_subsite_and_activates_theme(self):
        calls = self._subprocess_run_calls()

        def is_wp_cli(argv, *tokens):
            joined = " ".join(argv)
            return all(t in joined for t in ("wp",) + tokens)

        create_idx = None
        activate_idx = None
        for i, argv in enumerate(calls):
            if is_wp_cli(argv, "site", "create"):
                create_idx = i
            if is_wp_cli(argv, "theme", "activate", "onionpress"):
                # Must be keyed on the subsite URL, not blog_id=1
                joined = " ".join(argv)
                if "http://localhost/{__fmt__}/" in joined or re.search(
                    r"http://localhost/\{?onionname\}?/", joined
                ):
                    activate_idx = i

        self.assertIsNotNone(
            create_idx, "_provision_primary_subsite must call `wp site create`."
        )
        self.assertIsNotNone(
            activate_idx,
            "_provision_primary_subsite must call "
            "`wp theme activate onionpress --url=http://localhost/<onionname>/` "
            "after creating the subsite — otherwise the subsite keeps the "
            "WP default theme (twentytwentyfive) and the user never sees "
            "OnionPress, because root-redirect.php bounces / → /<onionname>/ "
            "once the subsite exists.",
        )
        self.assertGreater(
            activate_idx, create_idx,
            "theme activate must come AFTER wp site create — the subsite "
            "has to exist before we can activate a theme on it.",
        )


if __name__ == "__main__":
    unittest.main()
