"""Tests for src/onionpress/setup_logic.py.

Focused on install_fresh_wordpress() — the shared install path used by
the GTK SetupDialog, Mac SetupWindow, and `onionpress setup` SSH CLI.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from onionpress import setup_logic  # noqa: E402


def _ok(stdout=""):
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr="",
    )


def _fail(stderr="error", code=1):
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout="", stderr=stderr,
    )


class TestInstallFreshWordpress(unittest.TestCase):
    """install_fresh_wordpress: wp core install + user_url + post-install."""

    def setUp(self):
        # Each test gets a temp data_dir so the ONIONNAME config write
        # doesn't escape into the user's real ~/.onionpress.
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(self._rmtree)

    def _rmtree(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run(self, **kwargs):
        defaults = dict(
            site_title="My Blog",
            onionname="alice",
            password="hunter22",
            onion_addr="abc.onion",
            launcher_bin="/usr/local/bin/onionpress",
            data_dir=self.tmpdir,
        )
        defaults.update(kwargs)
        return setup_logic.install_fresh_wordpress(**defaults)

    def test_happy_path_invokes_wp_core_install(self):
        with mock.patch(
            "onionpress.setup_logic.subprocess.run", return_value=_ok(),
        ) as mrun:
            ok = self._run()
        self.assertTrue(ok)
        # First call must be `wp core install` with the user-typed creds.
        first_call_args = mrun.call_args_list[0].args[0]
        self.assertIn("core", first_call_args)
        self.assertIn("install", first_call_args)
        self.assertIn("--admin_user=alice", first_call_args)
        self.assertIn("--admin_password=hunter22", first_call_args)
        self.assertIn("--url=http://abc.onion", first_call_args)
        self.assertIn("--title=My Blog", first_call_args)

    def test_failure_at_wp_core_install_returns_false(self):
        with mock.patch(
            "onionpress.setup_logic.subprocess.run",
            return_value=_fail("DB connection refused"),
        ):
            ok = self._run()
        self.assertFalse(ok)

    def test_post_install_subcommand_invoked_with_launcher_bin(self):
        # Capture every subprocess.run call; assert one of them invokes
        # the launcher's provision-post-install subcommand.
        with mock.patch(
            "onionpress.setup_logic.subprocess.run", return_value=_ok(),
        ) as mrun:
            self._run(launcher_bin="/opt/onionpress/onionpress")
        post_install_calls = [
            c for c in mrun.call_args_list
            if c.args and isinstance(c.args[0], list)
            and "provision-post-install" in c.args[0]
        ]
        self.assertEqual(len(post_install_calls), 1)
        self.assertEqual(
            post_install_calls[0].args[0],
            ["/opt/onionpress/onionpress", "provision-post-install"],
        )

    def test_post_install_skipped_when_launcher_bin_missing(self):
        with mock.patch(
            "onionpress.setup_logic.subprocess.run", return_value=_ok(),
        ) as mrun:
            self._run(launcher_bin=None)
        # No `provision-post-install` should be invoked.
        for c in mrun.call_args_list:
            argv = c.args[0] if c.args else []
            self.assertNotIn(
                "provision-post-install", argv,
                "launcher_bin=None should suppress the post-install hop",
            )

    def test_onionname_persisted_to_config(self):
        with mock.patch(
            "onionpress.setup_logic.subprocess.run", return_value=_ok(),
        ):
            ok = self._run(onionname="bob")
        self.assertTrue(ok)
        with open(os.path.join(self.tmpdir, "config")) as f:
            contents = f.read()
        self.assertIn("ONIONNAME=bob", contents)

    def test_user_url_update_uses_per_user_path(self):
        # The "Website" link on the WP user profile should point to
        # http://<onion>/<onionname>/ not the bare onion root.
        with mock.patch(
            "onionpress.setup_logic.subprocess.run", return_value=_ok(),
        ) as mrun:
            self._run(onionname="alice", onion_addr="abc.onion")
        user_url_calls = [
            c for c in mrun.call_args_list
            if c.args and isinstance(c.args[0], list)
            and "user" in c.args[0] and "update" in c.args[0]
            and any("user_url" in a for a in c.args[0])
        ]
        self.assertTrue(user_url_calls)
        argv = user_url_calls[0].args[0]
        self.assertIn("--user_url=http://abc.onion/alice/", argv)


class TestNoBootstrapPasswordOnNewInstalls(unittest.TestCase):
    """Invariant: the bash launcher must no longer auto-write
    ~/.onionpress/wp-admin-password on non-interactive (systemd) start.

    Tested by grepping the launcher source for the abandoned pattern.
    If a future refactor needs to re-introduce a bootstrap password,
    delete this test deliberately rather than working around it.
    """

    def test_launcher_does_not_echo_password_to_data_dir(self):
        launcher_path = os.path.join(
            os.path.dirname(__file__), "..", "linux", "onionpress",
        )
        with open(launcher_path) as f:
            src = f.read()
        self.assertNotIn(
            'echo "$auto_pass" > "$DATA_DIR/wp-admin-password"', src,
            "bash launcher should not auto-generate + persist a "
            "bootstrap WP admin password on first systemd start; "
            "the SetupDialog / `onionpress setup` does the install now",
        )


if __name__ == "__main__":
    unittest.main()
