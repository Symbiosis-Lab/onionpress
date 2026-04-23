#!/usr/bin/env python3
"""Integration tests for the Mastodon importer plugin.

These drive the live plugin inside the onionpress-wordpress container
via `wp eval`, using the `onionpress_mastodon_fetch_statuses_mock`
filter hook to inject canned API responses — no real Mastodon server
is contacted.

Coverage focus: regressions we've hit in the field.
  1. Short page (count < PER_PAGE) MUST NOT mark backfill done — the
     bug that stopped a user's backfill at 647/1417.
  2. Truly empty page DOES mark backfill done.
  3. Cycle guard: max_id cursor not advancing for 3 rounds → error,
     no infinite loop.
  4. Dedupe: importing the same source_id twice creates one post.
  5. Reply/boost filters: include_replies/include_boosts correctly
     skip items when disabled.
  6. Token-lock mutex: fresh lock blocks a second daemon; stale lock
     lets a new daemon take over.

Prerequisites (skips the suite if any fails):
  - Docker running
  - `onionpress-wordpress` container up
  - At least one subsite to target
"""

import json
import shutil
import subprocess
import unittest
import uuid

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


_SAFE_TEST_ACCOUNT_ID = "1"
_SAFE_TEST_SERVER     = "example.test"


def _assert_test_sandbox(url):
    """Refuse to run if the Mastodon server or account_id option points
    at something other than the sandbox fixtures. Prevents tests from
    clobbering a real account's cursors when accidentally pointed at a
    production subsite — exact mechanism that burned the Bluesky side."""
    r = _wp(["option", "get", "onionpress_social_mastodon_server"],
            url=url, timeout=10)
    server = (r.stdout or "").strip()
    if server and server != _SAFE_TEST_SERVER:
        raise RuntimeError(
            f"Refusing to run Mastodon tests against {url!r}: "
            f"onionpress_social_mastodon_server is {server!r} (real server). "
            f"Clear it first: wp option delete onionpress_social_mastodon_server --url={url}"
        )
    r = _wp(["option", "get", "onionpress_social_mastodon_account_id"],
            url=url, timeout=10)
    acct = (r.stdout or "").strip()
    if acct and acct != _SAFE_TEST_ACCOUNT_ID:
        raise RuntimeError(
            f"Refusing to run Mastodon tests against {url!r}: "
            f"onionpress_social_mastodon_account_id is {acct!r} (real account). "
            f"Clear it first: wp option delete onionpress_social_mastodon_account_id --url={url}"
        )


def _cleanup_test_posts(url):
    """Delete any posts whose content is '<p>hi</p>' (our canned test
    status content) AND which have a _source_id. Tests that import
    statuses create real WP posts; without this they'd accumulate
    forever on the live site."""
    _eval("""
    global $wpdb;
    $ids = $wpdb->get_col(
      "SELECT p.ID FROM {$wpdb->posts} p
       JOIN {$wpdb->postmeta} m ON m.post_id=p.ID AND m.meta_key='_source_id'
       WHERE p.post_content='<p>hi</p>' AND p.post_status='publish'"
    );
    foreach ($ids as $id) { wp_delete_post((int)$id, true); }
    echo count($ids);
    """, url)


