#!/usr/bin/env python3
"""Integration tests for the Wayback archive plugin's sweep engine.

These drive the live plugin inside the onionpress-wordpress container
via `wp eval`, using the mock filter hooks we added to short-circuit
every network-touching function (user_status, submit, poll, cdx,
self_reachable) — no real Tor/SPN traffic.

Coverage focus: behaviors that are easy to break during refactors.
  1. Queue totals aggregate across every subsite in the network.
  2. CDX rescue: SPN flips success->error, CDX still has a capture;
     the post must end up archived via the CDX timestamp, not errored.
  3. Young-job skip: a job submitted in the last 15s must NOT be
     polled (wastes a Tor round-trip on a guaranteed "pending").
  4. Submit path: a fresh post with no job_id gets one, with a
     matching submitted_at, on a successful submit.
  5. Lock mutex: a fresh lock blocks a second sweep invocation.

Prerequisites (skips the suite if any fails):
  - Docker running
  - `onionpress-wordpress` container up with the wayback plugin
    present in mu-plugins/
  - At least one subsite to target
"""

import json
import shutil
import subprocess
import time
import unittest

_WP = "onionpress-wordpress"


def _docker_exec(args, **kwargs):
    return subprocess.run(
        ["docker", "exec", _WP] + args,
        capture_output=True, text=True, encoding='utf-8',
        errors='replace', **kwargs,
    )


def _wp(args, url=None, **kwargs):
    cmd = ["wp"] + args + ["--path=/var/www/html", "--allow-root"]
    if url:
        cmd.append("--url=" + url)
    return _docker_exec(cmd, **kwargs)


def _docker_available():
    if not shutil.which("docker"):
        return False
    r = subprocess.run(
        ["docker", "inspect", _WP, "--format={{.State.Running}}"],
        capture_output=True, text=True, timeout=10,
    )
    return r.returncode == 0 and "true" in r.stdout


def _pick_site():
    r = _wp(["site", "list", "--fields=blog_id,path,url", "--format=json"],
            timeout=15)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    sites = json.loads(r.stdout)
    sub = [s for s in sites if s.get("path") != "/"]
    return sub[0] if sub else (sites[0] if sites else None)


