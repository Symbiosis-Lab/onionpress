#!/usr/bin/env python3
"""Integration tests for the Wayback archive plugin's sweep engine.

These drive the live plugin inside the onionpress-wordpress container
via `wp eval`, using the mock filter hooks we added to short-circuit
every network-touching function (user_status, submit, poll, cdx,
self_reachable) — no real Tor/SPN traffic.

Coverage focus: behaviors that are easy to break during refactors.
  1. Queue totals aggregate across every subsite in the network.
  2. Young-job skip: a job submitted in the last 15s must NOT be
     polled (wastes a Tor round-trip on a guaranteed "pending").
  3. Submit path: a fresh post with no job_id gets one, with a
     matching submitted_at, on a successful submit.
  4. Lock mutex: a fresh lock blocks a second sweep invocation.

The rest guard the ways this engine has actually wedged in production,
all of which shared one shape — it kept logging a healthy sweep while
archiving nothing:
  5. A job SPN has FORGOTTEN (absent from /save/status, not "pending"
     or "error") must be cleared, or that URL deadlocks permanently.
  6. A job whose status batch never came back must NOT be cleared —
     failing to ask is not the same as being told it is gone.
  7. CDX rescue + the $answered guard, in one iteration: an SPN "error"
     must be verified against CDX before being written off, and must
     not be swept up as forgotten on the way there.
  8. Coverage bookkeeping in poll_parallel itself, driven through the
     curl seam rather than mocked past: only a batch that came back as
     a JSON list of statuses counts as an answer about its job_ids.
  9. The daemon must recycle on a timer and hand its lock back. It ran
     70 hours in one PHP request, serving option reads from a cache
     that predated the fix being applied to the database.
 10. The pages a moss generation publishes must be work items, not just
     files on disk. This site reported archived=5/5 while archiving none
     of its own content: the five were leftover WordPress defaults, and
     the moss pages a reader actually sees had never been in the queue.
 11. ...and they belong to ONE site. The generation is container-global
     while its state option is per-subsite, so enumerating it on every
     subsite submitted every page N times over.
 12. Enumeration must survive a directory it cannot read, and must not be
     repeated three times per subsite per iteration.

And one about the suite itself: it must be structurally incapable of
making a real Save Page Now capture. See TestOutboundHttpIsBlocked and the
_HTTP_GUARD_PHP comment — the mocks below cover the sweep's own network
calls, but a published post reaches SPN by a path no filter in this process
can see.

Prerequisites (skips the suite if any fails):
  - Docker running
  - `onionpress-wordpress` container up with the wayback plugin
    present in mu-plugins/
  - At least one subsite to target
"""

import base64
import json
import shutil
import subprocess
import time
import unittest

_WP = "onionpress-wordpress"

# ── the outbound-HTTP block ──────────────────────────────────────────────
#
# onionpress_wayback_schedule_sweep_now() ends with a non-blocking
# wp_remote_post() at site_url('wp-cron.php'). Apache answers that inside this
# container, and it is a SEPARATE HTTP request — the add_filter() mocks a
# `wp eval` installs live in THIS process and do not apply to it. What that
# loopback starts is a genuine unmocked daemon: real Tor, real Save Page Now
# captures, against the archive.org account shared by every OnionPress node,
# holding the sweep lock for up to OP_WB_LOOP_MAX_SEC afterwards.
#
# Every content-creating test reaches it. `wp post create --post_status=publish`
# fires save_post -> kick_now (measured: two loopback POSTs per created post),
# and wp_insert_comment fires the same path. It has already happened: captures
# triggered by this suite are in the Wayback Machine.
#
# So the block is not a filter a test opts into — one test remembering is worth
# nothing when the next one forgets. It is a `--require` file loaded by EVERY
# `wp` invocation _wp() makes, which is every way this suite can reach
# WordPress at all. A missing file makes wp-cli exit non-zero on the first
# command rather than quietly restoring the old behaviour, which is the right
# direction to fail in.
_HTTP_GUARD_PATH = "/tmp/op-wayback-test-no-http.php"
_HTTP_GUARD_LOG = "/tmp/op-wayback-test-blocked.log"
_HTTP_GUARD_PHP = """<?php
// Installed by tests/test_wayback_sweep.py. See the comment there.
WP_CLI::add_hook( 'after_wp_load', function () {
    add_filter( 'pre_http_request', function ( $pre, $args, $url ) {
        @file_put_contents( '%s', time() . ' ' . $url . "\\n", FILE_APPEND );
        return new WP_Error( 'op-test-no-outbound-http',
            'the wayback test suite blocks outbound HTTP: ' . $url );
    }, 0, 3 );
} );
""" % _HTTP_GUARD_LOG


def _docker_exec_raw(args, user=None, **kwargs):
    pre = ["docker", "exec"] + (["-u", user] if user else []) + [_WP]
    return subprocess.run(
        pre + args,
        capture_output=True, text=True, encoding='utf-8',
        errors='replace', **kwargs,
    )


def _docker_exec(args, user=None, **kwargs):
    """Run a command in the container. NOT for `wp` — see the refusal below."""
    # Anything that reaches WordPress must go through _wp(), because that is
    # where the outbound-HTTP block is attached. Refusing `wp` here is what
    # keeps the block from being one forgotten helper call away from being
    # off — the same reason it is not a per-test filter.
    if args and args[0] == "wp":
        raise AssertionError(
            "run wp through _wp(), never _docker_exec: _wp attaches the "
            "outbound-HTTP block that stops a test making real Save Page Now "
            "captures")
    return _docker_exec_raw(args, user=user, **kwargs)


def _wp(args, url=None, user=None, **kwargs):
    cmd = ["wp"] + args + ["--path=/var/www/html", "--allow-root",
                           "--require=" + _HTTP_GUARD_PATH]
    if url:
        cmd.append("--url=" + url)
    return _docker_exec_raw(cmd, user=user, **kwargs)


def _docker_available():
    if not shutil.which("docker"):
        return False
    r = subprocess.run(
        ["docker", "inspect", _WP, "--format={{.State.Running}}"],
        capture_output=True, text=True, timeout=10,
    )
    return r.returncode == 0 and "true" in r.stdout


