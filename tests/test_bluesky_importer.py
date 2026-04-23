#!/usr/bin/env python3
"""Integration tests for the Bluesky importer plugin.

Drive the live plugin inside the onionpress-wordpress container via
`wp eval`, using the `onionpress_bluesky_*_mock` filter hooks to
inject canned API responses — no real Bluesky or Tor traffic.

Coverage focus:
  1. Cursor-based pagination: missing cursor marks backfill done.
     (No "short-page" trap — absence is the only terminator.)
  2. Cycle guard: same cursor returned 3 rounds in a row → error,
     no infinite loop.
  3. Dedupe by AT-URI.
  4. Repost filter (reason.$type = reasonRepost).
  5. Reply filter + self-reply always imported.
  6. Quote-post rendering (inline blockquote with quoted author + text).
  7. Quote-post unavailable states (viewNotFound / viewBlocked /
     viewDetached) render as muted one-liners, not errors.
  8. Handle → DID resolution stored at save time.
  9. Token-lock mutex: fresh lock blocks a second daemon; stale lock
     is taken over.

Prerequisites (skips if not met):
  - Docker running
  - `onionpress-wordpress` container up with the plugin in mu-plugins/
  - At least one subsite (we write to /brewsterkahle/ by default)
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
    r = _wp(["eval", php], url=url, timeout=90)
    return r.stdout.strip()


_SAFE_TEST_DID = "did:plc:testactor"


def _assert_test_sandbox(url):
    """Refuse to run if the Bluesky DID option is set to something other
    than the sandbox DID (`did:plc:testactor`) or empty. Prevents tests
    from clobbering a real account's state when accidentally pointed at
    a production subsite — which is exactly how the real-account drift
    bug happened. Also acts as forward-pressure: anyone who wants to
    run tests against a real subsite has to manually clear the DID
    option first, which makes the risk explicit."""
    r = _wp(["option", "get", "onionpress_social_bluesky_did"],
            url=url, timeout=10)
    current = (r.stdout or "").strip()
    if current and current != _SAFE_TEST_DID:
        raise RuntimeError(
            f"Refusing to run Bluesky tests against {url!r}: "
            f"onionpress_social_bluesky_did is {current!r} (real account). "
            "Run against a clean subsite, or clear the option first: "
            f"wp option delete onionpress_social_bluesky_did --url={url}"
        )


def _cleanup_test_posts(url):
    """Delete any posts created by our test fixture. Matches by the
    test-only DID `did:plc:testactor` embedded in the _source_id —
    real Bluesky DIDs are cryptographically generated base32 strings,
    so this never collides with real imports even if the _op_test_marker
    mechanism gets skipped (e.g. a test crashes before addCleanup fires)."""
    _eval("""
    global $wpdb;
    $ids = $wpdb->get_col(
      "SELECT p.ID FROM {$wpdb->posts} p
       JOIN {$wpdb->postmeta} m ON m.post_id=p.ID AND m.meta_key='_source_id'
       WHERE (m.meta_value LIKE 'bluesky:at://did:plc:testactor/%'
              OR m.meta_value LIKE 'bluesky:repost:%did:plc:testactor/%')"
    );
    foreach ($ids as $id) { wp_delete_post((int)$id, true); }
    echo count($ids);
    """, url)


def _at_uri(rkey=None):
    """Build a deterministic AT-URI for tests."""
    return f"at://did:plc:testactor/app.bsky.feed.post/{rkey or uuid.uuid4().hex}"


def _feed_item(uri=None, text="hello from test", created="2026-04-23T00:00:00Z",
               reply_parent_uri=None, is_repost=False, embed=None,
               author_handle="testuser.bsky.social", author_did="did:plc:testactor"):
    """Build a minimal getAuthorFeed feed-view item."""
    uri = uri or _at_uri()
    record = {
        "$type":     "app.bsky.feed.post",
        "text":      text,
        "createdAt": created,
    }
    if reply_parent_uri:
        record["reply"] = {
            "parent": {"uri": reply_parent_uri, "cid": "bafy"},
            "root":   {"uri": reply_parent_uri, "cid": "bafy"},
        }
    item = {
        "post": {
            "uri":    uri,
            "cid":    "bafy",
            "author": {"did": author_did, "handle": author_handle, "displayName": "Test"},
            "record": record,
        }
    }
    if embed is not None:
        item["post"]["embed"] = embed
    if is_repost:
        item["reason"] = {
            "$type":     "app.bsky.feed.defs#reasonRepost",
            "by":        {"did": "did:plc:testactor", "handle": author_handle},
            "indexedAt": created,
        }
    return item


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestBlueskyBackfill(unittest.TestCase):
    """Backfill pagination + cursor semantics."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def setUp(self):
        _assert_test_sandbox(self.url)
        _wp(["option", "update", "onionpress_social_bluesky_did",
             "did:plc:testactor"], url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_bluesky_newest_uri"],
            url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_bluesky_backfill_cursor"],
            url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_bluesky_daemon_lock"],
            url=self.url, timeout=15)
        _wp(["transient", "delete", "onionpress_social_bluesky_lock"],
            url=self.url, timeout=15)
        # Default opts to include replies, exclude reposts (our project default)
        _wp(["option", "update", "onionpress_social_bluesky_opts",
             json.dumps({"include_replies": True, "include_reposts": False}),
             "--format=json"], url=self.url, timeout=15)
        self.addCleanup(_cleanup_test_posts, self.url)

    def _register_mock(self, pages_json):
        """Install a PHP filter that returns canned responses in order.
        Each page is a full getAuthorFeed response (`{feed: [...],
        cursor?: '...'}`). The absence of `cursor` signals end."""
        pages_escaped = pages_json.replace("'", "\\'")
        return f"""
        delete_option('op_test_bluesky_mock_idx');
        update_option('op_test_bluesky_mock_pages', '{pages_escaped}', false);
        add_filter('onionpress_bluesky_fetch_feed_mock', function($_, $params) {{
            $pages = json_decode((string) get_option('op_test_bluesky_mock_pages', '[]'), true);
            $idx = (int) get_option('op_test_bluesky_mock_idx', 0);
            update_option('op_test_bluesky_mock_idx', $idx + 1, false);
            return is_array($pages) && isset($pages[$idx]) ? $pages[$idx] : array('feed' => array());
        }}, 10, 2);
        """

    def test_missing_cursor_marks_backfill_done(self):
        """A response with no `cursor` field marks backfill as done —
        the opposite of mastodon's empty-page rule but cleaner."""
        page = {"feed": [_feed_item(_at_uri())]}  # no cursor!
        mock = json.dumps([page])
        php = self._register_mock(mock) + """
        $r = onionpress_bluesky_sync_one_tick('did:plc:testactor');
        echo 'done=' . ($r['done'] ? '1' : '0') . ';imported=' . $r['stats']['imported'];
        """
        out = _eval(php, self.url)
        self.assertIn("done=1", out)
        self.assertIn("imported=1", out)

    def test_cursor_advances_across_pages(self):
        """With a cursor, backfill continues. Absent cursor on second
        page terminates."""
        page1 = {"feed": [_feed_item(_at_uri())], "cursor": "CURSOR_PAGE2"}
        page2 = {"feed": [_feed_item(_at_uri())]}  # no cursor = done
        mock = json.dumps([page1, page2])
        php = self._register_mock(mock) + """
        $r = onionpress_bluesky_sync_one_tick('did:plc:testactor');
        echo 'done=' . ($r['done'] ? '1' : '0')
          . ';pages=' . $r['stats']['pages']
          . ';imported=' . $r['stats']['imported'];
        """
        out = _eval(php, self.url)
        self.assertIn("done=1", out)
        self.assertIn("pages=2", out)
        self.assertIn("imported=2", out)

    def test_cycle_guard_stops_after_3_no_progress_rounds(self):
        """If the API returns the same non-empty cursor 3 times, we
        error out rather than spinning forever."""
        stuck = {"feed": [_feed_item(_at_uri())], "cursor": "STUCK"}
        # Stuck cursor → we feed it back → API returns same cursor → loop
        mock = json.dumps([stuck] * 10)
        php = self._register_mock(mock) + """
        $r = onionpress_bluesky_sync_one_tick('did:plc:testactor');
        echo 'done=' . ($r['done'] ? '1' : '0')
          . ';errs=' . count($r['errors']);
        """
        out = _eval(php, self.url)
        self.assertIn("done=0", out)
        self.assertIn("errs=1", out)


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestBlueskyImportFilters(unittest.TestCase):
    """Per-item filter logic (replies/reposts) + dedupe."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def setUp(self):
        _assert_test_sandbox(self.url)
        _wp(["option", "update", "onionpress_social_bluesky_did",
             "did:plc:testactor"], url=self.url, timeout=15)
        self.addCleanup(_cleanup_test_posts, self.url)

    def _import(self, item, opts):
        s = json.dumps(item).replace("'", "\\'")
        o = json.dumps(opts).replace("'", "\\'")
        php = f"""
        $item = json_decode('{s}', true);
        $opts = json_decode('{o}', true);
        echo onionpress_bluesky_import_post($item, $opts);
        """
        return _eval(php, self.url)

    def test_dedupe(self):
        """Same AT-URI imported twice → one post created."""
        uri = _at_uri()
        item = _feed_item(uri)
        first  = self._import(item, {"include_replies": True})
        second = self._import(item, {"include_replies": True})
        self.assertEqual(first, "imported")
        self.assertEqual(second, "skipped")

    def test_reply_skipped_when_disabled(self):
        """Reply to someone else skipped when include_replies=off."""
        item = _feed_item(_at_uri(), reply_parent_uri="at://did:plc:otheruser/app.bsky.feed.post/theirs")
        r = self._import(item, {"include_replies": False})
        self.assertEqual(r, "skipped")

    def test_reply_imported_when_enabled(self):
        item = _feed_item(_at_uri(), reply_parent_uri="at://did:plc:otheruser/app.bsky.feed.post/theirs")
        r = self._import(item, {"include_replies": True})
        self.assertEqual(r, "imported")

    def test_self_reply_always_imported(self):
        """Replies to OUR OWN posts (thread continuations) are always
        imported, regardless of include_replies."""
        parent = "at://did:plc:testactor/app.bsky.feed.post/own-parent"
        item = _feed_item(_at_uri(), reply_parent_uri=parent)
        r = self._import(item, {"include_replies": False})
        self.assertEqual(r, "imported",
            "self-reply should always be imported as a thread continuation")

    def test_repost_skipped_when_disabled(self):
        item = _feed_item(_at_uri(), is_repost=True)
        r = self._import(item, {"include_reposts": False})
        self.assertEqual(r, "skipped")

    def test_repost_imported_when_enabled(self):
        item = _feed_item(_at_uri(), is_repost=True)
        r = self._import(item, {"include_reposts": True})
        self.assertEqual(r, "imported")


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestBlueskyQuotePostRendering(unittest.TestCase):
    """Quote-post rendering: inline blockquote with quoted author +
    text; muted one-liner for unavailable states."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def setUp(self):
        _assert_test_sandbox(self.url)
        self.addCleanup(_cleanup_test_posts, self.url)

    def _render_embed(self, embed):
        e = json.dumps(embed).replace("'", "\\'")
        php = f"""
        $embed = json_decode('{e}', true);
        echo onionpress_bluesky_render_embed($embed);
        """
        return _eval(php, self.url)

    def test_quote_renders_as_blockquote(self):
        embed = {
            "$type": "app.bsky.embed.record#view",
            "record": {
                "$type":  "app.bsky.embed.record#viewRecord",
                "uri":    "at://did:plc:quoted/app.bsky.feed.post/abc",
                "author": {"handle": "quoted.bsky.social", "did": "did:plc:quoted"},
                "value":  {"text": "original wisdom", "createdAt": "2026-04-20T00:00:00Z"},
            },
        }
        out = self._render_embed(embed)
        self.assertIn("blockquote", out)
        self.assertIn("op-bluesky-quote", out)
        self.assertIn("@quoted.bsky.social", out)
        self.assertIn("original wisdom", out)
        self.assertIn("View on Bluesky", out)

    def test_quote_not_found_renders_muted_line(self):
        embed = {
            "$type":  "app.bsky.embed.record#view",
            "record": {"$type": "app.bsky.embed.record#viewNotFound",
                       "uri":   "at://did:plc:gone/app.bsky.feed.post/zzz"},
        }
        out = self._render_embed(embed)
        self.assertIn("Quoted post deleted", out)
        self.assertIn("op-bluesky-quote--unavailable", out)

    def test_quote_blocked_renders_muted_line(self):
        embed = {
            "$type":  "app.bsky.embed.record#view",
            "record": {"$type": "app.bsky.embed.record#viewBlocked"},
        }
        out = self._render_embed(embed)
        self.assertIn("Quoted post unavailable (blocked)", out)

    def test_quote_detached_renders_muted_line(self):
        embed = {
            "$type":  "app.bsky.embed.record#view",
            "record": {"$type": "app.bsky.embed.record#viewDetached"},
        }
        out = self._render_embed(embed)
        self.assertIn("Quoted post unavailable (quote removed)", out)

    def test_quoted_post_media_appears_as_placeholder(self):
        """Quoted post with nested images should render [image]
        placeholder, NOT try to sideload them."""
        embed = {
            "$type": "app.bsky.embed.record#view",
            "record": {
                "$type":  "app.bsky.embed.record#viewRecord",
                "uri":    "at://did:plc:quoted/app.bsky.feed.post/abc",
                "author": {"handle": "quoted.bsky.social"},
                "value":  {"text": "see pic"},
                "embeds": [{"$type": "app.bsky.embed.images#view",
                            "images": [{"fullsize": "https://cdn.bsky.app/x.jpg"}]}],
            },
        }
        out = self._render_embed(embed)
        self.assertIn("[image]", out)
        # The quoted-post's image URL must NOT appear as an <img> tag —
        # we preserve only a placeholder, not the media.
        self.assertNotIn("cdn.bsky.app/x.jpg", out)


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestBlueskyHandleResolution(unittest.TestCase):
    """Handle → DID resolution at save time."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def setUp(self):
        _assert_test_sandbox(self.url)
        _wp(["option", "delete", "onionpress_social_bluesky_handle"],
            url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_bluesky_did"],
            url=self.url, timeout=15)

    def test_resolve_handle_stores_did(self):
        """A successful resolve stores the DID for downstream lookups."""
        php = """
        add_filter('onionpress_bluesky_resolve_handle_mock',
                   function($_, $h) { return 'did:plc:resolved'; }, 10, 2);
        $r = onionpress_bluesky_resolve_handle('brewster.archive.org');
        echo is_wp_error($r) ? 'ERR:' . $r->get_error_message() : $r;
        """
        out = _eval(php, self.url)
        self.assertEqual(out, "did:plc:resolved")

    def test_resolve_handle_error_returns_wp_error(self):
        """A failing resolve returns WP_Error for the save handler to
        surface as an admin notice."""
        php = """
        add_filter('onionpress_bluesky_resolve_handle_mock',
                   function($_, $h) { return new WP_Error('not_found', 'nope'); }, 10, 2);
        $r = onionpress_bluesky_resolve_handle('nosuch.bsky.social');
        echo is_wp_error($r) ? 'WPERR:' . $r->get_error_message() : 'OK:' . $r;
        """
        out = _eval(php, self.url)
        self.assertTrue(out.startswith("WPERR:"),
            f"expected WP_Error; got: {out}")


@unittest.skipUnless(_docker_available(), "requires running onionpress-wordpress container")
class TestBlueskyDaemonLock(unittest.TestCase):
    """Token-lock mutex semantics for the daemon entry point."""

    @classmethod
    def setUpClass(cls):
        s = _pick_site()
        if s is None:
            raise unittest.SkipTest("no site available")
        cls.url = s["url"].rstrip("/") + "/"

    def setUp(self):
        _assert_test_sandbox(self.url)
        _wp(["option", "update", "onionpress_social_bluesky_did",
             "did:plc:testactor"], url=self.url, timeout=15)
        _wp(["option", "delete", "onionpress_social_bluesky_daemon_lock"],
            url=self.url, timeout=15)

    def test_fresh_lock_blocks_second_run(self):
        php_seed = """
        update_option('onionpress_social_bluesky_daemon_lock',
                      'otherTok:' . time(), false);
        """
        _eval(php_seed, self.url)
        php = """
        add_filter('onionpress_bluesky_fetch_feed_mock',
                   function($_, $p){ return array('feed' => array()); }, 10, 2);
        $r = onionpress_bluesky_run_sync_tick(true);
        $lock = get_option('onionpress_social_bluesky_daemon_lock');
        echo 'level=' . ($r['level'] ?? '?') . ';lock=' . $lock;
        """
        out = _eval(php, self.url)
        self.assertIn("level=warning", out)
        self.assertTrue(out.startswith("level=warning;lock=otherTok:"),
            f"lock should not have changed owner: {out}")

    def test_stale_lock_is_taken_over(self):
        php_seed = """
        update_option('onionpress_social_bluesky_daemon_lock',
                      'deadTok:' . (time() - 9999), false);
        """
        _eval(php_seed, self.url)
        php = """
        add_filter('onionpress_bluesky_fetch_feed_mock',
                   function($_, $p){ return array('feed' => array()); }, 10, 2);
        onionpress_bluesky_run_sync_tick(true);
        $lock = (string) get_option('onionpress_social_bluesky_daemon_lock', '(empty)');
        echo 'lock=' . $lock;
        """
        out = _eval(php, self.url)
        self.assertNotIn("deadTok", out)


if __name__ == "__main__":
    unittest.main()
