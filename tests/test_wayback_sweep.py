#!/usr/bin/env python3
"""Integration tests for the Wayback sweeper plugin.

These tests drive the live plugin inside the onionpress-wordpress container
via `wp eval` and inspect side effects on post meta. They exercise the
state machine directly by seeding/reading post meta — no real SPN calls.

Prerequisites (skips the suite if any fails):
  - Docker running
  - `onionpress-wordpress` container up
  - A subsite with path != '/'
"""

import json
import os
import shutil
import subprocess
import sys
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


def _pick_test_site():
    r = _wp(["site", "list", "--fields=blog_id,path,url", "--format=json"],
            timeout=15)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    sites = json.loads(r.stdout)
    subsites = [s for s in sites if s.get("path") != "/"]
    return subsites[0] if subsites else (sites[0] if sites else None)


def _get_post_state(post_id, site_url):
    """Read all _op_wayback_* meta into a dict."""
    keys = {
        "archived_at":   "_op_wayback_archived_at",
        "snapshot_ts":   "_op_wayback_snapshot_ts",
        "job_id":        "_op_wayback_job_id",
        "retry_count":   "_op_wayback_retry_count",
        "retry_after":   "_op_wayback_retry_after",
        "failed_at":     "_op_wayback_failed_at",
        "failed_reason": "_op_wayback_failed_reason",
    }
    state = {}
    for label, meta_key in keys.items():
        r = _wp(["post", "meta", "get", str(post_id), meta_key, "--format=json"],
                url=site_url, timeout=15)
        state[label] = r.stdout.strip().strip('"') if r.returncode == 0 else ""
    return state


def _set_post_state(post_id, site_url, **kwargs):
    """Seed _op_wayback_* meta from keyword args. Missing keys are cleared."""
    mapping = {
        "archived_at":   "_op_wayback_archived_at",
        "snapshot_ts":   "_op_wayback_snapshot_ts",
        "job_id":        "_op_wayback_job_id",
        "retry_count":   "_op_wayback_retry_count",
        "retry_after":   "_op_wayback_retry_after",
        "failed_at":     "_op_wayback_failed_at",
        "failed_reason": "_op_wayback_failed_reason",
    }
    for label, meta_key in mapping.items():
        if label in kwargs and kwargs[label] not in (None, "", 0):
            _wp(["post", "meta", "update", str(post_id), meta_key, str(kwargs[label])],
                url=site_url, timeout=15)
        else:
            _wp(["post", "meta", "delete", str(post_id), meta_key],
                url=site_url, timeout=15)