def _install_http_guard():
    """Push the guard into the container before any `wp` runs. Raises rather
    than skipping: a suite that cannot install the block must not be one that
    quietly runs without it."""
    r = subprocess.run(
        ["docker", "exec", "-i", _WP, "sh", "-c", "cat > " + _HTTP_GUARD_PATH],
        input=_HTTP_GUARD_PHP, capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(
            "could not install the outbound-HTTP block at %s: %s"
            % (_HTTP_GUARD_PATH, r.stderr.strip()))


_DOCKER = _docker_available()
if _DOCKER:
    _install_http_guard()


def _sites():
    r = _wp(["site", "list", "--fields=blog_id,path,url", "--format=json"],
            timeout=15)
    if r.returncode != 0 or not r.stdout.strip():
        return []
    return json.loads(r.stdout)


def _pick_site():
    sites = _sites()
    sub = [s for s in sites if s.get("path") != "/"]
    return sub[0] if sub else (sites[0] if sites else None)


def _pick_main_site():
    """The site moss serves. Its generation is a container-global tree of URLs
    rooted at the onion address, so it belongs to the site at '/' and to no
    subsite — see onionpress_wayback_moss_serves_this_site()."""
    for s in _sites():
        if s.get("path") == "/":
            return s
    return None


def _pick_subsite():
    for s in _sites():
        if s.get("path") != "/":
            return s
    return None


def _eval(php, url, user=None):
    """Run PHP inside WP, return stdout (stripped)."""
    r = _wp(["eval", php], url=url, user=user, timeout=90)
    return r.stdout.strip()


# Sitewide capture state lives in wp_options rather than post meta, so unlike
# the disposable post each test creates it belongs to the real site. A test
# that writes it and walks away marks the home page unarchived, and the next
# real sweep spends an SPN slot re-capturing something already in the Wayback
# Machine. Every class that touches these options snapshots them.
_SITEWIDE_OPTIONS = (
    "op_wayback_home_state",
    "op_wayback_feed_state",
    "op_wayback_moss_state",
)


class SitewideStateMixin:
    """Save/restore the sitewide capture options around each test."""

    def snapshot_sitewide(self):
        php = "echo base64_encode(json_encode(array(%s)));" % ",".join(
            "'{0}' => get_option('{0}', null)".format(o) for o in _SITEWIDE_OPTIONS
        )
        saved = _eval(php, self.url)
        self.addCleanup(self._restore_sitewide, saved)

    def _restore_sitewide(self, saved):
        php = """
        $s = json_decode(base64_decode('%s'), true);
        if (!is_array($s)) { echo 'no-snapshot'; return; }
        foreach ($s as $opt => $val) {
            if ($val === null) { delete_option($opt); }
            else { update_option($opt, $val, false); }
        }
        echo 'restored';
        """ % saved
        _eval(php, self.url)


@unittest.skipUnless(_DOCKER, "requires running onionpress-wordpress container")
class TestOutboundHttpIsBlocked(unittest.TestCase):
    """The suite must be structurally incapable of a real Save Page Now
    capture. See the _HTTP_GUARD_PHP comment for the mechanism it defeats.

    This is a test about the harness, and it earns its place because the
    failure it guards is silent: nothing about a test run that spawned a real
    daemon looks different from one that did not, and the evidence turns up
    days later as captures in the Wayback Machine and a shared account's
    slots spent on someone else's behalf.
    """

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def test_the_wordpress_http_api_is_blocked(self):
        out = _eval("""
        $r = wp_remote_post(site_url('wp-cron.php?doing_wp_cron=' . microtime(true)),
                            array('timeout' => 0.01, 'blocking' => false));
        echo is_wp_error($r) ? 'blocked:' . $r->get_error_code() : 'ALLOWED';
        """, self.url)
        self.assertEqual("blocked:op-test-no-outbound-http", out,
            "wp_remote_post reached the network from a test process; every "
            "sweep-triggering test is then one loopback away from a real "
            "unmocked daemon")

    def test_publishing_a_post_cannot_start_a_real_sweep(self):
        """The block has to sit on the path that actually fires it, which is
        NOT `wp eval`: `wp post create` runs save_post -> kick_now in its own
        process, where no filter a test installs can reach. Asserting that the
        loopback was attempted AND blocked is what tells the guard apart from
        a guard that happens to be pointed somewhere harmless."""
        _docker_exec(["sh", "-c", "> " + _HTTP_GUARD_LOG])
        r = _wp(["post", "create", "--post_type=post", "--post_status=publish",
                 "--post_title=wayback-http-guard-probe", "--porcelain"],
                url=self.url, timeout=15)
        pid = int(r.stdout.strip())
        self.addCleanup(lambda: _wp(["post", "delete", str(pid), "--force"],
                                    url=self.url, timeout=15))
        blocked = _docker_exec(["cat", _HTTP_GUARD_LOG]).stdout
        self.assertIn("wp-cron.php", blocked,
            "publishing a post did not even attempt the wp-cron loopback — "
            "either the trigger moved or the guard is not loaded, and both "
            "mean this suite's safety is no longer being verified")


@unittest.skipUnless(_DOCKER, "requires running onionpress-wordpress container")
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


@unittest.skipUnless(_DOCKER, "requires running onionpress-wordpress container")
class TestWaybackSweepIteration(SitewideStateMixin, unittest.TestCase):
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
        self.snapshot_sitewide()
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
        """Short-circuit every network seam so the iteration reaches the
        poll/submit phases without touching Tor or SPN.

        The submit no-op is a DEFAULT rather than something each test opts
        into, because the cost of forgetting it is not a slow test — it is
        real captures against the Save Page Now account shared by every
        OnionPress node. One test here did forget, and the captures it
        triggered are in the Wayback Machine. A test that wants a different
        submit result adds its own filter after this one, which wins by
        running later on the same hook.

        Only the submit is defaulted. Poll and CDX are reads: leaving them
        live costs a Tor round-trip, not a capture, and defaulting them
        would quietly change what "no answer" means for the tests built
        around that distinction.
        """
        return f"""
        add_filter('onionpress_wayback_self_reachable_mock',
                   function() {{ return true; }});
        add_filter('onionpress_wayback_user_status_mock',
                   function() {{ return array('available' => {available}, 'processing' => 0); }});
        add_filter('onionpress_wayback_submit_parallel_mock',
                   function($_, $urls) {{ return array_fill_keys(array_keys($urls), ''); }}, 10, 2);
        """

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

    def test_job_spn_has_forgotten_is_cleared(self):
        """SPN has a behavior its API doesn't document: a job_id it has
        entirely forgotten comes back ABSENT from /save/status rather than
        as 'pending' or 'error'. Every finalize branch keys off a returned
        status dict, so such a job matched nothing, kept its job_id, and
        was skipped by the submit step forever. This site's home and feed
        sat that way for five days, archiving nothing, while every sweep
        logged a healthy avail=40.
        """
        self._set_meta("_op_wayback_job_id", "jid-forgotten-test")
        self._set_meta("_op_wayback_submitted_at", str(int(time.time()) - 4000))

        php = self._common_mocks() + """
        add_filter('onionpress_wayback_poll_parallel_mock',
                   function($_, $job_ids) { return array(); }, 10, 2);
        add_filter('onionpress_wayback_submit_parallel_mock',
                   function($_, $urls) {
            return array_fill_keys(array_keys($urls), '');
        }, 10, 2);
        onionpress_wayback_sweep_iteration();
        echo 'ok';
        """
        _eval(php, self.url)
        # The submit mock returns '' for everything, so nothing re-flights
        # and the outcome is exactly "cleared" — assert that, not merely
        # "changed". Both halves of the write matter: a surviving
        # submitted_at with no job_id would make the record look freshly
        # submitted to the age checks.
        self.assertEqual("", self._get_meta("_op_wayback_job_id"),
            "a job_id SPN no longer knows must not survive the sweep — "
            "keeping it deadlocks this URL permanently")
        self.assertEqual("", self._get_meta("_op_wayback_submitted_at"),
            "submitted_at must be cleared alongside the job_id")

    def test_cdx_rescue_and_the_answered_guard(self):
        """Two behaviours that only separate when they run in one iteration.

        1. CDX rescue: SPN flips success->error while the capture is still
           in CDX, so an errored job must be verified against CDX before
           being written off, and archived from the CDX timestamp.
        2. The $answered guard: the forgotten-sweep runs BEFORE that rescue,
           so a job SPN did answer for must be excluded from it. The guard
           is what protects the errored records the over-budget CDX path
           deliberately leaves in flight — without it the next sweep clears
           exactly those job_ids and the deferral means nothing.

        Asserting on the returned counters, not just the final meta, is
        the point. Both paths end up writing the same archived_at and
        snapshot_ts, so a state-only assertion passes with the guard
        deleted: the forgotten-sweep clears the job_id, then the rescue —
        holding its own pre-loop snapshot of the errored records —
        overwrites the result. `forgotten` is the only observable that
        tells the two apart.
        """
        self._set_meta("_op_wayback_job_id", "jid-answered-test")
        self._set_meta("_op_wayback_submitted_at", str(int(time.time()) - 4000))

        # A second in-flight record SPN says nothing about, so the same
        # iteration exercises both branches.
        php = self._common_mocks() + """
        update_option('op_wayback_home_state',
                      array('job_id' => 'jid-silent-test',
                            'submitted_at' => time() - 4000), false);
        add_filter('onionpress_wayback_poll_parallel_mock',
                   function($_, $job_ids) {
            return array(array(
                'job_id'     => 'jid-answered-test',
                'status'     => 'error',
                'status_ext' => 'error:no-captures',
            ));
        }, 10, 2);
        add_filter('onionpress_wayback_cdx_lookup_parallel_mock',
                   function($_, $urls) {
            $out = array();
            foreach ($urls as $k => $v) { $out[$k] = '20260202120000'; }
            return $out;
        }, 10, 2);
        add_filter('onionpress_wayback_submit_parallel_mock',
                   function($_, $urls) {
            return array_fill_keys(array_keys($urls), '');
        }, 10, 2);
        $stats = onionpress_wayback_sweep_iteration();
        $home = get_option('op_wayback_home_state', array());
        $stats['home_job'] = $home['job_id'] ?? '';
        echo json_encode($stats);
        """
        stats = json.loads(_eval(php, self.url))

        # Exactly one job was forgotten — the silent one. Two would mean
        # the answered job was swept up as well.
        self.assertEqual(1, stats["forgotten"],
            "only the job SPN stayed silent about may count as forgotten; "
            f"got {stats['forgotten']} — the $answered guard is not holding")
        self.assertEqual(1, stats["cdx"],
            f"the answered job should have been rescued via CDX; got {stats}")
        # ...and the rescue wrote a real record, from the CDX timestamp.
        self.assertEqual("20260202120000", self._get_meta("_op_wayback_snapshot_ts"),
            "snapshot_ts should come from the CDX timestamp")
        self.assertNotEqual("", self._get_meta("_op_wayback_archived_at"),
            "the answered job should end up archived via CDX")
        self.assertEqual("", self._get_meta("_op_wayback_job_id"),
            "job_id should be cleared once the capture is recorded")
        # The job SPN stayed silent about is the one that gets cleared.
        self.assertEqual("", stats["home_job"],
            f"the unanswered job should have been cleared; got: {stats}")

    def test_job_whose_batch_never_answered_is_not_cleared(self):
        """poll_parallel chunks job_ids 20 at a time and silently drops any
        batch that fails — a non-200 or unparseable body contributes nothing
        to the return value and leaves no trace. Age alone cannot tell that
        apart from amnesia, so one 40s Tor timeout would reclassify a whole
        batch of old jobs as forgotten and resubmit all of them.

        Coverage tracking is the fix: only jobs whose batch actually came
        back count as answered-about. Here the job is old enough to clear
        but its batch never returned, so it must be left alone.
        """
        self._set_meta("_op_wayback_job_id", "jid-lost-batch-test")
        self._set_meta("_op_wayback_submitted_at", str(int(time.time()) - 4000))

        php = self._common_mocks() + """
        add_filter('onionpress_wayback_poll_parallel_mock',
                   function($_, $job_ids) { return array(); }, 10, 2);
        // No batch came back, so nothing is covered.
        add_filter('onionpress_wayback_poll_covered_mock',
                   function($_, $job_ids) { return array(); }, 10, 2);
        add_filter('onionpress_wayback_submit_parallel_mock',
                   function($_, $urls) {
            return array_fill_keys(array_keys($urls), '');
        }, 10, 2);
        onionpress_wayback_sweep_iteration();
        echo 'ok';
        """
        _eval(php, self.url)
        self.assertEqual("jid-lost-batch-test", self._get_meta("_op_wayback_job_id"),
            "a job whose status batch never came back must keep its job_id — "
            "we did not learn that SPN forgot it, only that we failed to ask")
        self.assertNotEqual("", self._get_meta("_op_wayback_submitted_at"),
            "submitted_at must survive alongside the job_id — clearing it "
            "alone would make the record read as a zombie on the next sweep "
            "and get it cleared there instead")

    def test_recently_submitted_job_survives_an_empty_poll(self):
        """A guard against over-clearing, not a regression test — it passes
        against the pre-fix plugin too, which cleared nothing at all.

        poll_parallel returns [] both when SPN forgot the jobs and when the
        request itself failed. Coverage tracking now separates those, but
        the age gate is the second line of defence: a job younger than the
        threshold at which a *pending* job would be resubmitted must
        survive a blank response, or one Tor blip resubmits everything.
        """
        self._set_meta("_op_wayback_job_id", "jid-fresh-test")
        self._set_meta("_op_wayback_submitted_at", str(int(time.time()) - 30))

        php = self._common_mocks() + """
        add_filter('onionpress_wayback_poll_parallel_mock',
                   function($_, $job_ids) { return array(); }, 10, 2);
        add_filter('onionpress_wayback_submit_parallel_mock',
                   function($_, $urls) {
            return array_fill_keys(array_keys($urls), '');
        }, 10, 2);
        onionpress_wayback_sweep_iteration();
        echo 'ok';
        """
        _eval(php, self.url)
        self.assertEqual("jid-fresh-test", self._get_meta("_op_wayback_job_id"),
            "a young job must survive an empty poll — it is far more likely "
            "the poll failed than that SPN forgot a job submitted seconds ago")

    def _submit_everything_mock(self):
        """Hand every submitted URL a job_id, so 'did this get submitted?'
        is answerable by looking at the post's meta afterwards."""
        return """
        add_filter('onionpress_wayback_submit_parallel_mock',
                   function($_, $urls) {
            $out = array();
            foreach ($urls as $k => $u) { $out[$k] = 'jid-submitted-' . md5($u); }
            return $out;
        }, 10, 2);
        """

    def test_an_error_cdx_cannot_rescue_is_recorded_as_a_failure(self):
        """The other side of the CDX rescue, and the only path that reaches
        finalize_error. The rescue test above covers the CDX *hit*, so this
        branch had never once run under test — and it crashed the whole
        sweep the first time it did on the live stack, on an in-flight
        record that carries no read callable. The counter that drives the
        retry back-off is written here or nowhere.
        """
        self._set_meta("_op_wayback_job_id", "jid-unrescuable-test")
        self._set_meta("_op_wayback_submitted_at", str(int(time.time()) - 4000))
        self._set_meta("_op_wayback_error_count", "2")

        php = self._common_mocks() + """
        add_filter('onionpress_wayback_poll_parallel_mock', function($_, $job_ids) {
            return array(array(
                'job_id'     => 'jid-unrescuable-test',
                'status'     => 'error',
                'status_ext' => 'error:no-captures',
            ));
        }, 10, 2);
        add_filter('onionpress_wayback_poll_covered_mock',
                   function($_, $job_ids) { return $job_ids; }, 10, 2);
        // CDX has nothing: the capture really is lost.
        add_filter('onionpress_wayback_cdx_lookup_parallel_mock',
                   function($_, $urls) { return array(); }, 10, 2);
        add_filter('onionpress_wayback_submit_parallel_mock', function($_, $urls) {
            return array_fill_keys(array_keys($urls), '');
        }, 10, 2);
        echo json_encode(onionpress_wayback_sweep_iteration());
        """
        stats = json.loads(_eval(php, self.url))
        self.assertEqual(1, stats["error"])
        self.assertEqual("", self._get_meta("_op_wayback_job_id"),
                         "an unrescuable job must be cleared for a later retry")
        self.assertEqual("3", self._get_meta("_op_wayback_error_count"),
                         "the failure count must advance, or the back-off never grows")
        self.assertNotEqual("", self._get_meta("_op_wayback_last_error_at"))

    def test_a_failure_stamped_mid_iteration_still_counts_as_cooling(self):
        """The poll and the submit are two phases of ONE iteration, and the
        sweep captures $now once at the top — before the poll that records
        a failure. So a last_error_at written by that poll is legitimately
        LATER than the $now the submit phase compares against, with no
        clock skew involved. An escape hatch that treats any future
        timestamp as ready therefore lets a URL that just failed be
        resubmitted by the very iteration that failed it.

        Observed live: the feed came back from one sweep with a new job_id
        and error_count already at 2. It does not reproduce in a mocked
        iteration, where every phase lands in the same second — it needed
        the real one that ran 76s — so the property is asserted directly.
        Only a jump bigger than the back-off itself is skew rather than
        ordering.
        """
        verdicts = json.loads(_eval("""
        $now   = time();
        $delay = onionpress_wayback_retry_delay(1);
        echo json_encode(array(
            // Stamped a phase later than the caller's clock: ordering.
            'one_phase_ahead'  => onionpress_wayback_retry_ready(
                array('last_error_at' => $now + 80, 'error_count' => 1), $now),
            'just_now'         => onionpress_wayback_retry_ready(
                array('last_error_at' => $now, 'error_count' => 1), $now),
            'cooled_off'       => onionpress_wayback_retry_ready(
                array('last_error_at' => $now - $delay, 'error_count' => 1), $now),
            // Beyond any ordering explanation: the clock moved backwards.
            'clock_went_back'  => onionpress_wayback_retry_ready(
                array('last_error_at' => $now + $delay * 10, 'error_count' => 1), $now),
        ));
        """, self.url))
        self.assertFalse(verdicts["one_phase_ahead"],
            "a failure stamped mid-iteration must still be cooling — this is "
            "the resubmit-in-the-same-sweep bug")
        self.assertFalse(verdicts["just_now"])
        self.assertTrue(verdicts["cooled_off"])
        self.assertTrue(verdicts["clock_went_back"],
            "a genuine backwards clock jump must not strand the record until "
            "the clock catches up")

    def test_a_failed_capture_is_not_resubmitted_on_the_next_sweep(self):
        """The retry storm. A URL SPN failed returns to "no archived_at, no
        job_id" — exactly what posts_needing_submit selects on — so before
        the back-off it was resubmitted every sweep for as long as it kept
        failing. Measured live: 17 submissions, 9 errors, 2.4/min, a 100%
        failure rate, against an SPN account shared by every OnionPress
        node. The cost of that lands on other people's sites.
        """
        self._set_meta("_op_wayback_last_error_at", str(int(time.time())))
        self._set_meta("_op_wayback_error_count", "1")

        _eval(self._common_mocks() + self._submit_everything_mock()
              + "onionpress_wayback_sweep_iteration(); echo 'ok';", self.url)
        self.assertEqual("", self._get_meta("_op_wayback_job_id"),
            "a URL that failed seconds ago must not be resubmitted this sweep")

    def test_a_cooled_off_failure_is_retried(self):
        """The other half: the back-off must expire. A capture that failed
        for a transient reason has to get another chance, or one bad sweep
        retires a URL permanently."""
        self._set_meta("_op_wayback_last_error_at",
                       str(int(time.time()) - 301))   # just past RETRY_BASE_SEC
        self._set_meta("_op_wayback_error_count", "1")

        _eval(self._common_mocks() + self._submit_everything_mock()
              + "onionpress_wayback_sweep_iteration(); echo 'ok';", self.url)
        self.assertTrue(self._get_meta("_op_wayback_job_id").startswith("jid-submitted-"),
            "a URL whose back-off has expired must be retried")

    def test_a_successful_capture_clears_the_failure_count(self):
        """Otherwise the exponent is permanent: a URL that failed eight
        times, succeeded, then hit one transient failure would wait ten
        hours rather than five minutes."""
        self._set_meta("_op_wayback_job_id", "jid-recovering-test")
        self._set_meta("_op_wayback_submitted_at", str(int(time.time()) - 100))
        self._set_meta("_op_wayback_error_count", "8")
        self._set_meta("_op_wayback_last_error_at", str(int(time.time()) - 100))

        php = self._common_mocks() + """
        add_filter('onionpress_wayback_poll_parallel_mock', function($_, $job_ids) {
            return array(array(
                'job_id' => 'jid-recovering-test', 'status' => 'success',
                'timestamp' => '20260813000000', 'duration_sec' => 1.0,
            ));
        }, 10, 2);
        add_filter('onionpress_wayback_poll_covered_mock',
                   function($_, $job_ids) { return $job_ids; }, 10, 2);
        onionpress_wayback_sweep_iteration();
        echo 'ok';
        """
        _eval(php, self.url)
        self.assertNotEqual("", self._get_meta("_op_wayback_archived_at"))
        self.assertEqual("", self._get_meta("_op_wayback_error_count"),
            "a success must reset the back-off exponent, not leave it standing")
        self.assertEqual("", self._get_meta("_op_wayback_last_error_at"))

    def test_the_submit_query_over_fetches_past_cooling_posts(self):
        """The back-off is per record and exponential in that record's own
        error_count, so it cannot go in the meta_query — it is applied when
        the batch is assembled. Asking the query for exactly the budget
        would let the newest N posts, all of them cooling, hide every older
        post that is ready, and the queue would stall with work in it."""
        r = _wp(["post", "create", "--post_type=post", "--post_status=publish",
                 "--post_title=wayback-overfetch-probe", "--porcelain"],
                url=self.url, timeout=15)
        second = int(r.stdout.strip())
        self.addCleanup(lambda: _wp(["post", "delete", str(second), "--force"],
                                    url=self.url, timeout=15))
        n = int(_eval("echo count(onionpress_wayback_posts_needing_submit(1));",
                      self.url))
        self.assertGreater(n, 1,
            "a budget of 1 must still consider more than one candidate")

    def test_the_retry_delay_doubles_and_is_capped(self):
        curve = json.loads(_eval("""
        $out = array();
        foreach (array(0,1,2,3,10,100000) as $n) {
            $out[] = onionpress_wayback_retry_delay($n);
        }
        echo json_encode($out);
        """, self.url))
        base, cap = curve[1], curve[-1]
        self.assertEqual(curve[0], base, "never-failed and first-failure agree")
        self.assertEqual(curve[2], base * 2)
        self.assertEqual(curve[3], base * 4)
        self.assertEqual(curve[4], cap, "the curve reaches its ceiling")
        # The exponent is clamped as well as the result. min() on the product
        # alone still evaluates BASE * 2^100000 first, and (int) INF has been
        # 0 on some PHP versions — which would turn the ceiling into no
        # back-off at all, on exactly the record that needs it most.
        self.assertEqual(curve[5], cap, "a runaway count must not overflow to 0")

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


@unittest.skipUnless(_DOCKER, "requires running onionpress-wordpress container")
class TestWaybackPollCoverage(unittest.TestCase):
    """poll_parallel's own response handling, driven through the curl seam.

    Every sweep test above mocks poll_parallel wholesale, which leaves its
    body — the chunking, the chunk/result alignment, the HTTP-200 gate and
    the coverage bookkeeping — with no test at all. That body is where a
    misread response turns into "SPN forgot these 20 jobs", so it is the
    last place that should be untested. The default of the wholesale mock
    also happens to be the optimistic case (everything covered), i.e. the
    opposite of the real function's failure mode, so a test passing there
    says nothing about production.
    """

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def _poll(self, responses_php):
        """Poll 25 ids (j0..j24) => two chunks of 20 and 5, one parallel
        group. $responses_php returns the mocked curl_multi result."""
        php = """
        add_filter('onionpress_wayback_curl_multi_mock', function($_, $setups) {
            %s
        }, 10, 2);
        $ids = array();
        for ($i = 0; $i < 25; $i++) { $ids[] = 'j' . $i; }
        $covered = null;
        $res = onionpress_wayback_poll_parallel($ids, $covered);
        echo json_encode(array(
            'results' => $res,
            'covered' => array_keys($covered),
        ));
        """ % responses_php
        return json.loads(_eval(php, self.url))

    def test_only_the_batch_that_answered_counts_as_covered(self):
        """One chunk answers, the other times out. Coverage must follow the
        chunk boundary exactly: $parallel_group[$i] has to line up with the
        keys curl_multi returns, or the coverage map describes the wrong
        jobs and the forgotten-sweep clears a batch nobody asked about."""
        out = self._poll("""
            return array(
                0 => array('code' => 200,
                           'body' => '[{"job_id":"j3","status":"pending"}]'),
                1 => array('code' => 0, 'body' => ''),   // Tor timeout
            );
        """)
        self.assertEqual(
            ["j%d" % i for i in range(20)], sorted(out["covered"], key=lambda s: int(s[1:])),
            "exactly the 20 ids of the chunk that answered must be covered")
        self.assertNotIn("j20", out["covered"],
            "an id from the chunk that timed out must NOT be marked covered — "
            "that is what makes one 40s timeout resubmit a whole batch")
        self.assertEqual(1, len(out["results"]))
        self.assertEqual("j3", out["results"][0]["job_id"])

    def test_a_200_that_is_not_a_status_list_covers_nothing(self):
        """SPN answers 200 with a JSON *object* — a rate-limit or auth
        envelope, {"message": ...}. That decodes to a PHP array and used to
        pass the is_array() gate, marking all 20 ids in the batch covered on
        a response carrying no statuses whatsoever. The forgotten-sweep then
        reads it as "SPN answered and mentioned none of them" and resubmits
        the lot: the exact over-clearing the coverage tracking exists to
        prevent, reached through a narrower door."""
        out = self._poll("""
            return array(
                0 => array('code' => 200,
                           'body' => '{"message":"You have reached the limit '
                                     . 'of active sessions"}'),
                1 => array('code' => 200, 'body' => '[]'),
            );
        """)
        self.assertNotIn("j0", out["covered"],
            "a 200 carrying an object, not a list of statuses, is not an "
            "answer about any job in the batch")
        # The empty-list chunk IS a real answer: SPN said "none of these
        # five are known to me", which is what the forgotten path acts on.
        self.assertEqual(["j20", "j21", "j22", "j23", "j24"], sorted(out["covered"]),
            "an empty JSON list is a valid answer and must count as covered")
        self.assertEqual([], out["results"])


@unittest.skipUnless(_DOCKER, "requires running onionpress-wordpress container")
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

    def test_daemon_recycles_and_hands_the_lock_back(self):
        """The daemon must not run forever. It used to: OP_WB_LOOP_MAX_SEC
        appeared in two comments but was never defined, so the loop was
        `while (true)` with only a drained-queue exit. WordPress caches
        options per REQUEST and the daemon is one request, so a process
        alive for 70 hours kept reading job_ids that had been deleted from
        the database — and since a non-empty job_id is what marks the queue
        as having work, the stale read sustained the loop that sustained
        the stale read. Five days, nothing archived.

        What matters is the handoff, not just the exit: the lock must be
        released, or the queue stalls for LOCK_STALE_SEC on every recycle.
        """
        # The filter doubles as the sentinel. `echo 'ran'` after the sweep
        # proves nothing — the mutex can reject the invocation and return
        # immediately, and the echo still fires. Counting entries into the
        # loop body is the only way to observe that the daemon actually
        # started, which is exactly what the second half asserts.
        preamble = """
        add_filter('onionpress_wayback_self_reachable_mock',
                   function() { return true; });
        add_filter('onionpress_wayback_user_status_mock',
                   function() { return array('available' => 0); });
        // Cap of 0 => recycle on the very first iteration, before any
        // network-touching work, and (see below) without a real loopback.
        add_filter('onionpress_wayback_loop_max_sec', function() {
            update_option('op_test_loop_entered',
                          1 + (int) get_option('op_test_loop_entered', 0), false);
            return 0;
        });
        // The COUNTER for the assertion below, not the safety net — the
        // suite-wide block installed by _wp()'s --require file is what makes
        // the loopback unreachable, here and in every other test. This one
        // exists because "a zero-lifetime recycle must not fire the loopback"
        // is a claim about the plugin, and it needs something that observes
        // the attempt rather than merely preventing it.
        add_filter('pre_http_request', function($pre, $args, $url) {
            update_option('op_test_http_attempts',
                          1 + (int) get_option('op_test_http_attempts', 0), false);
            return new WP_Error('blocked-by-test', 'no outbound HTTP in tests');
        }, 10, 3);
        """
        php = """
        delete_option('op_test_loop_entered');
        delete_option('op_test_http_attempts');
        """ + preamble + """
        onionpress_wayback_sweep();
        echo json_encode(array(
            'lock'     => (string) get_option('op_wayback_sweep_lock', '(gone)'),
            'entered'  => (int) get_option('op_test_loop_entered', 0),
            'attempts' => (int) get_option('op_test_http_attempts', 0),
        ));
        """
        first = json.loads(_eval(php, self.url))
        self.addCleanup(lambda: _wp(
            ["option", "delete", "op_test_loop_entered"], url=self.url, timeout=15))
        self.addCleanup(lambda: _wp(
            ["option", "delete", "op_test_http_attempts"], url=self.url, timeout=15))

        self.assertEqual(1, first["entered"], "the daemon should have run its loop once")
        self.assertEqual("(gone)", first["lock"],
            "a recycling daemon must delete its lock so the successor can "
            f"claim it immediately rather than waiting it out; got: {first['lock']}")
        # A cap of 0 means every successor recycles on its own first
        # iteration, so firing the handoff there is a restart loop, not a
        # handoff — the production path is gated on a non-zero lifetime.
        self.assertEqual(0, first["attempts"],
            "a zero-lifetime recycle must not fire the loopback")

        # And the successor really can claim it — the property that makes
        # the recycle a handoff instead of a stall. Asserted by the loop
        # entry counter reaching 2, not by the sweep call returning.
        php2 = preamble + """
        onionpress_wayback_sweep();
        echo (int) get_option('op_test_loop_entered', 0);
        """
        self.assertEqual("2", _eval(php2, self.url).strip(),
            "a fresh sweep must be able to start straight after a recycle — "
            "it must reach the loop body, not bounce off a surviving lock")


@unittest.skipUnless(_DOCKER, "requires running onionpress-wordpress container")
class TestWaybackCommentResnapshot(unittest.TestCase):
    """`wp_insert_comment` triggers exactly one re-archive of the parent
    post — and only for imported posts that already have a snapshot.
    Caps the social-importer-threading SPN cost at one extra snapshot
    per parent (instead of one per comment)."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def _make_imported_post(self, archived=True):
        """Insert a publish-state imported post with the wayback metadata
        we'd expect after a successful capture (or empty if archived=False)."""
        archived_at = "2026-04-01 12:00:00" if archived else ""
        snapshot_ts = "20260401120000" if archived else ""
        pid = int(_eval(f"""
        $pid = wp_insert_post(array(
            'post_type'=>'post','post_status'=>'publish',
            'post_title'=>'imported parent','post_content'=>'<p>parent</p>',
            'meta_input'=>array(
                '_source_id'=>'mastodon:wbresnap-{int(time.time()*1000)}',
                '_op_wayback_archived_at'=>'{archived_at}',
                '_op_wayback_snapshot_ts'=>'{snapshot_ts}',
            ),
        ));
        echo (int)$pid;
        """, self.url))
        self.addCleanup(_eval, f"wp_delete_post({pid}, true);", self.url)
        return pid

    def _add_comment(self, post_id):
        cid = int(_eval(f"""
        $cid = wp_insert_comment(array(
            'comment_post_ID'=>{post_id},
            'comment_author'=>'me',
            'comment_content'=>'<p>thread reply</p>',
            'comment_approved'=>1,
        ));
        echo (int)$cid;
        """, self.url))
        return cid

    def _meta(self, post_id, key):
        return _eval(
            f"echo (string) get_post_meta({post_id}, '{key}', true);",
            self.url,
        )

    def test_first_comment_clears_snapshot_and_marks_resnapshot_done(self):
        pid = self._make_imported_post(archived=True)
        self.assertEqual(self._meta(pid, "_op_wayback_archived_at"),
                         "2026-04-01 12:00:00")
        self.assertEqual(self._meta(pid, "_op_wayback_resnapshot_done"), "")
        self._add_comment(pid)
        # Snapshot fields cleared → post will re-enter the queue.
        self.assertEqual(self._meta(pid, "_op_wayback_archived_at"), "")
        self.assertEqual(self._meta(pid, "_op_wayback_snapshot_ts"), "")
        self.assertEqual(self._meta(pid, "_op_wayback_resnapshot_done"), "1")

    def test_second_comment_is_noop(self):
        """Once flagged, further comments don't re-trigger — caps total
        comment-driven re-archives at one per parent."""
        pid = self._make_imported_post(archived=True)
        self._add_comment(pid)
        # Manually re-archive it (simulate the sweep completing).
        _eval(f"""
        update_post_meta({pid}, '_op_wayback_archived_at', '2026-04-02 00:00:00');
        update_post_meta({pid}, '_op_wayback_snapshot_ts', '20260402000000');
        """, self.url)
        # Adding a second comment should NOT clear the new snapshot.
        self._add_comment(pid)
        self.assertEqual(self._meta(pid, "_op_wayback_archived_at"),
                         "2026-04-02 00:00:00")

    def test_unarchived_post_is_skipped(self):
        """A post without a prior snapshot has nothing to invalidate —
        save_post will queue it through the normal path. The hook
        should not flip resnapshot_done in that case."""
        pid = self._make_imported_post(archived=False)
        self._add_comment(pid)
        self.assertEqual(self._meta(pid, "_op_wayback_resnapshot_done"), "")

    def test_original_post_is_skipped(self):
        """Posts without _source_id are 'original' — re-archive is
        already handled by save_post on actual edits, not by this hook."""
        pid = int(_eval(f"""
        $pid = wp_insert_post(array(
            'post_type'=>'post','post_status'=>'publish',
            'post_title'=>'original','post_content'=>'<p>original</p>',
            'meta_input'=>array(
                '_op_wayback_archived_at'=>'2026-04-01 12:00:00',
                '_op_wayback_snapshot_ts'=>'20260401120000',
            ),
        ));
        echo (int)$pid;
        """, self.url))
        self.addCleanup(_eval, f"wp_delete_post({pid}, true);", self.url)
        self._add_comment(pid)
        # No re-archive triggered — original posts go through save_post.
        self.assertEqual(self._meta(pid, "_op_wayback_archived_at"),
                         "2026-04-01 12:00:00")
        self.assertEqual(self._meta(pid, "_op_wayback_resnapshot_done"), "")


@unittest.skipUnless(_DOCKER, "requires running onionpress-wordpress container")
class TestWaybackKickAndInvalidate(SitewideStateMixin, unittest.TestCase):
    """`onionpress_wayback_kick_now()` / `_invalidate_sitewide()` are the
    shared mechanism behind every "archive right now" trigger: the
    save_post hook, the admin "kick" button, and (via the moss receiver)
    a moss publish. Covers the mechanism itself, not each caller."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def setUp(self):
        self.snapshot_sitewide()
        _eval("delete_option('op_wayback_home_state'); "
              "delete_option('op_wayback_feed_state'); "
              "update_option('op_wayback_backoff_until', time() + 999, false); "
              "wp_clear_scheduled_hook('onionpress_wayback_sweep');",
              self.url)

    def test_kick_now_clears_backoff_and_schedules_sweep(self):
        _eval("onionpress_wayback_kick_now();", self.url)
        backoff = _eval("echo (string) get_option('op_wayback_backoff_until', '');",
                         self.url)
        self.assertEqual(backoff, "", "backoff option should be deleted")
        scheduled = _eval(
            "echo wp_next_scheduled('onionpress_wayback_sweep') ? '1' : '0';",
            self.url)
        self.assertEqual(scheduled, "1", "sweep should be scheduled for immediate run")

    def test_invalidate_sitewide_clears_home_and_feed_when_idle(self):
        _eval("update_option('op_wayback_home_state', array('archived_at'=>'x'), false); "
              "update_option('op_wayback_feed_state', array('archived_at'=>'x'), false);",
              self.url)
        _eval("onionpress_wayback_invalidate_sitewide();", self.url)
        home = _eval("echo (string) get_option('op_wayback_home_state', '');", self.url)
        feed = _eval("echo (string) get_option('op_wayback_feed_state', '');", self.url)
        self.assertEqual(home, "")
        self.assertEqual(feed, "")

    def test_invalidate_sitewide_skips_home_with_job_in_flight(self):
        """A capture already submitted must not be wiped — the in-flight
        SPN job will render the current content anyway; clearing the
        option here would just burn a duplicate submission on the next
        sweep. Mirrors the reasoning in save_post's own comment."""
        _eval("update_option('op_wayback_home_state', "
              "array('job_id'=>'abc123'), false);", self.url)
        _eval("onionpress_wayback_invalidate_sitewide();", self.url)
        home = _eval("echo (string) get_option('op_wayback_home_state', '');", self.url)
        self.assertNotEqual(home, "", "home state with a job in flight must survive")


@unittest.skipUnless(_DOCKER, "requires running onionpress-wordpress container")
class TestMossPageEnumeration(unittest.TestCase):
    """Which pages of a moss generation the archiver can see.

    Pure enumeration, driven against fixture directories in the container's
    /tmp rather than the live generation, so these say nothing about the
    site's own content and cannot disturb it.
    """

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    # The real thing, byte for byte, minus the <loc> list. A bare <urlset>
    # was the fixture for a while and it tested a shape production never
    # emits: moss writes the sitemaps.org default namespace, under which
    # $xml->url resolves only because SimpleXML treats an unprefixed default
    # namespace as the document's own. The code was right and the fixture
    # could not have caught it becoming wrong.
    _SITEMAP_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>'
                     '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')

    def _fixture(self, name, sitemap=None, extra=(), unreadable=()):
        """A two-page generation: '/' and '/about/', plus whatever `extra`
        asks for. `sitemap` is None, 'urlset' or 'index'; `unreadable` names
        directories to create with mode 0000. Returns the directory path."""
        d = "/tmp/wb-moss-" + name
        # A deliberately WRONG host in the sitemap: moss persists whatever
        # site_url was configured at build time, and archiving that host
        # would submit URLs nobody can fetch. Only the path may be taken.
        bodies = {
            None: "",
            "urlset": (self._SITEMAP_HEAD
                       + "<url><loc>http://localhost:8080/</loc></url>"
                       + "<url><loc>http://localhost:8080/about/</loc></url>"
                       + "</urlset>"),
            # What moss emits past 50k URLs: an index OF sitemaps, whose
            # children are <sitemap>, not <url>.
            "index": ('<?xml version="1.0" encoding="UTF-8"?>'
                      '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                      "<sitemap><loc>http://localhost:8080/sitemap-1.xml</loc></sitemap>"
                      "</sitemapindex>"),
        }
        php = """
        $d = 'DIR';
        exec('rm -rf ' . escapeshellarg($d));
        mkdir($d . '/about', 0755, true);
        file_put_contents($d . '/index.html', 'root');
        file_put_contents($d . '/about/index.html', 'about');
        foreach (EXTRA as $rel) {
            @mkdir(dirname($d . $rel), 0755, true);
            file_put_contents($d . $rel, 'x');
        }
        $sitemap = base64_decode('SITEMAP');
        if ($sitemap !== '') { file_put_contents($d . '/sitemap.xml', $sitemap); }
        foreach (UNREADABLE as $rel) {
            @mkdir($d . $rel, 0755, true);
            file_put_contents($d . $rel . '/index.html', 'hidden');
            chmod($d . $rel, 0000);
        }
        echo 'built';
        """
        php = (php.replace("DIR", d)
                  .replace("EXTRA", "array(" + ",".join(
                      "'%s'" % e for e in extra) + ")")
                  .replace("UNREADABLE", "array(" + ",".join(
                      "'%s'" % u for u in unreadable) + ")")
                  .replace("SITEMAP", base64.b64encode(
                      bodies[sitemap].encode()).decode()))
        self.assertEqual(_eval(php, self.url), "built")
        # chmod 0000 dirs need root to remove.
        self.addCleanup(_docker_exec, ["rm", "-rf", d])
        return d

    def _paths(self, directory, user=None):
        php = ("$p = onionpress_wayback_moss_paths(array('id'=>'fx','dir'=>'%s'));"
               " sort($p); echo json_encode($p);" % directory)
        return json.loads(_eval(php, self.url, user=user))

    def test_sitemap_supplies_the_paths_and_never_the_host(self):
        d = self._fixture("sitemap", sitemap="urlset")
        self.assertEqual(self._paths(d), ["/", "/about/"])

    def test_the_walk_does_not_descend_the_asset_mount(self):
        """The fallback walk, which moss sites reach whenever sitemap.xml is
        absent — moss gates it on the site_url being deployed, so a site
        published before its onion name was registered ships none.

        _moss/ holds hashed css/js and generated OG images — on a real site
        an order of magnitude more files than pages, and never a page."""
        d = self._fixture("assets", sitemap=None,
                          extra=("/_moss/og/index.html",))
        self.assertEqual(self._paths(d), ["/", "/about/"])

    def test_a_sitemap_index_falls_back_to_the_walk(self):
        """Past ~50k URLs moss emits a <sitemapindex>, whose children are
        <sitemap> and not <url>, so isset($xml->url) is false. We deliberately
        do not follow it — such a site is two orders of magnitude past
        OP_WB_MOSS_MAX_PAGES, so the extra pages are ones this plugin refuses
        to track anyway — and fall through to the walk, which is a documented
        decision rather than an accident. This pins that: an index must
        enumerate, not return nothing."""
        d = self._fixture("index", sitemap="index")
        self.assertEqual(self._paths(d), ["/", "/about/"])

    def test_an_unreadable_directory_does_not_kill_the_sweep(self):
        """RecursiveDirectoryIterator raises UnexpectedValueException on a
        directory it cannot open, and nothing caught it. Reproduced in this
        container as www-data ("Failed to open directory: Permission denied"):
        the throw propagated out of sitewide_has_work(), out of
        sweep_iteration(), and killed the sweep as a PHP fatal on every cron
        fire until the permission was fixed — with no plugin log line saying
        so.

        Runs as www-data on purpose. The rest of this suite runs as root,
        which can read a 0000 directory, so the bug is invisible from there:
        it is the WEB user that the daemon actually runs as.
        """
        d = self._fixture("perm", sitemap=None, unreadable=("/secret",))
        self.assertEqual(self._paths(d, user="www-data"), ["/", "/about/"],
            "an unreadable subdirectory must cost its own subtree and "
            "nothing else")

    def test_the_page_list_is_memoized_per_generation(self):
        """One sweep iteration asks for the page list three times per subsite
        — the has-work probe, the poll's records and the submit's — so a
        four-blog network ran twelve full enumerations every 30 seconds. On
        the fallback walk that is up to OP_WB_MOSS_SCAN_MAX stats each.

        The memo is keyed on the generation, which is what keeps it honest: a
        publish is a new id, so it cannot serve yesterday's pages. Asserted by
        mutating the tree between two calls in ONE request — the second call
        must not see the change, and a call for a different generation must.
        """
        d = self._fixture("memo", sitemap=None)
        out = json.loads(_eval("""
        $g = array('id' => 'memo-gen', 'dir' => '%s');
        $first = onionpress_wayback_moss_paths($g);
        unlink('%s/about/index.html');
        $again = onionpress_wayback_moss_paths($g);
        $fresh = onionpress_wayback_moss_paths(
            array('id' => 'memo-gen-next', 'dir' => '%s'));
        sort($first); sort($again); sort($fresh);
        echo json_encode(compact('first', 'again', 'fresh'));
        """ % (d, d, d), self.url))
        self.assertEqual(["/", "/about/"], out["first"])
        self.assertEqual(out["first"], out["again"],
            "the same generation must be enumerated once per request")
        self.assertEqual(["/"], out["fresh"],
            "a new generation id must re-enumerate — otherwise a publish "
            "would be invisible to the archiver for the daemon's whole life")

    def test_the_generation_id_comes_from_the_target_not_the_link(self):
        """This is what realpath() buys, and it is the whole re-archive
        mechanism. `site/current` is a symlink whose own basename is the
        constant "current" — read the id off the unresolved path and it
        never changes, so every publish matches the generation already
        recorded, the per-page map never resets, and new content is
        silently never submitted."""
        out = json.loads(_eval("""
        $g = onionpress_wayback_moss_generation();
        echo json_encode($g === null ? null : array(
            'dir'     => $g['dir'],
            'is_link' => is_link($g['dir']),
            'id'      => $g['id'],
            'link'    => basename(onionpress_wayback_moss_current_path()),
            'pages'   => count(onionpress_wayback_moss_paths($g)),
        ));
        """, self.url))
        if out is None:
            self.skipTest("no moss generation is serving this site")
        self.assertNotEqual(out["id"], out["link"],
                            "the generation id must not be the symlink's own name")
        self.assertFalse(out["is_link"])
        self.assertEqual(out["id"], out["dir"].rsplit("/", 1)[-1])
        self.assertGreater(out["pages"], 0,
                           "a live generation that enumerates zero pages is the bug")