def _status(id_str, created="2026-04-23T00:00:00.000Z",
            in_reply_to_id=None, reblog=None, content="<p>hi</p>"):
    """Build a fake Mastodon status object, shape matching the API."""
    return {
        "id": id_str,
        "created_at": created,
        "in_reply_to_id": in_reply_to_id,
        "in_reply_to_account_id": None,
        "reblog": reblog,
        "content": content,
        "account": {"id": "1", "display_name": "test", "username": "test",
                    "url": "https://example.test/@test", "avatar_static": ""},
        "media_attachments": [],
        "tags": [],
    }


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestMastodonImporterBackfill(unittest.TestCase):
    """Backfill pagination semantics."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def setUp(self):
        _assert_test_sandbox(self.url)
        # Fresh cursors + minimal config so the sync has a valid target.
        _wp(["option", "update", "onionpress_social_mastodon_server",
             "example.test"], url=self.url, timeout=15)
        _wp(["option", "update", "onionpress_social_mastodon_account_id",
             "1"], url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_mastodon_oldest_id"],
            url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_mastodon_newest_id"],
            url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_mastodon_daemon_lock"],
            url=self.url, timeout=15)
        _wp(["transient", "delete", "onionpress_social_mastodon_lock"],
            url=self.url, timeout=15)
        # sync_one_tick imports whatever the mock returns into real
        # WP posts — clean those up after every test.
        self.addCleanup(_cleanup_test_posts, self.url)

    def _register_mock(self, pages_json):
        """Install a PHP filter that returns pre-canned pages in order,
        then empties. Pages are drained one per call. We stash the
        pages JSON into a WP option so the filter closure can
        json_decode it at fire time — avoids PHP/JSON brace collisions
        in string interpolation."""
        pages_escaped = pages_json.replace("'", "\\'")
        return f"""
        delete_option('op_test_mastodon_mock_idx');
        update_option('op_test_mastodon_mock_pages', '{pages_escaped}', false);
        add_filter('onionpress_mastodon_fetch_statuses_mock', function($_, $params) {{
            $pages = json_decode((string) get_option('op_test_mastodon_mock_pages', '[]'), true);
            $idx = (int) get_option('op_test_mastodon_mock_idx', 0);
            update_option('op_test_mastodon_mock_idx', $idx + 1, false);
            return is_array($pages) && isset($pages[$idx]) ? $pages[$idx] : array();
        }}, 10, 2);
        """

    def test_short_page_does_not_mark_done(self):
        """A page with count < PER_PAGE (40) must NOT mark backfill done."""
        # 20-entry page (short), then empty. After processing both,
        # oldest_id should be 'done' only because of the empty page —
        # and the 20 items from the short page must have been iterated.
        page = [_status(str(1000 - i)) for i in range(20)]
        mock = json.dumps([page, []])
        php = self._register_mock(mock) + """
        $r = onionpress_mastodon_sync_one_tick('example.test', '1');
        echo 'done=' . ($r['done'] ? '1' : '0') . ';pages=' . $r['stats']['pages'];
        """
        out = _eval(php, self.url)
        # Expect 2 pages processed (short + empty) and done=1 only
        # because of the EMPTY page.
        self.assertIn("done=1", out)
        self.assertIn("pages=2", out)
        # Regression: before the fix, we'd see pages=1 (short page
        # incorrectly treated as done).

    def test_empty_page_marks_done(self):
        """An immediately-empty page marks backfill done in one step."""
        mock = json.dumps([[]])
        php = self._register_mock(mock) + """
        $r = onionpress_mastodon_sync_one_tick('example.test', '1');
        echo 'done=' . ($r['done'] ? '1' : '0') . ';pages=' . $r['stats']['pages'];
        """
        out = _eval(php, self.url)
        self.assertIn("done=1", out)
        self.assertIn("pages=1", out)

    def test_cycle_guard_stops_after_3_no_progress_rounds(self):
        """Server stuck returning same last-id → error after 3 rounds."""
        # Return the SAME single-item page over and over. max_id cursor
        # won't advance (import_status sees dupe → skipped doesn't
        # change oldest_id... actually oldest_id IS set from the status
        # id regardless of import outcome). So same id means same
        # before_id after: triggers cycle guard.
        stuck_page = [_status("42")]
        mock = json.dumps([stuck_page] * 10)
        php = self._register_mock(mock) + """
        $r = onionpress_mastodon_sync_one_tick('example.test', '1');
        echo 'done=' . ($r['done'] ? '1' : '0')
          . ';pages=' . $r['stats']['pages']
          . ';errs=' . count($r['errors']);
        """
        out = _eval(php, self.url)
        # Should NOT be done (no empty page seen), and errors should
        # have the "cursor stuck" entry after 3 no-progress rounds.
        self.assertIn("done=0", out)
        self.assertIn("errs=1", out)


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestMastodonImporterStatus(unittest.TestCase):
    """Per-status filter logic (include_replies / include_boosts / dedupe)."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def setUp(self):
        _assert_test_sandbox(self.url)
        self.addCleanup(_cleanup_test_posts, self.url)

    def _import(self, status, opts):
        # Pass as JSON strings then json_decode inside PHP — direct
        # string interpolation would collide with PHP's {} block syntax.
        s = json.dumps(status).replace("'", "\\'")
        o = json.dumps(opts).replace("'", "\\'")
        php = f"""
        $s = json_decode('{s}', true);
        $o = json_decode('{o}', true);
        $r = onionpress_mastodon_import_status($s, $o);
        echo $r;
        """
        return _eval(php, self.url)

    def test_dedupe(self):
        """Same source_id imported twice → one post created."""
        sid = "mastodon:test-" + uuid.uuid4().hex
        status = _status(sid)
        first = self._import(status, {"include_boosts": False, "include_replies": False})
        second = self._import(status, {"include_boosts": False, "include_replies": False})
        self.assertEqual(first, "imported")
        self.assertEqual(second, "skipped")

    def test_reply_skipped_when_disabled(self):
        status = _status("r-" + uuid.uuid4().hex, in_reply_to_id="99")
        r = self._import(status, {"include_boosts": False, "include_replies": False})
        self.assertEqual(r, "skipped")

    def test_reply_imported_when_enabled(self):
        status = _status("r-" + uuid.uuid4().hex, in_reply_to_id="99")
        r = self._import(status, {"include_boosts": False, "include_replies": True})
        self.assertEqual(r, "imported")

    def test_boost_skipped_when_disabled(self):
        inner = _status("b-inner-" + uuid.uuid4().hex)
        boost = _status("b-outer-" + uuid.uuid4().hex, reblog=inner)
        r = self._import(boost, {"include_boosts": False, "include_replies": False})
        self.assertEqual(r, "skipped")


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestMastodonDaemonLock(unittest.TestCase):
    """Token-lock mutex semantics for the daemon entry point."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def setUp(self):
        _assert_test_sandbox(self.url)
        _wp(["option", "update", "onionpress_social_mastodon_server",
             "example.test"], url=self.url, timeout=15)
        _wp(["option", "update", "onionpress_social_mastodon_account_id",
             "1"], url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_mastodon_daemon_lock"],
            url=self.url, timeout=15)

    def test_fresh_lock_blocks_second_run(self):
        """A fresh lock < STALE threshold rejects a new run."""
        # Seed a fresh lock that belongs to a fake other token.
        php_seed = """
        update_option('onionpress_social_mastodon_daemon_lock',
                      'otherTok:' . time(), false);
        echo 'seeded';
        """
        _eval(php_seed, self.url)
        # Now call the entry. from_admin=true so we get a notice back.
        # Should return a "already running" warning and NOT change the lock.
        php = """
        add_filter('onionpress_mastodon_fetch_statuses_mock',
                   function($_, $params){ return []; }, 10, 2);
        $r = onionpress_mastodon_run_sync_tick(true);
        $lock = get_option('onionpress_social_mastodon_daemon_lock');
        echo 'level=' . ($r['level'] ?? '?') . ';lock=' . $lock;
        """
        out = _eval(php, self.url)
        self.assertIn("level=warning", out)
        self.assertTrue(out.startswith("level=warning;lock=otherTok:"),
                        f"lock should not have changed owner: {out}")

    def test_stale_lock_is_taken_over(self):
        """A lock older than STALE_SEC is replaced by the new daemon."""
        # Seed a stale lock (heartbeat 9999s old).
        php_seed = """
        update_option('onionpress_social_mastodon_daemon_lock',
                      'deadTok:' . (time() - 9999), false);
        echo 'seeded';
        """
        _eval(php_seed, self.url)
        php = """
        // Mock: return empty so the daemon exits immediately after
        // one iteration. We only care about lock acquisition semantics.
        add_filter('onionpress_mastodon_fetch_statuses_mock',
                   function($_, $params){ return []; }, 10, 2);
        onionpress_mastodon_run_sync_tick(true);
        $lock = (string) get_option('onionpress_social_mastodon_daemon_lock', '(empty)');
        echo 'lock=' . $lock;
        """
        out = _eval(php, self.url)
        # After a clean run, the lock should be deleted (finally {}).
        # If any cleanup path leaves it, confirm it's at least not
        # the stale 'deadTok' value — a new token took over.
        self.assertNotIn("deadTok", out)


if __name__ == "__main__":
    unittest.main()
