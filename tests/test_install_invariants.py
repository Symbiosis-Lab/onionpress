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
    default theme takes over.

    The logic now lives in setup_logic.provision_primary_subsite() which is
    shared between Mac and Linux. This test parses that function's AST and
    asserts both calls are present.
    """

    def setUp(self):
        src = _read("src/onionpress/setup_logic.py")
        tree = ast.parse(src)
        self.func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "provision_primary_subsite":
                self.func = node
                break
        self.assertIsNotNone(
            self.func,
            "Could not find provision_primary_subsite in "
            "src/onionpress/setup_logic.py — has it been renamed? Update this test.",
        )

    def _subprocess_run_calls(self):
        """Return every wp-cli call inside the function.

        Handles two call patterns:
          1. subprocess.run(["docker", "exec", ..., "wp", ...], ...)
             — used by old menubar.py inline style
          2. wp("site", "create", ...)
             — used by setup_logic.py's inner wp() helper
        Both are normalised to a flat list of string tokens so the
        same is_wp_cli() checks work for both patterns.
        """
        def _extract_str_args(nodes):
            result = []
            for elt in nodes:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    result.append(elt.value)
                elif isinstance(elt, ast.JoinedStr):  # f-string
                    pieces = []
                    for v in elt.values:
                        if isinstance(v, ast.Constant):
                            pieces.append(str(v.value))
                        elif isinstance(v, ast.FormattedValue):
                            pieces.append("{__fmt__}")
                    result.append("".join(pieces))
            return result

        calls = []
        for node in ast.walk(self.func):
            if not isinstance(node, ast.Call):
                continue
            func = node.func

            # Pattern 1: subprocess.run(["docker", "exec", ...], ...)
            if isinstance(func, ast.Attribute) and func.attr == "run" and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.List):
                    calls.append(_extract_str_args(arg.elts))

            # Pattern 2: wp("site", "create", ...) — inner helper used by
            # setup_logic.provision_primary_subsite(). Prepend a canonical
            # docker-exec prefix so is_wp_cli() matches the same way.
            elif isinstance(func, ast.Name) and func.id == "wp" and node.args:
                tokens = _extract_str_args(node.args)
                if tokens:
                    calls.append(
                        ["docker", "exec", "onionpress-wordpress",
                         "wp", "--allow-root"] + tokens
                    )

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


class TestSubsiteSetsPrimaryBlog(unittest.TestCase):
    """Second half of the subsite regression: even after the role and
    theme are set on the new subsite, users still hit blog_id=1's admin
    from "+ New", "My Sites", and wp-admin redirects until their
    primary_blog usermeta points at the subsite. Observed on a fresh
    install where `+ New → Post` routed to http://<onion>/wp-admin/
    instead of http://<onion>/<onionname>/wp-admin/.

    The logic now lives in setup_logic.provision_primary_subsite().
    """

    def test_primary_blog_is_set_on_new_subsite(self):
        src = _read("src/onionpress/setup_logic.py")
        import ast
        tree = ast.parse(src)
        func = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "provision_primary_subsite":
                func = node
                break
        self.assertIsNotNone(func)

        body = ast.unparse(func)
        self.assertIn("primary_blog", body,
            "_provision_primary_subsite must update the user's primary_blog "
            "meta to point at the newly-created subsite. Without this, "
            'wp-admin\'s "+ New" still routes to blog_id=1 (network root) '
            "and users end up posting on the wrong site.")
        self.assertIn("'user', 'meta', 'update'", body,
            "primary_blog should be set via `wp user meta update`.")
        # The update must reference the user (onionname) and a resolved
        # blog_id — guard against a hardcoded `1` or `2` accidentally
        # shipping.
        self.assertIn("get_blog_id_from_url", body,
            "Resolve the subsite's blog_id dynamically (the onionname "
            "varies per install) rather than hardcoding a literal.")


class TestLinuxInstallTray(unittest.TestCase):
    """Guard against regressions in the Linux install tray setup.

    Incident: after a reinstall the tray icon never appeared because
    install.sh (a) had no symlink for onionpress-tray, (b) never wrote
    an autostart .desktop file, and (c) never launched the tray process.
    All three were added together; these checks ensure they stay wired up.
    """

    def setUp(self):
        self.script = _read("linux/install.sh")

    def test_tray_binary_copied_to_install_dir(self):
        self.assertRegex(
            self.script,
            r'cp\s+.*linux/onionpress-tray.*INSTALL_DIR',
            "install.sh must copy linux/onionpress-tray into INSTALL_DIR — "
            "without it the symlink and tray launch both fail silently.",
        )

    def test_tray_symlink_created(self):
        self.assertRegex(
            self.script,
            r'ln\s+-sf\s+["\$\w/]*onionpress-tray["\s]+/usr/local/bin/onionpress-tray',
            "install.sh must symlink onionpress-tray into /usr/local/bin/ — "
            "without it the autostart .desktop Exec= path doesn't resolve.",
        )

    def test_usr_local_bin_created_before_symlink(self):
        """Guard against the install completing but `onionpress` being absent
        from PATH because /usr/local/bin didn't exist on a minimal image and
        the original `ln -sf … 2>/dev/null || true` swallowed the failure.

        The fix: mkdir -p /usr/local/bin while sudo is held, and remove the
        error-suppressing `|| true` so a genuine symlink failure aborts the
        install instead of producing a half-working state.
        """
        # mkdir must appear before the symlink lines. Match the actual
        # `ln -sf … /usr/local/bin/onionpress…` command, not just any
        # mention of the path (comments referring to the symlink target
        # earlier in the file would otherwise pass-through-pollute this).
        import re
        mkdir_pos = self.script.find("mkdir -p /usr/local/bin")
        ln_match = re.search(
            r'ln\s+-sf\s+[^\n]*\s/usr/local/bin/onionpress', self.script)
        self.assertGreaterEqual(
            mkdir_pos, 0,
            "install.sh must `mkdir -p /usr/local/bin` — that dir doesn't "
            "exist on minimal Ubuntu/Debian images.",
        )
        self.assertIsNotNone(
            ln_match,
            "install.sh must `ln -sf … /usr/local/bin/onionpress`.",
        )
        self.assertLess(
            mkdir_pos, ln_match.start(),
            "`mkdir -p /usr/local/bin` must come BEFORE the `ln -sf` lines.",
        )
        # The symlink lines must NOT swallow errors any more — otherwise a
        # real failure (filesystem read-only, etc.) silently produces a
        # broken install.
        self.assertNotRegex(
            self.script,
            r'ln\s+-sf\s+[^\n]*/usr/local/bin/onionpress[^\n]*\|\|\s*true',
            "Symlink command must NOT end with `|| true` — that was the bug "
            "that hid the missing /usr/local/bin and produced an install "
            "with no `onionpress` on PATH.",
        )

    def test_autostart_desktop_file_written(self):
        self.assertIn(
            "onionpress-tray.desktop",
            self.script,
            "install.sh must write ~/.config/autostart/onionpress-tray.desktop "
            "so the tray relaunches on every login.",
        )
        self.assertRegex(
            self.script,
            r'AUTOSTART_DIR.*\.config/autostart',
            "install.sh must place the autostart file under ~/.config/autostart/.",
        )

    def test_autostart_desktop_exec_points_at_tray(self):
        # The heredoc block must contain an Exec= pointing at the tray binary.
        self.assertIsNotNone(
            re.search(r'cat\s*>.*onionpress-tray\.desktop.*<<TRAY_EOF', self.script),
            "install.sh must write onionpress-tray.desktop via a heredoc.",
        )
        self.assertRegex(
            self.script,
            r'Exec=.*onionpress-tray',
            "The autostart .desktop Exec= must point at the onionpress-tray binary.",
        )

    def test_tray_launched_on_gui_install(self):
        # The display guard and tray launch may be on separate lines, so use
        # re.DOTALL explicitly rather than assertRegex (which doesn't set it).
        self.assertIsNotNone(
            re.search(
                r'DISPLAY.*WAYLAND_DISPLAY.*onionpress-tray',
                self.script,
                re.DOTALL,
            ),
            "install.sh must launch onionpress-tray immediately when a "
            "display is present — otherwise the icon only appears after the "
            "next login.",
        )

    def test_stale_tray_killed_before_relaunch(self):
        self.assertRegex(
            self.script,
            r'pkill.*onionpress-tray',
            "install.sh must kill any running tray before relaunching — "
            "on reinstall the old process would otherwise persist alongside "
            "the new one.",
        )


class TestLinuxSymlinkBeforeTrayLaunch(unittest.TestCase):
    """Guard against the SetupDialog → provision-post-install race.

    Incident: install.sh launched the tray (which exposes a Setup dialog
    that the user can click into immediately) before it created the
    /usr/local/bin/onionpress symlink. A fast user could complete Setup
    while the symlink still didn't exist; setup_logic.install_fresh_
    wordpress then ran `subprocess.run(["/usr/local/bin/onionpress",
    "provision-post-install"])`, which raised FileNotFoundError. The
    error got swallowed into the dialog's log buffer (not onionpress.log),
    install_fresh_wordpress still returned True, and WP came up themeless
    and single-site. Pin the order so this can't regress.
    """

    def test_symlink_created_before_tray_launch(self):
        script = _read("linux/install.sh")
        ln_match = re.search(
            r'ln\s+-sf\s+[^\n]*\s/usr/local/bin/onionpress\b', script)
        tray_launch = re.search(
            r'run_as_user\s+["\$\w/]*onionpress-tray', script)
        self.assertIsNotNone(
            ln_match,
            "install.sh must symlink onionpress into /usr/local/bin/.",
        )
        self.assertIsNotNone(
            tray_launch,
            "install.sh must launch the tray via run_as_user.",
        )
        self.assertLess(
            ln_match.start(), tray_launch.start(),
            "/usr/local/bin/onionpress symlink must be created BEFORE the "
            "tray is launched — SetupDialog shells out to that path to "
            "install the theme + convert to multisite. If the symlink "
            "isn't there yet and the user clicks Setup fast, the post-"
            "install step silently fails and WP comes up themeless.",
        )


class TestLinuxProvisionPostInstallOrder(unittest.TestCase):
    """Guard the ensure_multisite → install_multisite_domain_map order.

    Incident: provision-post-install ran install_multisite_domain_map
    (which sets SUNRISE=true and drops sunrise.php) BEFORE ensure_multisite
    (which runs `wp core multisite-convert`). sunrise.php queries wp_site
    on every WP load; with the constant set but the table missing, every
    subsequent wp-cli call errored, ensure_multisite's wp_is_installed
    guard returned false, and install_onionpress_theme silently skipped.
    The Mac launcher has the correct order; the Linux one didn't.
    """

    def _ordered_calls(self, body):
        # Position of the first ensure_multisite vs install_multisite_domain_map
        # call inside the given body of bash. Skip comments.
        em = re.search(r'^\s*ensure_multisite\b', body, re.MULTILINE)
        mdm = re.search(r'^\s*install_multisite_domain_map\b', body, re.MULTILINE)
        theme = re.search(r'^\s*install_onionpress_theme\b', body, re.MULTILINE)
        return em, mdm, theme

    def test_provision_post_install_subcommand_order(self):
        script = _read("linux/onionpress")
        # Carve out the `provision-post-install)` case body.
        m = re.search(
            r'provision-post-install\)(.*?);;', script, re.DOTALL)
        self.assertIsNotNone(
            m, "provision-post-install subcommand must exist in linux/onionpress")
        em, mdm, theme = self._ordered_calls(m.group(1))
        for name, match in [
            ("ensure_multisite", em),
            ("install_multisite_domain_map", mdm),
            ("install_onionpress_theme", theme),
        ]:
            self.assertIsNotNone(
                match, f"{name} must be called from provision-post-install")
        self.assertLess(
            em.start(), mdm.start(),
            "ensure_multisite must run BEFORE install_multisite_domain_map — "
            "the latter sets SUNRISE+sunrise.php, which queries wp_site on "
            "every WP load. Reversed, every subsequent wp-cli call breaks.",
        )
        self.assertLess(
            mdm.start(), theme.start(),
            "install_multisite_domain_map must run before install_onionpress_"
            "theme (sunrise.php needed for the per-onion domain rewrites).",
        )

    def test_start_containers_runs_provision_steps_in_order(self):
        script = _read("linux/onionpress")
        m = re.search(
            r'start_containers\(\)\s*\{(.*?)^\}', script, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(
            m, "start_containers function must exist in linux/onionpress")
        em, mdm, theme = self._ordered_calls(m.group(1))
        # All three should be called inside start_containers (gated on
        # wp_is_installed). Order must match provision-post-install.
        for name, match in [
            ("ensure_multisite", em),
            ("install_multisite_domain_map", mdm),
            ("install_onionpress_theme", theme),
        ]:
            self.assertIsNotNone(
                match, f"{name} must be called from start_containers")
        self.assertLess(em.start(), mdm.start())
        self.assertLess(mdm.start(), theme.start())


class TestLinuxReinstallBrowserOpen(unittest.TestCase):
    """Guard the fix for browser not auto-opening after reinstall.

    Incident: on reinstall the launcher imports the existing onion key and
    writes ~/.onionpress/onion_address before Python starts, so
    _had_cached_address=True even on a first run.  The old guard
    `auto_opened_browser = self._had_cached_address` then silently skipped
    the browser open.  The fix ANDs in `not self._is_first_run` so a first
    run always opens the browser regardless of a pre-written address file.
    """

    def test_auto_opened_browser_respects_first_run(self):
        src = _read("src/menubar.py")
        # The assignment must include the _is_first_run guard.
        self.assertRegex(
            src,
            r'self\.auto_opened_browser\s*=\s*self\._had_cached_address\s+and\s+not\s+self\._is_first_run',
            "auto_opened_browser must be False when _is_first_run is True, "
            "regardless of _had_cached_address — a reinstall imports the "
            "existing key before Python starts, making _had_cached_address "
            "True even on a fresh install, which suppressed the browser open.",
        )


if __name__ == "__main__":
    unittest.main()