def _eval(php, url):
    """Run PHP inside WP, return stdout (stripped)."""
    r = _wp(["eval", php], url=url, timeout=90)
    return r.stdout.strip()


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestWaybackQueueTotals(unittest.TestCase):
    """Queue totals aggregate correctly across every subsite."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def test_totals_structure_and_aggregate(self):
        """Totals come back as expected, with the remaining invariant holding."""
        php = """
        $t = onionpress_wayback_queue_totals();
        echo json_encode($t);
        """
        out = _eval(php, self.url)
        totals = json.loads(out)
        for k in ("archived", "in_flight", "remaining", "total"):
            self.assertIn(k, totals, f"missing key: {k}")
            self.assertIsInstance(totals[k], int)
        # remaining = max(0, total - archived - in_flight).
        self.assertEqual(
            totals["remaining"],
            max(0, totals["total"] - totals["archived"] - totals["in_flight"]),
        )
        # Aggregated total must be >= this subsite alone.
        php_one = """
        global $wpdb;
        echo (int) $wpdb->get_var(
            "SELECT COUNT(*) FROM $wpdb->posts WHERE post_status='publish' "
            . "AND post_type IN ('post','page')"
        );
        """
        one = int(_eval(php_one, self.url))
        self.assertGreaterEqual(totals["total"], one,
            f"aggregated total {totals['total']} < this subsite's {one}")


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestWaybackSweepIteration(unittest.TestCase):
    """Sweep iteration behavior with mocked network functions."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def setUp(self):
        _wp(["option", "delete", "op_wayback_backoff_until"],
            url=self.url, timeout=15)
        r = _wp(["post", "create", "--post_type=post", "--post_status=publish",
                 "--post_title=wayback-test-" + self._testMethodName,
                 "--porcelain"], url=self.url, timeout=15)
        self.post_id = int(r.stdout.strip())
        self.addCleanup(self._cleanup_post)

    def _cleanup_post(self):
        _wp(["post", "delete", str(self.post_id), "--force"],
            url=self.url, timeout=15)

    def _set_meta(self, key, value):
        _wp(["post", "meta", "update", str(self.post_id), key, str(value)],
            url=self.url, timeout=15)

    def _get_meta(self, key):
        r = _wp(["post", "meta", "get", str(self.post_id), key],
                url=self.url, timeout=15)
        return r.stdout.strip()

    def _common_mocks(self, available=40):
        """Short-circuit reachability + user_status so the iteration
        reaches the poll/submit phases."""
        return f"""
        add_filter('onionpress_wayback_self_reachable_mock',
                   function() {{ return true; }});
        add_filter('onionpress_wayback_user_status_mock',
                   function() {{ return array('available' => {available}, 'processing' => 0); }});
        """

    def test_cdx_rescues_spn_error(self):
        """SPN flips success->error; CDX still has capture -> post archived via CDX."""
        self._set_meta("_op_wayback_job_id", "jid-cdx-test")
        self._set_meta("_op_wayback_submitted_at", str(int(time.time()) - 120))

        php = self._common_mocks() + """
        add_filter('onionpress_wayback_poll_parallel_mock',
                   function($_, $job_ids) {
            return array(array(
                'job_id'     => 'jid-cdx-test',
                'status'     => 'error',
                'status_ext' => 'error:no-captures',
            ));
        }, 10, 2);
        add_filter('onionpress_wayback_cdx_lookup_parallel_mock',
                   function($_, $urls) {
            $out = array();
            foreach ($urls as $k => $v) { $out[$k] = '20260101120000'; }
            return $out;
        }, 10, 2);
        add_filter('onionpress_wayback_submit_parallel_mock',
                   function($_, $urls) {
            return array_fill_keys(array_keys($urls), '');
        }, 10, 2);
        onionpress_wayback_sweep_iteration();
        echo 'ok';
        """
        _eval(php, self.url)
        self.assertNotEqual("", self._get_meta("_op_wayback_archived_at"),
            "post should be archived via CDX rescue")
        self.assertEqual("20260101120000", self._get_meta("_op_wayback_snapshot_ts"),
            "snapshot_ts should come from the CDX timestamp")
        self.assertEqual("", self._get_meta("_op_wayback_job_id"),
            "job_id should be cleared after success")

    def test_young_job_is_not_polled(self):
        """A job submitted < YOUNG_JOB_SKIP_SEC ago MUST NOT be polled."""
        self._set_meta("_op_wayback_job_id", "jid-young-test")
        self._set_meta("_op_wayback_submitted_at", str(int(time.time())))

        php = self._common_mocks() + """
        delete_option('op_test_wb_poll_called_with');
        add_filter('onionpress_wayback_poll_parallel_mock',
                   function($_, $job_ids) {
            update_option('op_test_wb_poll_called_with',
                          implode(',', $job_ids), false);
            return array();
        }, 10, 2);
        add_filter('onionpress_wayback_submit_parallel_mock',
                   function($_, $urls) {
            return array_fill_keys(array_keys($urls), '');
        }, 10, 2);
        onionpress_wayback_sweep_iteration();
        echo (string) get_option('op_test_wb_poll_called_with', '(unset)');
        """
        out = _eval(php, self.url)
        self.assertNotIn("jid-young-test", out,
            f"young job should not be polled; poll got: {out}")

    def test_submit_assigns_job_id(self):
        """A fresh post (no job_id) gets one on a successful submit."""
        php = self._common_mocks() + f"""
        add_filter('onionpress_wayback_poll_parallel_mock',
                   function($_, $job_ids) {{ return array(); }}, 10, 2);
        add_filter('onionpress_wayback_submit_parallel_mock',
                   function($_, $urls) {{
            $out = array();
            foreach ($urls as $k => $v) {{
                $out[$k] = ($k === 'post:{self.post_id}')
                    ? 'jid-submit-test'
                    : '';
            }}
            return $out;
        }}, 10, 2);
        onionpress_wayback_sweep_iteration();
        echo 'ok';
        """
        _eval(php, self.url)
        self.assertEqual("jid-submit-test", self._get_meta("_op_wayback_job_id"),
            "post should have received the mocked job_id")
        submitted_at = self._get_meta("_op_wayback_submitted_at")
        self.assertNotEqual("", submitted_at, "submitted_at should be set")
        self.assertGreater(int(submitted_at), int(time.time()) - 60)


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestWaybackSweepLock(unittest.TestCase):
    """Token-lock mutex semantics for the sweep entry point."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def setUp(self):
        _wp(["option", "delete", "op_wayback_sweep_lock"],
            url=self.url, timeout=15)

    def tearDown(self):
        _wp(["option", "delete", "op_wayback_sweep_lock"],
            url=self.url, timeout=15)

    def test_fresh_lock_blocks_second_invocation(self):
        """A fresh lock (< STALE threshold) rejects a new sweep."""
        php_seed = """
        update_option('op_wayback_sweep_lock',
                      'otherTok:' . time(), false);
        echo 'seeded';
        """
        _eval(php_seed, self.url)
        php = """
        add_filter('onionpress_wayback_self_reachable_mock',
                   function() { return true; });
        add_filter('onionpress_wayback_user_status_mock',
                   function() { return array('available' => 0); });
        onionpress_wayback_sweep();
        echo (string) get_option('op_wayback_sweep_lock', '(empty)');
        """
        out = _eval(php, self.url)
        self.assertTrue(out.startswith("otherTok:"),
            f"lock should still belong to otherTok: {out}")


if __name__ == "__main__":
    unittest.main()