@unittest.skipUnless(_DOCKER, "requires running onionpress-wordpress container")
class TestMossPagesReachTheQueue(SitewideStateMixin, unittest.TestCase):
    """The pages a moss generation publishes have to be work items, not
    just files on disk.

    The failure: this site reported archived=5/5 while archiving none of
    its own content. The five were leftover WordPress default posts; the
    site a reader sees is a moss generation served at the onion root, and
    none of its pages had ever been submitted. The queue was not failing —
    the pages were never in it.

    Pinned to the MAIN site, because that is whose URLs the generation is.
    It is served at the onion root from one container-global symlink, while
    sitewide_records() runs once per subsite inside the sweep's network loop
    and OP_WB_OPT_MOSS is a per-subsite option — so enumerating it everywhere
    gave every subsite its own map, its own job_ids, and its own submission of
    the same page.
    """

    @classmethod
    def setUpClass(cls):
        s = _pick_main_site()
        if s is None:
            raise unittest.SkipTest("no main site available")
        cls.url = s["url"].rstrip("/") + "/"
        sub = _pick_subsite()
        cls.sub_url = sub["url"].rstrip("/") + "/" if sub else None
        if _eval("echo onionpress_wayback_moss_is_owner() ? '1' : '0';",
                 cls.url) != "1":
            raise unittest.SkipTest("no moss generation is serving this site")

    def setUp(self):
        self.snapshot_sitewide()

    def _records(self, url=None):
        return json.loads(_eval("""
        $out = array();
        foreach (onionpress_wayback_sitewide_records() as $r) {
            $out[] = array('key' => $r['key'], 'url' => $r['url']);
        }
        echo json_encode($out);
        """, url or self.url))

    def test_moss_pages_are_queued_alongside_home_and_feed(self):
        recs = self._records()
        keys = [r["key"] for r in recs]
        self.assertTrue(any(k.startswith("moss:") for k in keys),
                        "no moss page in the queue: " + ", ".join(keys))
        self.assertIn("opt:op_wayback_home_state", keys)

    def test_the_site_root_is_never_queued_twice(self):
        """moss's sitemap lists '/' and so does the home record. SPN's
        concurrent slots are counted in single digits; spending two on the
        same URL every sweep is not a rounding error."""
        urls = [r["url"] for r in self._records()]
        self.assertEqual(len(urls), len(set(urls)),
                         "duplicate URL in the queue: " + ", ".join(sorted(urls)))

    def test_a_subsite_contributes_no_moss_pages_and_keeps_its_own_feed(self):
        """The moss generation is container-global; the option holding its
        per-page state is not. Every subsite therefore enumerated the same
        pages and submitted them under its own job_ids: measured on this
        four-blog network, 12 SPN submissions per drain cycle for 3 distinct
        URLs, plus four of /rss.xml — against an account whose concurrent
        slots are in single digits, which is exactly the cost the per-URL
        back-off exists to keep off other people's sites.

        The feed is the same bug with a sharper edge: gating on
        moss_is_owner(), a global answer, silently replaced subsite 2's own
        /op-bluesky-test/feed/ with the MAIN site's /rss.xml, so the subsite
        archived a feed that was not its own and never archived the one that
        was.
        """
        if self.sub_url is None:
            self.skipTest("network has no subsite")
        keys = [r["key"] for r in self._records(self.sub_url)]
        self.assertFalse([k for k in keys if k.startswith("moss:")],
            "a subsite must contribute no moss records: " + ", ".join(keys))
        feed = _eval("echo onionpress_wayback_feed_url_full();", self.sub_url)
        path = self.sub_url.rstrip("/").rsplit("/", 1)[-1]
        self.assertTrue(feed.endswith("/%s/feed/" % path),
            "a subsite must keep its own WordPress feed, not the main site's "
            "moss rss.xml; got " + feed)

    def test_the_feed_queued_is_the_one_readers_subscribe_to(self):
        """/feed/ is a WordPress route. A moss generation publishes at
        /rss.xml and emits nothing at /feed/ at all, so the archiver was
        submitting WordPress's own empty feed sweep after sweep."""
        has_rss = _eval("""
        $g = onionpress_wayback_moss_generation();
        echo ($g !== null && is_file($g['dir'] . '/rss.xml')) ? '1' : '0';
        """, self.url)
        feed = _eval("echo onionpress_wayback_feed_url_full();", self.url)
        if has_rss == "1":
            self.assertTrue(feed.endswith("/rss.xml"), feed)
        else:
            # Decided by the file being there, not by "is a generation live":
            # a site built before its onion name was registered has no
            # rss.xml, and WordPress's route is the only feed that exists.
            self.assertTrue(feed.endswith("/feed/"), feed)

    def test_a_publish_puts_every_page_back_in_the_queue(self):
        """A new generation id is what makes new content get archived. The map
        is keyed to the generation it describes, so the moment a publish
        replaces it every page reads as unarchived — and the first write for
        the new generation drops the retired rows in one go.

        Asserted through the records, and with a purity check, because ASKING
        must not be what performs the reset. sitewide_has_work() is a
        predicate the multisite loop calls to decide it may skip a subsite
        entirely; it used to rewrite this option and log a publish every time
        it was called, three times per subsite per iteration.
        """
        live = _eval("$g = onionpress_wayback_moss_generation(); echo $g['id'];",
                     self.url)
        out = json.loads(_eval("""
        $rec = null;
        foreach (onionpress_wayback_sitewide_records() as $r) {
            if (strpos($r['key'], 'moss:') === 0) { $rec = $r; break; }
        }
        if ($rec === null) { echo json_encode(array('err' => 'no-moss-record')); return; }
        $path = substr($rec['key'], strlen('moss:'));

        // A map left behind by an earlier generation, claiming this very page
        // is already archived, plus a page that generation served and the
        // live one does not.
        update_option('op_wayback_moss_state', array(
            'generation' => 'moss-from-a-previous-publish',
            'urls'       => array(
                $path      => array('archived_at' => 1),
                '/retired/'=> array('archived_at' => 1),
            ),
        ), false);

        $before = get_option('op_wayback_moss_state');
        $state  = call_user_func($rec['read']);
        $work   = onionpress_wayback_sitewide_has_work();
        $after  = get_option('op_wayback_moss_state');

        // The first real write is what adopts the live generation.
        call_user_func($rec['write'], array('job_id' => 'jid-adopt-test'));
        $s = get_option('op_wayback_moss_state', array());
        echo json_encode(array(
            'path'          => $path,
            'stale_read'    => $state,
            'has_work'      => $work,
            'reads_pure'    => ($before === $after),
            'gen'           => $s['generation'],
            'rows'          => array_keys($s['urls']),
        ));
        """, self.url))
        self.assertNotIn("err", out, "the live generation enumerated no pages")
        self.assertEqual([], out["stale_read"],
            "a row written for a previous generation must not be reported as "
            "this page's state — that row is what makes the archiver think "
            "freshly published content is already captured")
        self.assertTrue(out["has_work"],
            "every page of a new generation is unarchived, so the site must "
            "not be skipped by the multisite loop")
        self.assertTrue(out["reads_pure"],
            "building records and asking whether work exists must not write "
            "the option they read")
        self.assertEqual(live, out["gen"],
            "the first write must adopt the live generation")
        self.assertEqual([out["path"]], out["rows"],
            "adopting must retire every row of the previous generation, "
            "including pages it served that this one does not")

    def test_a_write_from_a_retired_generation_is_refused(self):
        """finalize_success writes archived_at and clears job_id in two
        calls, so a job submitted against the previous generation can land
        after a publish. Without the guard it resurrects a row for a page the
        new generation may not serve, and nothing ever retires it.

        The RETURN value is load-bearing, not just the state. The submit step
        counts a submission only when its write landed: a refusal reporting
        success is how the sweep came to log submitted=N for job_ids it had
        thrown away, each of which still runs at SPN, still holds one of the
        account's few concurrent slots, and is never polled by anyone.
        """
        out = json.loads(_eval("""
        $live = onionpress_wayback_moss_generation();
        $refused = onionpress_wayback_moss_write('moss-a-generation-that-is-gone',
                                                 '/anything/', array('job_id' => 'late'));
        $s1 = get_option('op_wayback_moss_state', array());
        $ok = onionpress_wayback_moss_write($live['id'], '/anything/',
                                            array('job_id' => 'current'));
        $s2 = get_option('op_wayback_moss_state', array());
        echo json_encode(array(
            'refused'      => $refused,
            'retired_row'  => isset($s1['urls']['/anything/']),
            'accepted'     => $ok,
            'live_row'     => $s2['urls']['/anything/']['job_id'] ?? '',
        ));
        """, self.url))
        self.assertIs(False, out["refused"],
            "a write into a retired generation must report failure, or the "
            "sweep counts a submission whose job_id it just discarded")
        self.assertFalse(out["retired_row"],
            "the refused write must not resurrect a row")
        self.assertIs(True, out["accepted"],
            "a write for the live generation must report success")
        self.assertEqual("current", out["live_row"])

    def test_an_unarchived_moss_page_keeps_the_site_in_the_sweep(self):
        """The multisite loop skips a site it believes has no work. It used to
        decide that from the home and feed options alone, so a site whose
        posts were archived could be skipped while its moss pages sat
        unarchived forever — the same bug one layer up."""
        state = _eval("""
        // Everything archived and nothing in flight: the loop is entitled to
        // skip. job_id has to be cleared explicitly, not assumed absent —
        // this runs against the live site, where the real daemon may have a
        // capture outstanding on the home page at any moment, and a record
        // with a job_id counts as work no matter what archived_at says.
        foreach (onionpress_wayback_sitewide_records() as $r) {
            call_user_func($r['write'], array('archived_at' => time(), 'job_id' => ''));
        }
        $idle = onionpress_wayback_sitewide_has_work() ? 'work' : 'idle';

        // Now un-archive one moss page and nothing else.
        $moss = null;
        foreach (onionpress_wayback_sitewide_records() as $r) {
            if (strpos($r['key'], 'moss:') === 0) { $moss = $r; break; }
        }
        if ($moss === null) { echo 'no-moss-record'; return; }
        call_user_func($moss['write'], array('archived_at' => ''));
        echo $idle . ',' . (onionpress_wayback_sitewide_has_work() ? 'work' : 'idle');
        """, self.url)
        self.assertEqual(state, "idle,work")


if __name__ == "__main__":
    unittest.main()