def _eval_php(code, site_url):
    """Run PHP code inside WP, return stdout."""
    r = _wp(["eval", code], url=site_url, timeout=60)
    return r.stdout.strip()


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestWaybackStateMachine(unittest.TestCase):
    """Test the advance() state machine by calling it with mocked SPN results.

    The plugin's advance() function takes read/write callables for state,
    so we inject Python-controlled state and verify the PHP advance()
    mutates it correctly based on a mocked SPN response.

    We do this by monkeypatching the SPN calls via WP runtime filters:
    neither onionpress_wayback_submit nor onionpress_wayback_poll_status
    are filterable directly, so we test via post meta (which IS real) and
    compare pre/post states through advance().

    For a pure-logic test we instead call advance() with inline-
    constructed read/write closures and a pre-baked state dict, bypassing
    the WP post meta layer entirely.
    """

    @classmethod
    def setUpClass(cls):
        cls.site = _pick_test_site()
        if cls.site is None:
            raise unittest.SkipTest("no test site available")
        cls.site_url = cls.site["url"].rstrip("/") + "/"

    def _make_post(self, title_suffix=""):
        r = _wp(["post", "create",
                 "--post_title=wayback-test " + title_suffix,
                 "--post_content=test",
                 "--post_status=publish",
                 "--porcelain"],
                url=self.site_url, timeout=30)
        self.assertEqual(r.returncode, 0, f"post create failed: {r.stderr}")
        return r.stdout.strip()

    def _delete_post(self, post_id):
        _wp(["post", "delete", str(post_id), "--force"],
            url=self.site_url, timeout=30)

    def test_state_is_empty_after_publish(self):
        """A fresh post has no _op_wayback_* meta yet."""
        post_id = self._make_post("empty-state")
        try:
            state = _get_post_state(post_id, self.site_url)
            self.assertEqual(state["archived_at"], "")
            self.assertEqual(state["job_id"], "")
            self.assertEqual(state["failed_at"], "")
        finally:
            self._delete_post(post_id)

    def test_advance_transitions_archived_to_skip(self):
        """A post with archived_at set is a no-op — advance() reports 'already-archived'."""
        post_id = self._make_post("archived-skip")
        try:
            _set_post_state(post_id, self.site_url,
                            archived_at=1700000000,
                            snapshot_ts="20231114000000")
            code = (
                f"$state = onionpress_wayback_post_state({post_id});"
                f"$read = function() use ($state) {{ return $state; }};"
                f"$writes = array();"
                f"$write = function($s) use (&$writes) {{ $writes[] = $s; }};"
                f"$action = onionpress_wayback_advance('http://example/', $read, $write);"
                f"echo $action . '|' . count($writes);"
            )
            out = _eval_php(code, self.site_url)
            self.assertEqual(out, "already-archived|0",
                f"expected no writes on archived post, got: {out}")
        finally:
            self._delete_post(post_id)

    def test_advance_transitions_failed_to_skip(self):
        """A post with failed_at set is also a no-op."""
        post_id = self._make_post("failed-skip")
        try:
            _set_post_state(post_id, self.site_url,
                            failed_at=1700000000,
                            failed_reason="error:no-captures: unreachable",
                            retry_count=20)
            code = (
                f"$state = onionpress_wayback_post_state({post_id});"
                f"$read = function() use ($state) {{ return $state; }};"
                f"$write = function($s) {{ /* no-op */ }};"
                f"echo onionpress_wayback_advance('http://example/', $read, $write);"
            )
            out = _eval_php(code, self.site_url)
            self.assertEqual(out, "given-up")
        finally:
            self._delete_post(post_id)

    def test_advance_respects_retry_after(self):
        """If retry_after is in the future, advance() waits."""
        post_id = self._make_post("retry-wait")
        try:
            code = (
                f"$state = array('retry_after' => time() + 600);"
                f"$read = function() use ($state) {{ return $state; }};"
                f"$write = function($s) {{ /* no-op */ }};"
                f"echo onionpress_wayback_advance('http://example/', $read, $write);"
            )
            out = _eval_php(code, self.site_url)
            self.assertEqual(out, "waiting")
        finally:
            self._delete_post(post_id)

    def test_post_state_round_trip(self):
        """post_state_set + post_state returns what we wrote (with 0/''
        entries cleared — those delete_post_meta calls shouldn't trip us)."""
        post_id = self._make_post("round-trip")
        try:
            code = (
                f"$in = array("
                f"  'job_id' => 'spn2-abc123',"
                f"  'retry_count' => 2,"
                f"  'retry_after' => 1234567890,"
                f");"
                f"onionpress_wayback_post_state_set({post_id}, $in);"
                f"$out = onionpress_wayback_post_state({post_id});"
                f"echo $out['job_id'] . '|' . $out['retry_count'] . '|' . $out['retry_after'];"
            )
            out = _eval_php(code, self.site_url)
            self.assertEqual(out, "spn2-abc123|2|1234567890")
        finally:
            self._delete_post(post_id)

    def test_save_post_invalidates_home_and_feed(self):
        """Publishing a post should clear the home+feed option state so
        the next sweep tick re-archives them (content has changed)."""
        # Seed home + feed as already archived.
        for opt in ("op_wayback_home_state", "op_wayback_feed_state"):
            code = (
                f"update_option('{opt}', array("
                f"  'archived_at' => 1700000000,"
                f"  'snapshot_ts' => '20231114000000',"
                f"), false);"
            )
            _eval_php(code, self.site_url)

        # Confirm seeded.
        pre = _eval_php(
            "echo (string)(get_option('op_wayback_home_state')['archived_at'] ?? 0);",
            self.site_url,
        )
        self.assertEqual(pre, "1700000000", "seed didn't stick")

        # Publish a post — should fire save_post → clear home+feed.
        post_id = self._make_post("invalidate-home-feed")
        try:
            home_after = _eval_php(
                "$o = get_option('op_wayback_home_state', null);"
                "echo is_array($o) && !empty($o['archived_at']) ? 'set' : 'cleared';",
                self.site_url,
            )
            feed_after = _eval_php(
                "$o = get_option('op_wayback_feed_state', null);"
                "echo is_array($o) && !empty($o['archived_at']) ? 'set' : 'cleared';",
                self.site_url,
            )
            self.assertEqual(home_after, "cleared",
                "save_post should have cleared op_wayback_home_state")
            self.assertEqual(feed_after, "cleared",
                "save_post should have cleared op_wayback_feed_state")
        finally:
            self._delete_post(post_id)
            # Clean up the seeded option so later tests start fresh.
            _eval_php("delete_option('op_wayback_home_state');"
                     "delete_option('op_wayback_feed_state');", self.site_url)

    def test_sweep_query_picks_up_unarchived(self):
        """The sweep's meta_query returns published posts without
        archived_at/failed_at set. Seed two posts — one archived, one not —
        and verify only the un-archived one shows up."""
        archived_id = self._make_post("sweep-archived")
        unarchived_id = self._make_post("sweep-unarchived")
        try:
            _set_post_state(archived_id, self.site_url,
                            archived_at=1700000000,
                            snapshot_ts="20231114000000")
            code = (
                "$posts = get_posts(array("
                "  'post_status' => 'publish',"
                "  'post_type' => array('post', 'page'),"
                "  'numberposts' => 100,"
                "  'meta_query' => array('relation' => 'AND',"
                "    array('key' => OP_WB_META_ARCHIVED_AT, 'compare' => 'NOT EXISTS'),"
                "    array('key' => OP_WB_META_FAILED_AT,   'compare' => 'NOT EXISTS'),"
                "  ),"
                "));"
                "$ids = array_map(function($p){return $p->ID;}, $posts);"
                "echo implode(',', $ids);"
            )
            out = _eval_php(code, self.site_url)
            ids = out.split(",") if out else []
            self.assertIn(str(unarchived_id), ids,
                f"unarchived post {unarchived_id} should be in sweep set, got: {ids}")
            self.assertNotIn(str(archived_id), ids,
                f"archived post {archived_id} should NOT be in sweep set, got: {ids}")
        finally:
            self._delete_post(archived_id)
            self._delete_post(unarchived_id)


if __name__ == "__main__":
    unittest.main()
