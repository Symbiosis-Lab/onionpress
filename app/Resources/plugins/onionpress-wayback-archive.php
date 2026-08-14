<?php
/**
 * Plugin Name: OnionPress Wayback Archive
 * Description: Archives the site's posts, home page, and RSS feed to the
 *              Internet Archive's Wayback Machine via Save Page Now (SPN2).
 *              Fire-and-forget pipeline: a 60s cron tick polls outstanding
 *              job_ids in batch, then submits fresh work up to the account's
 *              current available-slots count. No per-URL retry counter,
 *              no back-off chain — a failed submit is simply retried on a
 *              later tick. Two global gates throttle the whole sweep:
 *              (1) self-reachability of our onion, (2) SPN account slots.
 * Version:     4.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

// ───────────────────────────── tunables ─────────────────────────────

// How often wp-cron fires the entry point. The entry point runs a
// daemon-style inner loop for up to OP_WB_LOOP_MAX_SEC, so cron only
// has to fire as a watchdog that restarts the loop if it died. 5 min
// is plenty — if the loop is still running, cron is a no-op (mutex).
//
// Deliberately NOT raised alongside OP_WB_LOOP_LOCK_STALE_SEC. A fire that
// lands while the daemon is alive costs one option read, and this interval is
// what bounds how long a dead daemon's stale lock sits unclaimed (worst case
// LOCK_STALE + this), so raising it would only slow recovery. It also has to
// stay under the 10-minute window wp_schedule_single_event() dedupes within —
// see onionpress_wayback_schedule_sweep_now(), where "the recurring entry is
// ALWAYS inside that window" is load-bearing for how the kick path behaves.
define( 'OP_WB_CRON_INTERVAL', 300 );

// Max new submissions per sweep tick. Through onionheaven SOCKS, 40
// is the sweet spot — sweeps complete in 50-80s, leaving headroom for
// poll/CDX. Bumping to 60 stretched elapsed to >120s (12 chunks of
// concurrency=5) and hurt total throughput despite higher per-tick
// submit count.
define( 'OP_WB_SUBMIT_BATCH_MAX', 40 );

// Max concurrent in-flight curl handles. Measured ceiling: 10 works
// reliably through our onionpress-tor SOCKS but starves other Tor
// consumers (notably the OnionHeaven heartbeat running in a sibling
// container). Dropped to 5 so the SPN loop leaves circuit headroom
// for the heartbeat and reachability checks. 20 saturates outright.
define( 'OP_WB_CONCURRENT_MAX', 5 );

// Max job_ids bundled into a single /save/status POST. SPN accepts
// comma-separated lists; 20 is comfortable.
define( 'OP_WB_STATUS_BATCH_MAX', 20 );

// If a job has been "pending" for this long, something went wrong at
// SPN — clear the job_id so the next tick resubmits fresh.
define( 'OP_WB_STALE_PENDING_SEC', 300 );

// Per-request Tor timeouts. Both are ceilings paid only by a request that
// fails to make progress: a warm onion round-trip measures ~3.6s and comes
// nowhere near either.
//
// They are sized for the COLD path, because that is the one that decides
// whether the archiver works at all after a restart. The first connection to
// an onion has to fetch its descriptor from an HSDir and build a rendezvous
// circuit, measured at 25-70s; Tor abandons the attempt around 70s. At the
// previous CONNECTTIMEOUT of 15 every request in the minutes after a Tor
// restart, a laptop wake, or a NEWNYM that flushed the descriptor cache
// failed before the lookup could possibly have finished — and the sweep
// reported that as SPN being unreachable, which it was not.
//
// 70 for the connect because 70 is where Tor itself gives up: a connect still
// outstanding past that is waiting on something Tor has already abandoned, so
// a larger ceiling buys nothing and only burns wall clock that the sweep
// budget below then has to account for. 75 total leaves the HTTP exchange the
// few seconds a warm round-trip costs, on top of a connect that used its
// whole allowance.
//
// tor-watchdog.py keeps these descriptors warm (see its WARM_DESCRIPTOR_
// INTERVAL), so the cold path should be rare. These ceilings are the floor
// for correctness when it is not — not the normal case.
define( 'OP_WB_CONNECT_TIMEOUT_SEC', 70 );
define( 'OP_WB_REQUEST_TIMEOUT_SEC', 75 );

// Per-sweep wall-clock budget, enforced between phases (poll, CDX rescue,
// submit). It used to be 45 and enforced nowhere — $deadline was computed
// and then only ever used to derive `elapsed` for the log — so the real
// worst case was unbounded: submit_parallel alone can run 8 sequential
// chunks at a full request timeout.
//
// What the budget actually protects is the lock. The loop heartbeats
// op_wayback_sweep_lock around each iteration and around each subsite, so an
// iteration that outruns OP_WB_LOOP_LOCK_STALE_SEC lets a second daemon
// declare the lock dead and start up alongside this one, both writing the
// same records.
//
// The bound has to be stated as budget PLUS overshoot, because every gate is
// checked on phase entry and the phase it admits then runs to its own
// timeout. Writing T for OP_WB_REQUEST_TIMEOUT_SEC (75s), each phase can
// overshoot by at most one T:
//   poll    groups = max(1, floor(left / T)); the floor is the only way a
//           group starts with less than T left               = +T
//   CDX     entered only under the deadline, one group       = +T
//   submit  entered only under the deadline — but it pays a reachability
//           probe first, which is itself a full request, so the deadline is
//           re-checked AFTER the probe and the group likewise only starts
//           under it                                          = +T
// Each phase re-checks before starting, so at most one of them overshoots:
//   one iteration ≤ BUDGET + T = 450 + 75 = 525s
// which is what OP_WB_LOOP_LOCK_STALE_SEC = BUDGET + 2T = 600 is derived
// from. (Under the old 40s timeouts this read 200 + 60 = 260 against a 300
// threshold. Raising the timeouts for the cold Tor path invalidated every
// number in it — the same 200 with T=75 would allow 200 + 150 = 350 against
// that 300, i.e. a nominal iteration that outlives the lock.)
//
// The budget also SIZES the batches, because both phases divide the time
// left by T, and that is why it had to grow rather than shrink. 450 keeps
// throughput at parity with the old 200/40s pair once the status probe is
// paid (~4s warm):
//   submit  floor((446 - 75) / 75) = 4 groups × 5 = 20 URLs
//           (old: floor((196 - 20) / 40) = 4 groups × 5 = 20 URLs)
//   poll    floor(446 / 75)        = 5 groups × 100 = 500 job_ids
//           (old: floor(196 / 40)  = 4 groups × 100 = 400 job_ids)
// Leaving the budget at 200 would not have been merely slower: it would have
// sized the submit down to a single group of 5 URLs per iteration.
define( 'OP_WB_SWEEP_BUDGET_SEC', 450 );

// Don't poll a job younger than this — SPN's minimum capture time is
// ~20s, so polling immediately wastes a Tor round-trip on a guaranteed
// "pending" answer.
define( 'OP_WB_YOUNG_JOB_SKIP_SEC', 15 );

// Continuous-loop tuning. On a cron tick, the sweep enters a while
// loop and runs indefinitely until the queue is drained, then exits.
// A mutex prevents overlapping invocations; the lock timestamp is
// heartbeated every iteration so a crashed process becomes unstuck
// quickly rather than holding the lock until MAX_SEC expires.
//
// If the loop crashes partway, the next cron tick (whenever a page
// view wakes wp-cron) sees a stale lock and restarts.
define( 'OP_WB_LOOP_IDLE_SLEEP',    30 );  // between iterations when work was done
define( 'OP_WB_LOOP_NOWORK_SLEEP',  90 );  // when iteration found nothing to submit
// Lock not heartbeated in this long = dead. Two gaps have to fit under it,
// because heartbeats bracket the work rather than happening inside it:
//   - one sweep_iteration, worst case BUDGET + T = 525s (the budget comment
//     above derives that; the heartbeat is per subsite, so it is ONE
//     iteration that has to fit, not the whole pass over the network);
//   - the inter-iteration sleep, OP_WB_LOOP_IDLE_SLEEP normally and at most
//     the longest back-off — OP_WB_BACKOFF_BAD_CREDENTIALS at 300s.
// 600 = BUDGET + 2T covers the first with a whole request timeout to spare
// and the second twice over.
//
// The cost of the cold Tor path is paid here: a genuinely dead daemon is now
// reclaimed in up to 10 minutes rather than 5. That is the trade for a daemon
// that can sit through a 70s descriptor lookup without a second one deciding
// it has died mid-request and starting up alongside it — which is strictly
// worse, because two daemons write the same records. Recovery latency stays
// bounded on the other side by OP_WB_CRON_INTERVAL, deliberately left at 300.
define( 'OP_WB_LOOP_LOCK_STALE_SEC', 600 );
// Hard ceiling on one daemon's lifetime. The comments above and on
// onionpress_wayback_sweep_loop have always described this cap, but the
// constant was never defined and the loop was `while (true)` with no
// time-based exit — so a daemon that always found work ran forever.
//
// That is not merely untidy. WordPress caches options and post queries
// per REQUEST, and this daemon is a single request; a process that never
// exits never refreshes those caches. One that had been alive 70 hours
// went on reading a job_id that had been deleted from the database hours
// earlier, and because a non-empty job_id is itself what marks the queue
// "still has work", the stale read kept the loop alive that was keeping
// the read stale. Recycling on a timer breaks that circuit: cron starts
// a fresh process with a fresh cache within a minute or two.
define( 'OP_WB_LOOP_MAX_SEC',      1800 ); // recycle the daemon every 30 min

// How long a self-reachability verdict may be reused. One iteration
// visits every subsite and asks once per subsite, but the answer belongs
// to the onion service, not the subsite. Short enough that a recovering
// onion is noticed within an iteration or two.
define( 'OP_WB_SELF_REACHABLE_TTL',  60 );

// Per-URL retry back-off, after SPN has reported an error for that URL.
//
// SPN failing a URL is not a reason to stop trying it — captures fail for
// transient reasons all the time, and the CDX rescue verifies each error
// against the record before writing it off. But nothing gated the RETRY.
// A failed URL returns to "no archived_at, no job_id", which is exactly
// what posts_needing_submit selects on, so it was resubmitted on the very
// next sweep and every sweep after that, forever.
//
// Measured on this stack before the fix: 17 submissions, 9 errors, 2.4
// submissions a minute, a 100% failure rate — against an SPN account shared
// by every OnionPress node. The archiver was not retrying, it was storming,
// and the cost of that landed on other people's sites.
//
// Doubling from 5 minutes to a day (300, 600, 1200, … 86400) puts ~9
// submissions in a permanently-failing URL's first day rather than ~3400,
// and still retries a transient failure within five minutes. Recorded per
// record in error_count, cleared the moment a capture succeeds.
define( 'OP_WB_RETRY_BASE_SEC',  300 );
define( 'OP_WB_RETRY_MAX_SEC', 86400 );

// Candidate over-fetch when assembling a submit batch. The back-off above is
// per record and exponential in that record's own error_count, so it cannot
// be expressed in the meta_query — it is applied as the batch is built.
// Asking for exactly the budget would let the newest N posts, all of them
// still cooling off, hide every older post that is ready; the queue would
// stall with work in it. 4x, with a ceiling so the query stays bounded.
define( 'OP_WB_SUBMIT_CANDIDATE_FACTOR', 4 );
define( 'OP_WB_SUBMIT_CANDIDATE_MAX',  200 );

// Back-off durations (written to op_wayback_backoff_until option).
define( 'OP_WB_BACKOFF_NO_SLOTS',    20 );  // SPN says available=0
define( 'OP_WB_BACKOFF_UNREACHABLE', 120 ); // our own onion not responding
// SPN cannot accept our work until a human changes a setting: either no S3
// keys are configured at all, or SPN answered 401/403 for the ones that are.
// Long, because nothing about either state improves on a timer — but not
// permanent, because a 403 through Tor might not have been about our keys at
// all. Someone who has just fixed their keys presses the admin page's Kick
// button, which clears the back-off outright.
//
// Any value here is bounded by OP_WB_LOOP_LOCK_STALE_SEC: the loop sleeps a
// back-off out BETWEEN heartbeats, so a back-off past the stale threshold
// would have a live daemon declare itself dead. 300 against 600 leaves half.
define( 'OP_WB_BACKOFF_BAD_CREDENTIALS', 300 );
// No OP_WB_BACKOFF_SPN_DOWN: an unreachable /save/status/user does NOT
// back the sweep off. The gate below says why — a status call failing is
// usually Tor jitter, and pausing every sweep for it starves the queue
// while submits would still have gone through. The constant existed for
// years, referenced nowhere, implying a behaviour the code had rejected.

// Meta keys (kept compatible with v3 for the already-archived posts).
define( 'OP_WB_META_ARCHIVED_AT',     '_op_wayback_archived_at' );
define( 'OP_WB_META_SNAPSHOT_TS',     '_op_wayback_snapshot_ts' );
define( 'OP_WB_META_JOB_ID',          '_op_wayback_job_id' );
define( 'OP_WB_META_SUBMITTED_AT',    '_op_wayback_submitted_at' );
define( 'OP_WB_META_ORIGINAL_URL',    '_op_wayback_original_url' );
define( 'OP_WB_META_DURATION_SEC',    '_op_wayback_duration_sec' );
define( 'OP_WB_META_RESOURCES_COUNT', '_op_wayback_resources_count' );
define( 'OP_WB_META_OUTLINKS_COUNT',  '_op_wayback_outlinks_count' );
define( 'OP_WB_META_LAST_ERROR_EXT',  '_op_wayback_last_error_ext' );
define( 'OP_WB_META_LAST_ERROR_AT',   '_op_wayback_last_error_at' );
define( 'OP_WB_META_ERROR_COUNT',     '_op_wayback_error_count' );
// Set when the post has been re-archived once due to a comment being
// added (i.e. social-importer threading folded a reply into this post).
// Caps the comment-driven re-archive at one snapshot per post — without
// this, every comment a thread accumulates would re-trigger SPN, which
// is the budget waste the once-only policy was originally designed to
// avoid. With it: a thread of 12 self-replies re-archives the parent
// exactly once after the first reply lands.
define( 'OP_WB_META_RESNAPSHOT_DONE',  '_op_wayback_resnapshot_done' );

// wp_options keys.
define( 'OP_WB_OPT_HOME',          'op_wayback_home_state' );
define( 'OP_WB_OPT_FEED',          'op_wayback_feed_state' );
define( 'OP_WB_OPT_BACKOFF_UNTIL', 'op_wayback_backoff_until' );

// Every page of the live moss generation keeps its state in this ONE option,
// shaped { generation: '<id>', urls: { '<path>': {job_id, archived_at, …} } }.
// One row rather than one option per page: the set is bounded (see
// OP_WB_MOSS_MAX_PAGES), the whole thing is read and written atomically, and
// the sweep holds a single-instance lock so there is no concurrent writer to
// lose an update to. The generation id rides along because it is what makes a
// publish re-archive — see onionpress_wayback_moss_records().
define( 'OP_WB_OPT_MOSS',          'op_wayback_moss_state' );

// Ceiling on how many moss pages are tracked. This is a storage bound, not a
// throughput one — the sweep already drains whatever it enumerates across
// iterations at OP_WB_SUBMIT_BATCH_MAX a time. At roughly 150 serialized
// bytes per archived page, 1000 pages is ~150 kB in one non-autoloaded
// option, which is unremarkable; a hundred thousand would not be. Pages past
// the cap are not archived, and the enumeration log line says so rather than
// dropping them silently.
define( 'OP_WB_MOSS_MAX_PAGES', 1000 );

// How many files the fallback directory walk may look at before giving up on
// the pages it has found so far. Only reached when a generation ships no
// sitemap.xml AND carries a very large tree; the walk already refuses to
// descend into moss's asset mount, which is what makes a normal site cost far
// less than this.
define( 'OP_WB_MOSS_SCAN_MAX', 20000 );

// ─────────────────────────── logging + helpers ──────────────────────

function onionpress_wayback_log( $msg ) {
    error_log( '[OnionPress Wayback] ' . $msg );
}

function onionpress_wayback_version() {
    static $ver = null;
    if ( $ver === null ) {
        $f = '/var/lib/onionpress/version';
        $ver = file_exists( $f ) ? trim( (string) @file_get_contents( $f ) ) : 'dev';
    }
    return $ver;
}

function onionpress_wayback_auth_header() {
    $access = get_blog_option( 1, 'onionpress_archive_s3_access', '' );
    $secret = get_blog_option( 1, 'onionpress_archive_s3_secret', '' );
    if ( empty( $access ) || empty( $secret ) ) {
        return '';
    }
    return 'LOW ' . $access . ':' . $secret;
}

function onionpress_wayback_onion_addr() {
    $f = '/var/lib/onionpress/onion_address';
    if ( ! file_exists( $f ) ) {
        return '';
    }
    return trim( (string) @file_get_contents( $f ) );
}

function onionpress_wayback_post_url( $post_id ) {
    $onion = onionpress_wayback_onion_addr();
    if ( empty( $onion ) ) {
        return '';
    }
    $path = wp_parse_url( get_permalink( $post_id ), PHP_URL_PATH );
    return $path ? 'http://' . $onion . $path : '';
}

function onionpress_wayback_home_url_full() {
    $onion = onionpress_wayback_onion_addr();
    if ( empty( $onion ) ) {
        return '';
    }
    $path = wp_parse_url( home_url( '/' ), PHP_URL_PATH ) ?: '/';
    return 'http://' . $onion . $path;
}

/**
 * The feed URL to archive.
 *
 * `/feed/` is a WordPress route. A moss generation replaces the whole site at
 * the onion root and publishes its feed at `/rss.xml` — it emits nothing at
 * `/feed/` at all, so on a moss site the archiver was submitting WordPress's
 * own empty feed to SPN, sweep after sweep, and never once archiving the feed
 * a reader actually subscribes to.
 *
 * Decided by the file being there, not by "is a generation live": moss emits
 * rss.xml and sitemap.xml only once its site_url is *deployed* (`has_rss` in
 * its renderer gates both), so a site published before its onion name was
 * registered genuinely has no rss.xml and WordPress's route is still the only
 * feed that exists.
 *
 * And only on the site moss actually serves. `/rss.xml` sits at the onion
 * ROOT, so it is the main site's feed; a subsite's readers subscribe to
 * `http://<onion>/<subsite>/feed/`, which moss neither publishes nor replaces.
 * Gating on moss_is_owner() alone — a container-global answer — swapped every
 * subsite's own feed for the main site's rss.xml.
 */
function onionpress_wayback_feed_url_full() {
    $onion = onionpress_wayback_onion_addr();
    if ( empty( $onion ) ) {
        return '';
    }
    if ( onionpress_wayback_moss_serves_this_site() ) {
        $gen = onionpress_wayback_moss_generation();
        if ( $gen !== null && is_file( $gen['dir'] . '/rss.xml' ) ) {
            return 'http://' . $onion . '/rss.xml';
        }
    }
    $path = wp_parse_url( home_url( '/' ), PHP_URL_PATH ) ?: '/';
    return 'http://' . $onion . rtrim( $path, '/' ) . '/feed/';
}

/**
 * Shared curl setup — all our HTTP goes through Tor SOCKS.
 *
 * Route chosen: onionheaven container's Tor, NOT onionpress-tor. This
 * isolates our outgoing SPN bursts from the onion service we're trying
 * to keep reachable. The onionpress-tor daemon does double duty — it
 * serves our onion AND handles the heartbeat — so heavy outgoing curl
 * bursts through it starve both inbound SPN crawls and the heartbeat,
 * which cascades into takeover. onionheaven container has its own Tor
 * with spare capacity, and already routes reachability probes, so
 * layering SPN traffic through it leaves onionpress-tor focused on
 * serving incoming traffic + keeping the heartbeat fresh.
 */
function onionpress_wayback_curl_common( $ch ) {
    curl_setopt_array( $ch, array(
        CURLOPT_RETURNTRANSFER    => true,
        CURLOPT_FOLLOWLOCATION    => true,
        CURLOPT_UNRESTRICTED_AUTH => true,
        CURLOPT_MAXREDIRS         => 3,
        CURLOPT_USERAGENT         => 'OnionPress/' . onionpress_wayback_version(),
        CURLOPT_PROXY             => 'socks5h://onionheaven:9050',
        CURLOPT_PROXYTYPE         => CURLPROXY_SOCKS5_HOSTNAME,
        CURLOPT_SSL_VERIFYPEER    => false,
        CURLOPT_SSL_VERIFYHOST    => 0,
        // Through socks5h the SOCKS handshake does not complete until Tor has
        // the descriptor and a rendezvous circuit, so a cold onion's whole
        // 25-70s lands in curl's CONNECT phase — see the constant.
        CURLOPT_CONNECTTIMEOUT    => OP_WB_CONNECT_TIMEOUT_SEC,
    ) );
}

// ───────────────────── gates (self-reachability, slots) ─────────────

/**
 * HEAD our own onion. SPN's crawler follows the same path a client would,
 * so if we can't reach ourselves through Tor there's no point submitting
 * anything to SPN — every job will come back error:no-captures.
 *
 * Only 200 or 301 count. A 302 indicates the OnionHeaven takeover
 * redirector is in front of us, NOT the real WordPress instance — we
 * haven't fully come up yet.
 */
function onionpress_wayback_self_reachable( $onion ) {
    // Test hook: return non-null to short-circuit the HTTP check.
    $mock = apply_filters( 'onionpress_wayback_self_reachable_mock', null, $onion );
    if ( $mock !== null ) {
        return (bool) $mock;
    }
    // Short-lived memo. The daemon calls this once per subsite per loop
    // iteration, and the answer is a property of the onion service, not of
    // the subsite — on a four-blog network that was four identical Tor
    // round-trips inside one iteration, spent out of the same budget the
    // iteration has to finish in. Deliberately a TTL and not a plain
    // static: this process lives up to OP_WB_LOOP_MAX_SEC, and caching a
    // "down" verdict for half an hour would outlast the outage.
    static $memo = array(); // onion => array( expires_at, result )
    if ( isset( $memo[ $onion ] ) && $memo[ $onion ][0] > microtime( true ) ) {
        return $memo[ $onion ][1];
    }
    $ch = curl_init( 'http://' . $onion . '/' );
    onionpress_wayback_curl_common( $ch );
    curl_setopt_array( $ch, array(
        CURLOPT_NOBODY         => true,
        CURLOPT_FOLLOWLOCATION => false, // a 302 means redirector, not us
        CURLOPT_TIMEOUT        => OP_WB_REQUEST_TIMEOUT_SEC,
    ) );
    curl_exec( $ch );
    $code = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    curl_close( $ch );
    $ok = ( $code === 200 || $code === 301 );
    // Expiry dated from AFTER the probe, not from before it. The probe can now
    // outlast the TTL on a cold descriptor (75s ceiling against a 60s TTL), and
    // dating the entry from the start of a probe that slow writes a memo that
    // is already expired — every subsite would re-probe and the memo would
    // cost a dictionary write for nothing, precisely on the slow path it
    // exists to protect.
    $memo[ $onion ] = array( microtime( true ) + OP_WB_SELF_REACHABLE_TTL, $ok );
    return $ok;
}

/**
 * Age in seconds of an in-flight record, or null when we never recorded a
 * submitted_at (a v3-era row, or a write that died between its two halves).
 *
 * The distinction matters three separate ways in one sweep — ripeness,
 * stale-pending, and forgotten — and the three used to spell "unknown"
 * differently (PHP_INT_MAX in one place, null in two others). They happened
 * to agree; a fourth reader would not have been so lucky.
 */
function onionpress_wayback_job_age( array $rec, $now ) {
    return empty( $rec['submitted_at'] ) ? null : ( $now - (int) $rec['submitted_at'] );
}

/**
 * GET /save/status/user. Returns the decoded body, or null when we have no
 * usable slot count.
 *
 * $status is an out-param carrying WHY, because null used to mean three
 * unrelated things at once — no credentials configured, credentials SPN
 * refused, and a request that never came back — and the caller collapsed all
 * three into the same optimistic available=40. The log then printed an
 * identical healthy "avail=40" whichever it was, which cost one whole
 * debugging session spent looking for an outage in a service that had been
 * answering the entire time. Same out-param shape as poll_parallel's
 * $covered, and for the same reason: the return value cannot express the
 * distinction, and guessing at it is what did the damage.
 *
 *   'ok'             body parsed and carries an available count
 *   'no-credentials' no S3 keys configured — we never asked SPN anything
 *   'rejected'       SPN answered 401/403: it refuses these keys
 *   'timeout'        no HTTP response at all — Tor could not reach SPN
 *   'http-<code>'    SPN answered, but not with a 200
 *   'malformed'      a 200 whose body is not a status object we understand
 */
function onionpress_wayback_user_status( &$status = null ) {
    // Test hook: return an array to short-circuit the SPN call.
    $mock = apply_filters( 'onionpress_wayback_user_status_mock', null );
    if ( $mock !== null ) {
        // A non-array mock means "the call did not produce a slot count", and
        // its default cause is the common one. The companion filter is how a
        // test names a different cause — the credential branches below have
        // no other seam, exactly as poll_parallel's covered-mock exists so
        // "the batch never came back" can be expressed at all.
        $status = is_array( $mock ) ? 'ok' : (string) apply_filters(
            'onionpress_wayback_user_status_reason_mock', 'timeout' );
        return is_array( $mock ) ? $mock : null;
    }
    $auth = onionpress_wayback_auth_header();
    if ( empty( $auth ) ) {
        $status = 'no-credentials';
        return null;
    }
    $ch = curl_init( 'https://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/save/status/user?t=' . time() );
    onionpress_wayback_curl_common( $ch );
    curl_setopt_array( $ch, array(
        CURLOPT_TIMEOUT    => OP_WB_REQUEST_TIMEOUT_SEC,
        CURLOPT_HTTPHEADER => array(
            'Accept: application/json',
            'Authorization: ' . $auth,
        ),
    ) );
    $response = curl_exec( $ch );
    $code     = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    curl_close( $ch );
    if ( $code === 0 ) {
        // curl never got an HTTP response: SOCKS refused, descriptor lookup
        // gave up, or the transfer outran OP_WB_REQUEST_TIMEOUT_SEC. All of
        // them are "we could not ask", which is the case the optimistic
        // proceed in the caller is for.
        $status = 'timeout';
        return null;
    }
    if ( $code === 401 || $code === 403 ) {
        $status = 'rejected';
        return null;
    }
    if ( $code !== 200 ) {
        $status = 'http-' . $code;
        return null;
    }
    // An empty 200 body falls through to the parse below deliberately: it is
    // a 200 that carries no slot count, which is 'malformed', not 'http-200'.
    $body = @json_decode( (string) $response, true );
    // A 200 without an `available` key is not a slot count. It used to fall
    // through to (int) ( $user['available'] ?? 0 ) in the caller, so SPN
    // answering 200 with {"message":"…"} — a rate-limit notice, a maintenance
    // page — read as available=0 and set a back-off on a number nothing had
    // reported. Say we don't know instead, and let a real 429 on the submit
    // be what backs the sweep off.
    if ( ! is_array( $body ) || ! isset( $body['available'] ) ) {
        $status = 'malformed';
        return null;
    }
    $status = 'ok';
    return $body;
}

// ────────────────────────── SPN submit + poll ───────────────────────

/**
 * Generic curl_multi runner. $setups is an array keyed by arbitrary ID,
 * each value is a callable receiving a fresh curl handle to configure.
 * Returns an array keyed the same way; each value is
 *   ['code' => int, 'body' => string]
 * regardless of success, so callers can decide what counts as an error.
 *
 * This is the heart of the throughput improvement — instead of paying
 * ~3-5s of Tor round-trip per request serially, all requests run in
 * parallel and the total time is roughly the slowest single request.
 */
function onionpress_wayback_curl_multi( array $setups ) {
    if ( empty( $setups ) ) {
        return array();
    }
    // Test hook: return a keyed array of ['code'=>int,'body'=>string] to
    // stand in for the whole parallel fetch. This is the only seam below
    // the per-caller mocks, and it exists so the response-handling code
    // above it — chunk/result alignment, the HTTP-200 gate, the
    // coverage bookkeeping in poll_parallel — can be tested at all.
    // Mocking each caller instead left that code with no tests, which is
    // precisely where a silent misclassification would live.
    $mock = apply_filters( 'onionpress_wayback_curl_multi_mock', null, $setups );
    if ( is_array( $mock ) ) {
        return $mock;
    }
    $mh = curl_multi_init();
    $handles = array();
    foreach ( $setups as $key => $configure ) {
        $ch = curl_init();
        onionpress_wayback_curl_common( $ch );
        $configure( $ch );
        curl_multi_add_handle( $mh, $ch );
        $handles[ $key ] = $ch;
    }
    do {
        $status = curl_multi_exec( $mh, $running );
        if ( $running > 0 ) {
            curl_multi_select( $mh, 1.0 );
        }
    } while ( $running > 0 && $status === CURLM_OK );

    $results = array();
    foreach ( $handles as $key => $ch ) {
        $body = (string) curl_multi_getcontent( $ch );
        $code = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
        curl_multi_remove_handle( $mh, $ch );
        curl_close( $ch );
        $results[ $key ] = array( 'code' => $code, 'body' => $body );
    }
    curl_multi_close( $mh );
    return $results;
}

/**
 * Submit many URLs to SPN in parallel. $urls is an array keyed by any
 * stable ID (caller's choice) with values being the URL to submit.
 * Returns an array keyed the same way; each value is a string:
 *   - a job_id on success
 *   - 'RATE_LIMITED' if SPN returned 429 for that URL
 *   - '' if the submit failed
 */
function onionpress_wayback_submit_parallel( array $urls ) {
    if ( empty( $urls ) ) {
        return array();
    }
    // Test hook: return a same-keyed map to short-circuit the SPN submit.
    $mock = apply_filters( 'onionpress_wayback_submit_parallel_mock', null, $urls );
    if ( is_array( $mock ) ) {
        return $mock;
    }
    $auth = onionpress_wayback_auth_header();
    if ( empty( $auth ) ) {
        return array_fill_keys( array_keys( $urls ), '' );
    }
    $headers = array( 'Accept: application/json', 'Authorization: ' . $auth );

    // Speed-tuning params per SPN docs (Tips for faster captures):
    //   skip_first_archive=1      don't compute "is this a first?"
    //   js_behavior_timeout=0     skip JS behaviors (WP content is server-rendered)
    // Dropped `if_not_archived_within=1h` — on retries after a failed
    // onion crawl, SPN was returning the cached error instead of re-
    // trying with a fresh circuit. Relying on SPN's built-in default
    // (45 min) lets genuinely failed URLs retry sooner on new circuits.
    //
    // Chunk the URL list so we never fire more than OP_WB_CONCURRENT_MAX
    // handles at once — Tor SOCKS saturates above that point and every
    // request fails with code=0. Each chunk runs in parallel, chunks run
    // sequentially.
    $results = array();
    $keys = array_keys( $urls );
    foreach ( array_chunk( $keys, OP_WB_CONCURRENT_MAX ) as $key_chunk ) {
        $setups = array();
        foreach ( $key_chunk as $key ) {
            $url = $urls[ $key ];
            $setups[ $key ] = function ( $ch ) use ( $url, $headers ) {
                curl_setopt_array( $ch, array(
                    CURLOPT_URL        => 'https://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/save',
                    CURLOPT_POST       => true,
                    CURLOPT_POSTFIELDS => http_build_query( array(
                        'url'                 => $url,
                        'skip_first_archive'  => 1,
                        'js_behavior_timeout' => 0,
                    ) ),
                    CURLOPT_TIMEOUT    => OP_WB_REQUEST_TIMEOUT_SEC,
                    CURLOPT_HTTPHEADER => $headers,
                ) );
            };
        }
        $raw = onionpress_wayback_curl_multi( $setups );
        foreach ( $raw as $key => $r ) {
            if ( $r['code'] === 429 ) {
                $results[ $key ] = 'RATE_LIMITED';
                continue;
            }
            if ( $r['code'] < 200 || $r['code'] >= 400 || empty( $r['body'] ) ) {
                $results[ $key ] = '';
                continue;
            }
            $data = @json_decode( $r['body'], true );
            $results[ $key ] = ( is_array( $data ) && ! empty( $data['job_id'] ) )
                ? (string) $data['job_id'] : '';
        }
    }
    return $results;
}

/**
 * Ask CDX for the latest Wayback capture of each URL, returning the
 * YYYYMMDDHHMMSS timestamp on hit or '' on miss / transport failure.
 *
 * SPN's /save/status memory is unreliable: it flips "success" →
 * "error:no-captures" for the same job_id a few minutes later even when
 * the capture persists in the Wayback Machine. Before trashing a post
 * on SPN's verdict, we verify against CDX directly.
 *
 * (Tried /wayback/available as a lighter primary path — it's not
 * exposed on the archive.org onion mirror and 404s every time. CDX
 * is what works through Tor.)
 *
 * One retry on transport failure (Tor circuit jitter is common; the
 * retry catches most transient 504s without doubling traffic on good
 * circuits).
 */
function onionpress_wayback_cdx_lookup_parallel( array $urls ) {
    if ( empty( $urls ) ) {
        return array();
    }
    // Test hook: return a same-keyed map to short-circuit the CDX call.
    $mock = apply_filters( 'onionpress_wayback_cdx_lookup_parallel_mock', null, $urls );
    if ( is_array( $mock ) ) {
        return $mock;
    }
    // Single pass — no retry. Misses are acceptable: if CDX didn't see
    // a capture this tick, SPN either really hasn't archived it yet or
    // the rescue call itself transient-failed, and either way the URL
    // will be re-submitted on the next sweep, with another shot at
    // rescue later. Retrying here just doubles the Tor load on misses.
    return onionpress_wayback_cdx_one_pass( $urls );
}

// Back-compat alias for the new name.
function onionpress_wayback_availability_parallel( array $urls ) {
    return onionpress_wayback_cdx_lookup_parallel( $urls );
}

/**
 * Single CDX query pass. $urls is keyed map key → URL. Returns a
 * same-keyed map of key → timestamp (empty string on miss).
 */
function onionpress_wayback_cdx_one_pass( array $urls ) {
    $headers = array( 'Accept: application/json' );
    $result = array_fill_keys( array_keys( $urls ), '' );
    foreach ( array_chunk( array_keys( $urls ), OP_WB_CONCURRENT_MAX ) as $key_chunk ) {
        $setups = array();
        foreach ( $key_chunk as $key ) {
            $url_no_scheme = preg_replace( '#^https?://#', '', $urls[ $key ] );
            $endpoint = 'https://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/cdx/search/cdx?'
                . 'url=' . urlencode( $url_no_scheme ) . '&output=json&limit=-1';
            $setups[ $key ] = function ( $ch ) use ( $endpoint, $headers ) {
                curl_setopt_array( $ch, array(
                    CURLOPT_URL        => $endpoint,
                    CURLOPT_TIMEOUT    => OP_WB_REQUEST_TIMEOUT_SEC,
                    CURLOPT_HTTPHEADER => $headers,
                ) );
            };
        }
        $raw = onionpress_wayback_curl_multi( $setups );
        foreach ( $raw as $key => $r ) {
            if ( $r['code'] !== 200 || empty( $r['body'] ) ) continue;
            $data = @json_decode( $r['body'], true );
            if ( ! is_array( $data ) || count( $data ) < 2 ) continue;
            // Row 0 is the CDX header; subsequent rows are capture
            // records. Timestamp is index 1. limit=-1 sorts oldest-
            // first so the last element is the most recent capture.
            $last = end( $data );
            if ( is_array( $last ) && ! empty( $last[1] ) ) {
                $result[ $key ] = (string) $last[1];
            }
        }
    }
    return $result;
}

/**
 * Poll SPN for many job_ids in parallel. $job_ids is a flat list; this
 * function chunks them into batches of OP_WB_STATUS_BATCH_MAX, fires
 * one POST per batch concurrently, and returns the flattened response
 * objects. Each object is the raw SPN status dict (with 'job_id',
 * 'status', and on success 'timestamp', 'original_url', 'duration_sec',
 * 'resources', 'outlinks').
 */
function onionpress_wayback_poll_parallel( array $job_ids, &$covered = null ) {
    // $covered is an out-param: the set of job_ids whose batch actually
    // came back parseable, as a job_id => true map. The caller needs it to
    // tell "SPN answered and did not mention this job" (it forgot it) from
    // "the batch carrying this job never came back" (we simply don't know).
    // Both look identical in the return value — an absent entry — and
    // conflating them means one timed-out Tor request silently reclassifies
    // a whole 20-job batch as forgotten and resubmits it.
    $covered = array();
    if ( empty( $job_ids ) ) {
        return array();
    }
    // Test hook: return a list of status dicts to short-circuit the SPN poll.
    $mock = apply_filters( 'onionpress_wayback_poll_parallel_mock', null, $job_ids );
    if ( is_array( $mock ) ) {
        // A mock stands in for a reachable SPN, so everything asked about
        // counts as covered — otherwise a mock returning [] would mean
        // "poll failed" and no test could exercise the forgotten path.
        // The second filter exists so a test can express the opposite:
        // batches that never came back, which is the case the coverage
        // tracking was added for and which the mock alone cannot produce.
        $covered = array_fill_keys( $job_ids, true );
        $covered_mock = apply_filters(
            'onionpress_wayback_poll_covered_mock', null, $job_ids );
        if ( is_array( $covered_mock ) ) {
            $covered = array_fill_keys( $covered_mock, true );
        }
        return $mock;
    }
    $auth = onionpress_wayback_auth_header();
    $headers = array( 'Accept: application/json' );
    if ( $auth ) {
        $headers[] = 'Authorization: ' . $auth;
    }
    // Same chunking discipline as submit_parallel: cap concurrent handles
    // at OP_WB_CONCURRENT_MAX through the shared Tor SOCKS.
    $all = array();
    $id_chunks = array_chunk( $job_ids, OP_WB_STATUS_BATCH_MAX );
    foreach ( array_chunk( $id_chunks, OP_WB_CONCURRENT_MAX ) as $parallel_group ) {
        $setups = array();
        foreach ( $parallel_group as $i => $chunk ) {
            $setups[ $i ] = function ( $ch ) use ( $chunk, $headers ) {
                curl_setopt_array( $ch, array(
                    CURLOPT_URL        => 'https://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/save/status',
                    CURLOPT_POST       => true,
                    CURLOPT_POSTFIELDS => http_build_query( array( 'job_ids' => implode( ',', $chunk ) ) ),
                    CURLOPT_TIMEOUT    => OP_WB_REQUEST_TIMEOUT_SEC,
                    CURLOPT_HTTPHEADER => $headers,
                ) );
            };
        }
        $raw = onionpress_wayback_curl_multi( $setups );
        foreach ( $raw as $i => $r ) {
            if ( $r['code'] !== 200 || empty( $r['body'] ) ) continue;
            $data = @json_decode( $r['body'], true );
            // Must be a JSON *list* of status dicts. A 200 carrying an
            // object — {"message":"..."} for a rate limit, an auth error,
            // a maintenance notice — decodes to an array too, and counting
            // that as an answer would mark all 20 job_ids in the batch
            // covered on the strength of a response containing no statuses
            // at all. The forgotten loop would then read it as "SPN
            // answered and mentioned none of them" and resubmit the batch:
            // the same over-eager clearing the coverage tracking exists to
            // prevent, just through a narrower door.
            if ( ! is_array( $data ) || ! array_is_list( $data ) ) continue;
            // This batch answered, so every job_id in it is now accounted
            // for: any of them missing from $data really is one SPN has
            // forgotten, not one we failed to ask about. Keyed by $i,
            // which indexes $parallel_group the same way $setups does.
            foreach ( $parallel_group[ $i ] as $jid ) {
                $covered[ $jid ] = true;
            }
            foreach ( $data as $item ) {
                if ( is_array( $item ) ) $all[] = $item;
            }
        }
    }
    return $all;
}

// ───────────────── finalize: write outcomes into storage ────────────

/**
 * Record a successful capture. $data is one element from the
 * /save/status batch response. Generic over storage: the caller
 * supplies read/write callables so this works for both postmeta and
 * wp_options (home + feed).
 */
function onionpress_wayback_finalize_success( callable $write, $url, array $data ) {
    $state = array(
        'archived_at'     => time(),
        'snapshot_ts'     => (string) ( $data['timestamp'] ?? '' ),
        'original_url'    => (string) ( $data['original_url'] ?? '' ),
        'duration_sec'    => (float) ( $data['duration_sec'] ?? 0 ),
        'resources_count' => is_array( $data['resources'] ?? null ) ? count( $data['resources'] ) : 0,
        'outlinks_count'  => is_array( $data['outlinks']  ?? null ) ? count( $data['outlinks']  ) : 0,
    );
    // Only store original_url if it actually differs from what we submitted,
    // to avoid 4kB of duplicated URLs in postmeta.
    if ( $state['original_url'] === $url ) {
        unset( $state['original_url'] );
    }
    $write( $state );
}

/**
 * Record a failed capture, and count it. $state is the record's state as
 * just read, which is where the running count lives — the caller has it in
 * hand and re-reading through the write callable would be a second query
 * for a number already sitting on the stack.
 */
function onionpress_wayback_finalize_error( callable $write, $ext, array $state = array() ) {
    $write( array(
        'last_error_ext' => (string) $ext,
        'last_error_at'  => time(),
        'error_count'    => (int) ( $state['error_count'] ?? 0 ) + 1,
    ) );
}

/**
 * How long a record must cool off after its Nth consecutive failure.
 *
 * The exponent is clamped as well as the result. min() on the product alone
 * would still evaluate OP_WB_RETRY_BASE_SEC * 2^N first, and a record that
 * somehow accumulated a few thousand failures would overflow to INF before
 * the clamp ever saw it — (int) INF is undefined behaviour across PHP
 * versions and has been 0 on some, which would turn the ceiling into no
 * back-off at all on exactly the record that needs it most.
 */
function onionpress_wayback_retry_delay( $error_count ) {
    $n = max( 1, (int) $error_count );
    return (int) min(
        OP_WB_RETRY_MAX_SEC,
        OP_WB_RETRY_BASE_SEC * pow( 2, min( $n - 1, 30 ) )
    );
}

/**
 * May this record be submitted again yet? True for anything that has never
 * failed, so the ordinary path pays one array lookup.
 */
function onionpress_wayback_retry_ready( array $state, $now ) {
    $at = (int) ( $state['last_error_at'] ?? 0 );
    if ( $at <= 0 ) {
        return true;
    }
    $delay = onionpress_wayback_retry_delay( $state['error_count'] ?? 1 );
    // A last_error_at FAR in the future means the clock moved backwards — a
    // laptop resuming with a corrected time, most often. Treat that as ready,
    // because a back-off lasting until the clock catches up is
    // indistinguishable from the record being abandoned.
    //
    // "Far" is doing real work here. $now is whatever the caller had, and the
    // sweep captures it once at the top of an iteration that then runs for up
    // to the whole budget — so a failure recorded by the poll is legitimately
    // stamped LATER than the $now the submit phase is comparing against, with
    // no clock skew involved at all. Treating any future value as ready let a
    // URL that had just failed be resubmitted by the very iteration that
    // failed it: observed live, the feed came back from one sweep with a new
    // job_id and error_count already at 2. Only a jump bigger than the
    // back-off itself is skew rather than ordering.
    if ( $at - $now > $delay ) {
        return true;
    }
    return ( $now - $at ) >= $delay;
}

// ────────────────────── state read/write helpers ────────────────────

function onionpress_wayback_post_read( $post_id ) {
    return array(
        'archived_at' => (int)    get_post_meta( $post_id, OP_WB_META_ARCHIVED_AT, true ),
        'job_id'      => (string) get_post_meta( $post_id, OP_WB_META_JOB_ID,       true ),
        'submitted_at'=> (int)    get_post_meta( $post_id, OP_WB_META_SUBMITTED_AT, true ),
        // Read by the retry gate. A record that skipped these would be
        // treated as never having failed, which is the storm this fixes.
        'last_error_at'=> (int)   get_post_meta( $post_id, OP_WB_META_LAST_ERROR_AT, true ),
        'error_count' => (int)    get_post_meta( $post_id, OP_WB_META_ERROR_COUNT,   true ),
    );
}

/**
 * Write a patch of fields to a post's wayback meta. Only keys present in
 * $patch are touched; the rest are preserved. An empty string deletes.
 *
 * Returns true, like every other record write. Only moss_write() can refuse,
 * but the sweep calls these through a 'write' callable without knowing which
 * storage is behind it, so they have to agree on what a write reports.
 */
function onionpress_wayback_post_write( $post_id, array $patch ) {
    $mapping = array(
        'archived_at'     => OP_WB_META_ARCHIVED_AT,
        'snapshot_ts'     => OP_WB_META_SNAPSHOT_TS,
        'job_id'          => OP_WB_META_JOB_ID,
        'submitted_at'    => OP_WB_META_SUBMITTED_AT,
        'original_url'    => OP_WB_META_ORIGINAL_URL,
        'duration_sec'    => OP_WB_META_DURATION_SEC,
        'resources_count' => OP_WB_META_RESOURCES_COUNT,
        'outlinks_count'  => OP_WB_META_OUTLINKS_COUNT,
        'last_error_ext'  => OP_WB_META_LAST_ERROR_EXT,
        'last_error_at'   => OP_WB_META_LAST_ERROR_AT,
        'error_count'     => OP_WB_META_ERROR_COUNT,
    );
    foreach ( $patch as $key => $val ) {
        if ( ! isset( $mapping[ $key ] ) ) continue;
        if ( $val === '' || $val === 0 || $val === 0.0 ) {
            delete_post_meta( $post_id, $mapping[ $key ] );
        } else {
            update_post_meta( $post_id, $mapping[ $key ], $val );
        }
    }
    return true;
}

function onionpress_wayback_opt_read( $option_key ) {
    $raw = get_option( $option_key, array() );
    return is_array( $raw ) ? $raw : array();
}

function onionpress_wayback_opt_write( $option_key, array $patch ) {
    $raw = onionpress_wayback_opt_read( $option_key );
    foreach ( $patch as $k => $v ) {
        if ( $v === '' || $v === 0 || $v === 0.0 ) {
            unset( $raw[ $k ] );
        } else {
            $raw[ $k ] = $v;
        }
    }
    update_option( $option_key, $raw, false /* no autoload */ );
    return true;
}

// ─────────────────────────── moss generation ────────────────────────

/**
 * Where the live generation is symlinked. The moss receiver owns this path;
 * read its constant when the receiver is loaded rather than keeping a second
 * copy of the same string in two mu-plugins that must agree.
 */
function onionpress_wayback_moss_current_path() {
    return defined( 'ONIONPRESS_MOSS_CURRENT' )
        ? ONIONPRESS_MOSS_CURRENT
        : '/var/www/html/site/current';
}

/**
 * The live moss generation as array( 'id' => …, 'dir' => … ), or null when
 * no generation is serving (a plain WordPress site, or a first run before
 * moss has ever committed).
 *
 * realpath() is load-bearing, though not for the reason it looks like. The
 * walk below is indifferent to it — PHP descends a symlinked start path
 * perfectly well, unlike find(1), which needs -L. What resolving buys is the
 * ID: `site/current` is a symlink whose own basename is the constant
 * "current". Read the id off the unresolved path and it never changes, so
 * every publish matches the generation already recorded, the per-page map
 * never resets, and new content is silently never submitted — the generation
 * id is the entire mechanism by which a publish re-archives.
 */
function onionpress_wayback_moss_generation() {
    $real = realpath( onionpress_wayback_moss_current_path() );
    if ( $real === false || ! is_dir( $real ) ) {
        return null;
    }
    return array( 'id' => basename( $real ), 'dir' => $real );
}

function onionpress_wayback_moss_is_owner() {
    return onionpress_wayback_moss_generation() !== null;
}

/**
 * Does the live moss generation publish THIS subsite's URLs?
 *
 * Everything moss-shaped in this plugin is container-global — one
 * `site/current` symlink, one /var/lib/onionpress/onion_address — but
 * OP_WB_OPT_MOSS is a per-subsite option and the sweep visits every subsite
 * in the network. So each subsite enumerated the same generation, kept its
 * own map of the same paths, and submitted the same URLs under its own
 * job_ids. Measured on this four-blog network: 4 subsites x 3 pages = 12 SPN
 * submissions per drain cycle for 3 distinct URLs, plus four of /rss.xml —
 * against an account whose concurrent slots are counted in single digits, and
 * precisely the cost OP_WB_RETRY_BASE_SEC exists to keep off other people's
 * sites.
 *
 * moss serves at the onion ROOT, so its pages are the main site's URLs; a
 * subsite lives under a path prefix and owns none of them. function_exists()
 * because is_main_site() ships with multisite only, and this plugin also runs
 * on single-site installs — where the one site is the main one by definition.
 */
function onionpress_wayback_moss_serves_this_site() {
    if ( function_exists( 'is_main_site' ) && ! is_main_site() ) {
        return false;
    }
    return onionpress_wayback_moss_is_owner();
}

/**
 * Every page path the live generation serves, as absolute site paths ('/',
 * '/about/', …), sorted and capped at OP_WB_MOSS_MAX_PAGES.
 *
 * sitemap.xml first, because moss writes it from its own build manifest and
 * it is therefore the authoritative list. Only the PATH of each <loc> is
 * used: the host in there comes from moss's persisted site_url, which is
 * whatever was configured at build time and can be stale or even
 * http://localhost — archiving that would submit URLs nobody can fetch.
 *
 * The walk is the fallback because moss gates sitemap.xml (and rss.xml) on
 * its site_url being "deployed", so a site built before its onion name was
 * registered ships neither. Without the fallback those sites would archive
 * nothing at all and the reason would be invisible.
 *
 * Memoized per REQUEST, keyed on the generation. One sweep iteration asks
 * three times per subsite — the has-work probe, the poll's record list and
 * the submit's — so a four-blog network ran twelve full enumerations every
 * 30 seconds. With a sitemap that is a wasted parse; on the fallback walk it
 * is up to OP_WB_MOSS_SCAN_MAX stats each, ~480k stat calls a minute, and the
 * two notices below fired on every one of them (~34,000 identical log lines a
 * day). Keying on the generation is what keeps a publish visible: a new
 * generation is a new key, so the memo cannot serve yesterday's page list.
 * Within one generation the tree is immutable — moss builds each publish into
 * its own directory and moves the symlink — and the daemon recycles every
 * OP_WB_LOOP_MAX_SEC anyway, so nothing here outlives a request by design.
 */
function onionpress_wayback_moss_paths( array $gen ) {
    static $memo = array();
    $memo_key = (string) $gen['id'] . "\0" . (string) $gen['dir'];
    if ( isset( $memo[ $memo_key ] ) ) {
        return $memo[ $memo_key ];
    }

    $paths   = array();
    $sitemap = $gen['dir'] . '/sitemap.xml';

    if ( is_readable( $sitemap ) && function_exists( 'simplexml_load_file' ) ) {
        $prev = libxml_use_internal_errors( true );
        $xml  = simplexml_load_file( $sitemap );
        libxml_clear_errors();
        libxml_use_internal_errors( $prev );
        if ( $xml !== false && isset( $xml->url ) ) {
            foreach ( $xml->url as $u ) {
                $path = wp_parse_url( (string) $u->loc, PHP_URL_PATH );
                if ( is_string( $path ) && $path !== '' ) {
                    $paths[ $path ] = true;
                }
            }
        } elseif ( $xml !== false && $xml->getName() === 'sitemapindex' ) {
            // Past 50k URLs moss emits an INDEX of sitemaps instead, whose
            // children are <sitemap> not <url>. We deliberately do not follow
            // it: a site that large is two orders of magnitude past
            // OP_WB_MOSS_MAX_PAGES, so the pages the index would add are ones
            // this plugin refuses to track anyway. The walk below then
            // enumerates what it can. Say so rather than letting an
            // unrecognised sitemap look like an absent one — the fallback is
            // much more expensive, and an operator seeing a walk on a site
            // that plainly ships a sitemap would have no way to know why.
            onionpress_wayback_log(
                'moss: sitemap.xml is a <sitemapindex> (a site past ~50k URLs); '
                . 'not followed — falling back to the directory walk, and only '
                . 'the first ' . OP_WB_MOSS_MAX_PAGES . ' pages are tracked' );
        }
    }

    // Fallback, only when the sitemap named nothing. A generation that ships
    // one is authoritative, and the walk is the expensive path.
    //
    // A page is a directory carrying index.html. Skip moss's asset mount — it
    // holds the hashed css/js and the generated OG images, which on a real
    // site outnumber the pages by an order of magnitude and can never contain
    // one.
    //
    // Every part of this can throw. RecursiveDirectoryIterator reports an
    // unreadable directory as an UnexpectedValueException — from its own
    // constructor for the root, and from getChildren() for a subdirectory —
    // and there was nothing here to catch either. One directory the web user
    // could not open (reproduced in-container as www-data: "Failed to open
    // directory: Permission denied") therefore propagated out of
    // sitewide_has_work(), out of sweep_iteration(), and killed the sweep as
    // a PHP fatal on every invocation until someone fixed the permission —
    // with no plugin log line to say what had happened. CATCH_GET_CHILD skips
    // the unreadable subtree; the try/catch is for the root and for anything
    // else the iterator decides to raise. Both keep whatever was enumerated
    // before the failure, because a partial page list is worth far more than
    // a dead sweep.
    if ( ! $paths ) {
        $scanned    = 0;
        $unreadable = array();
        try {
            $dir    = new RecursiveDirectoryIterator( $gen['dir'], FilesystemIterator::SKIP_DOTS );
            $filter = new RecursiveCallbackFilterIterator( $dir, function ( $file ) use ( &$unreadable ) {
                if ( $file->getFilename() === '_moss' ) {
                    return false;
                }
                // CATCH_GET_CHILD turns an unreadable subdirectory from a
                // fatal into a SILENT skip, and silent is its own failure:
                // every page under it stops being archived and nothing says
                // why. One is_readable() per directory — not per file — buys
                // the log line below. CATCH_GET_CHILD stays as the backstop
                // for the gap between this check and the descent.
                if ( $file->isDir() && ! $file->isReadable() ) {
                    $unreadable[] = $file->getPathname();
                    return false;
                }
                return true;
            } );
            $walk = new RecursiveIteratorIterator(
                $filter,
                RecursiveIteratorIterator::LEAVES_ONLY,
                RecursiveIteratorIterator::CATCH_GET_CHILD
            );
            foreach ( $walk as $file ) {
                if ( ++$scanned > OP_WB_MOSS_SCAN_MAX ) {
                    onionpress_wayback_log( sprintf(
                        'moss: directory walk hit its %d-file ceiling; enumerated %d page(s) before stopping',
                        OP_WB_MOSS_SCAN_MAX, count( $paths )
                    ) );
                    break;
                }
                if ( $file->getFilename() !== 'index.html' ) {
                    continue;
                }
                $rel = substr( $file->getPath(), strlen( $gen['dir'] ) );
                $rel = str_replace( '\\', '/', $rel );
                $paths[ $rel === '' ? '/' : rtrim( $rel, '/' ) . '/' ] = true;
            }
        } catch ( Exception $e ) {
            onionpress_wayback_log( sprintf(
                'moss: directory walk of %s failed after %d file(s) (%s); '
                . 'archiving the %d page(s) enumerated so far',
                $gen['dir'], $scanned, $e->getMessage(), count( $paths )
            ) );
        }
        if ( $unreadable ) {
            onionpress_wayback_log( sprintf(
                'moss: %d directory/ies could not be opened and were skipped, '
                . 'so any page under them is NOT archived: %s',
                count( $unreadable ), implode( ', ', array_slice( $unreadable, 0, 5 ) )
                    . ( count( $unreadable ) > 5 ? ', …' : '' )
            ) );
        }
    }

    $out = array_keys( $paths );
    sort( $out );
    // Storage bound, not a throughput one — see OP_WB_MOSS_MAX_PAGES. Applied
    // here rather than in moss_records() so it sits inside the memo: it was
    // the other log line firing three times per subsite per iteration.
    $total = count( $out );
    if ( $total > OP_WB_MOSS_MAX_PAGES ) {
        $out = array_slice( $out, 0, OP_WB_MOSS_MAX_PAGES );
        onionpress_wayback_log( sprintf(
            'moss: %d page(s) found, tracking the first %d (OP_WB_MOSS_MAX_PAGES); %d not archived',
            $total, OP_WB_MOSS_MAX_PAGES, $total - OP_WB_MOSS_MAX_PAGES
        ) );
    }
    $memo[ $memo_key ] = $out;
    return $out;
}

/**
 * Patch one moss page's state inside the single OP_WB_OPT_MOSS row. Returns
 * false when the write was refused, so a caller that is counting successful
 * submissions can tell one from a write that did nothing.
 *
 * Refuses to write into a generation that is no longer the live one. A job
 * submitted against the previous generation can land here after a publish,
 * and finalize_success writes archived_at and clears job_id in two separate
 * calls — without this guard a late write resurrects a row for a page the new
 * generation may not even serve, and nothing would ever retire it.
 *
 * Adopting the new generation is the other half of the same comparison, and
 * it lives here rather than in the record builder because THIS is the write
 * path. It used to happen in moss_records(), which meant merely ASKING what
 * work existed reset the option and logged a publish — sitewide_has_work(),
 * a yes/no query, did both three times per subsite per iteration. Doing it on
 * the first real write instead makes every read path pure, and loses nothing:
 * a record's read (see moss_records) already reports a page as unarchived
 * whenever the stored map describes a different generation, so a publish puts
 * every page back in the queue whether or not anything has been written yet.
 */
function onionpress_wayback_moss_write( $gen_id, $path, array $patch ) {
    $state = onionpress_wayback_opt_read( OP_WB_OPT_MOSS );
    if ( (string) ( $state['generation'] ?? '' ) !== (string) $gen_id ) {
        // The map describes some other generation. Two cases, opposite
        // answers, and only the live generation id separates them.
        $live = onionpress_wayback_moss_generation();
        if ( $live === null || (string) $live['id'] !== (string) $gen_id ) {
            return false; // a late write from a generation that has been retired
        }
        // A publish has landed and this is the first write against it: adopt
        // it, retiring every row of the old generation in one write. That
        // replacement is the entire mechanism by which a publish re-archives.
        onionpress_wayback_log( sprintf(
            'moss: generation %s is live (was %s) — re-archiving its pages',
            $gen_id, ( $state['generation'] ?? '(none)' )
        ) );
        $state = array( 'generation' => (string) $gen_id, 'urls' => array() );
    }
    $urls = isset( $state['urls'] ) && is_array( $state['urls'] ) ? $state['urls'] : array();
    $row  = isset( $urls[ $path ] ) && is_array( $urls[ $path ] ) ? $urls[ $path ] : array();
    foreach ( $patch as $k => $v ) {
        if ( $v === '' || $v === 0 || $v === 0.0 ) {
            unset( $row[ $k ] );
        } else {
            $row[ $k ] = $v;
        }
    }
    if ( $row ) {
        $urls[ $path ] = $row;
    } else {
        unset( $urls[ $path ] );
    }
    $state['urls'] = $urls;
    update_option( OP_WB_OPT_MOSS, $state, false /* no autoload */ );
    return true;
}

/**
 * The live generation's pages as work records, in the same shape posts and
 * home/feed already use, so poll / CDX rescue / finalize / back-off all
 * apply to them unchanged.
 *
 * $covered lets the caller pass the URLs home/feed already claim. moss's
 * sitemap lists '/' too, so without it the site root would be submitted
 * twice per sweep against an SPN account whose concurrent slots are counted
 * in single digits.
 *
 * Reads nothing it does not need and writes nothing at all: building the work
 * list is a question, and the answer to a question should not depend on
 * having been asked. The generation reset this used to perform lives in
 * moss_write() now — see the argument there.
 */
function onionpress_wayback_moss_records( array $covered = array() ) {
    $onion = onionpress_wayback_onion_addr();
    if ( $onion === '' ) {
        return array();
    }
    $gen = onionpress_wayback_moss_generation();
    if ( $gen === null ) {
        return array();
    }

    $records = array();
    foreach ( onionpress_wayback_moss_paths( $gen ) as $path ) {
        $url = 'http://' . $onion . $path;
        if ( isset( $covered[ $url ] ) ) {
            continue;
        }
        $gen_id = $gen['id'];
        $records[] = array(
            'key'   => 'moss:' . $path,
            'url'   => $url,
            // A row belongs to the generation the map was written for. When a
            // publish has replaced it, every page of the NEW generation is
            // unarchived by definition — including one whose path the old
            // generation happened to serve too — so the stale row must not be
            // reported as this page's state. That is what puts every page back
            // in the queue on a publish, and it is why nothing has to reset
            // the option eagerly to make it happen.
            'read'  => function () use ( $gen_id, $path ) {
                $s = onionpress_wayback_opt_read( OP_WB_OPT_MOSS );
                if ( (string) ( $s['generation'] ?? '' ) !== (string) $gen_id ) {
                    return array();
                }
                return isset( $s['urls'][ $path ] ) && is_array( $s['urls'][ $path ] )
                    ? $s['urls'][ $path ]
                    : array();
            },
            'write' => function ( $patch ) use ( $gen_id, $path ) {
                return onionpress_wayback_moss_write( $gen_id, $path, $patch );
            },
        );
    }
    return $records;
}

// ───────────────────── work queue (posts + home/feed) ───────────────

/**
 * Unified record for one URL under management. Each is an associative
 * array with at minimum 'url', 'read' callable (returns current state),
 * 'write' callable (applies a patch). Used by the sweep so posts and
 * home/feed go through the same code path.
 */
function onionpress_wayback_posts_needing_submit( $limit ) {
    // Over-fetch: the caller drops records still cooling off from a failed
    // capture, and that back-off is per record and exponential in its own
    // error_count, so it cannot be expressed here. Asking for exactly the
    // budget lets the newest N posts — all of them cooling — hide every
    // older post that is ready, and the queue stalls with work in it.
    $limit      = max( 1, (int) $limit );
    $candidates = min( OP_WB_SUBMIT_CANDIDATE_MAX, $limit * OP_WB_SUBMIT_CANDIDATE_FACTOR );
    $posts = get_posts( array(
        'post_status'      => 'publish',
        'post_type'        => array( 'post', 'page' ),
        'numberposts'      => $candidates,
        'orderby'          => 'date',
        'order'            => 'DESC',
        'meta_query'       => array(
            'relation' => 'AND',
            array( 'key' => OP_WB_META_ARCHIVED_AT, 'compare' => 'NOT EXISTS' ),
            array( 'key' => OP_WB_META_JOB_ID,      'compare' => 'NOT EXISTS' ),
        ),
        'fields'           => 'ids',
        'suppress_filters' => false,
    ) );
    $records = array();
    foreach ( $posts as $post_id ) {
        $url = onionpress_wayback_post_url( $post_id );
        if ( empty( $url ) ) continue;
        $records[] = array(
            'key'   => 'post:' . $post_id,
            'url'   => $url,
            'read'  => function() use ( $post_id ) {
                return onionpress_wayback_post_read( $post_id );
            },
            'write' => function( $patch ) use ( $post_id ) {
                return onionpress_wayback_post_write( $post_id, $patch );
            },
        );
    }
    return $records;
}

function onionpress_wayback_posts_with_in_flight() {
    $posts = get_posts( array(
        'post_status'      => 'publish',
        'post_type'        => array( 'post', 'page' ),
        'numberposts'      => -1,
        'meta_query'       => array(
            array( 'key' => OP_WB_META_JOB_ID, 'compare' => 'EXISTS' ),
        ),
        'fields'           => 'ids',
        'suppress_filters' => false,
    ) );
    $records = array();
    foreach ( $posts as $post_id ) {
        $job_id = (string) get_post_meta( $post_id, OP_WB_META_JOB_ID, true );
        if ( empty( $job_id ) ) continue;
        $url = onionpress_wayback_post_url( $post_id );
        $records[ $job_id ] = array(
            'key'          => 'post:' . $post_id,
            'url'          => $url,
            'submitted_at' => (int) get_post_meta( $post_id, OP_WB_META_SUBMITTED_AT, true ),
            'error_count'  => (int) get_post_meta( $post_id, OP_WB_META_ERROR_COUNT, true ),
            'write'        => function( $patch ) use ( $post_id ) {
                return onionpress_wayback_post_write( $post_id, $patch );
            },
        );
    }
    return $records;
}

/**
 * Home + feed as work records, matching the shape used for posts.
 * Returns a mixed list: some awaiting submission, some in-flight. The
 * sweep checks each record's state to decide what to do.
 */
function onionpress_wayback_sitewide_records() {
    $records = array();
    $home = onionpress_wayback_home_url_full();
    $feed = onionpress_wayback_feed_url_full();
    $items = array();
    if ( $home ) $items[ OP_WB_OPT_HOME ] = $home;
    if ( $feed ) $items[ OP_WB_OPT_FEED ] = $feed;
    $covered = array();
    foreach ( $items as $opt_key => $url ) {
        $covered[ $url ] = true;
        $records[] = array(
            'key'   => 'opt:' . $opt_key,
            'url'   => $url,
            // A state that names a DIFFERENT url describes a different page,
            // so it cannot answer for this one — report unarchived and let it
            // be submitted. Both of these urls move: feed_url_full() switches
            // from WordPress's /feed/ route to moss's /rss.xml the moment a
            // generation ships one, and home_url_full() follows the onion
            // address.
            //
            // Without this the record only ever recorded THAT it archived,
            // never WHAT, so the first success pinned it forever. Confirmed
            // against archive.org's index for the live test site: /feed/ was
            // captured 2026-08-13 (200, application/rss+xml) and /rss.xml has
            // no capture at any time — the feed record had been marked done
            // by the old url and the new one was never once submitted.
            //
            // Same shape as moss_write()'s generation guard: state keyed to
            // what it describes, and a change to that key retires it.
            'read'  => function() use ( $opt_key, $url ) {
                $state = onionpress_wayback_opt_read( $opt_key );
                $stored = (string) ( $state['url'] ?? '' );
                // Absent on rows written before this field existed. Treating
                // those as a mismatch would re-submit every site's home page
                // once on upgrade, for no gain — the url is only known to be
                // stale when it is present and differs.
                if ( $stored !== '' && $stored !== $url ) {
                    return array();
                }
                return $state;
            },
            'write' => function( $patch ) use ( $opt_key, $url ) {
                // Stamped on every write, so a row becomes self-describing at
                // the first thing that happens to it — submission included,
                // which is what keeps an in-flight job from being credited to
                // a url that changed while it was out.
                $patch['url'] = $url;
                return onionpress_wayback_opt_write( $opt_key, $patch );
            },
        );
    }
    // Home and feed are the only two URLs WordPress can name generically. When
    // moss is serving, they are 2 of N — every other page it publishes was
    // outside the queue entirely, which is why a site could report
    // "archived=5/5" while none of its actual pages had ever been submitted.
    //
    // On the main site only. The generation is one container-global tree of
    // URLs rooted at the onion address, but this function runs once per
    // subsite inside the sweep's network loop, and OP_WB_OPT_MOSS is
    // per-subsite — so every subsite kept its own map and its own job_ids for
    // the same pages and submitted all of them. See moss_serves_this_site().
    if ( ! onionpress_wayback_moss_serves_this_site() ) {
        return $records;
    }
    return array_merge( $records, onionpress_wayback_moss_records( $covered ) );
}

/**
 * Does any sitewide URL still need attention? A record needs work unless it
 * has an archived_at and no job_id in flight.
 *
 * Derived from the records rather than from named options so it cannot drift
 * from what the sweep would do — the multisite loop uses this to decide it
 * may skip a subsite entirely, and a stale answer there means content is
 * silently never archived.
 *
 * A pure query, which it had not always been: through moss_records() it used
 * to reset OP_WB_OPT_MOSS and log a publish, so a yes/no question rewrote an
 * option three times per subsite per iteration. Both moved to moss_write().
 */
function onionpress_wayback_sitewide_has_work() {
    foreach ( onionpress_wayback_sitewide_records() as $rec ) {
        $state = call_user_func( $rec['read'] );
        if ( empty( $state['archived_at'] ) || ! empty( $state['job_id'] ) ) {
            return true;
        }
    }
    return false;
}

// ──────────────────────────── sweep ─────────────────────────────────

/**
 * Entry point wired to wp-cron. Runs a continuous inner loop for up
 * to OP_WB_LOOP_MAX_SEC, so one cron invocation can drive many sweep
 * iterations. Exits early when:
 *   - queue fully drained (no posts with job_id=null, archived_at=null)
 *   - a gate tells us to back off for longer than remaining budget
 *
 * Next cron tick restarts us. This is the "daemon with cron watchdog"
 * pattern — cron only has to fire occasionally to keep the process
 * alive; all the real pacing is inside the inner loop.
 */
function onionpress_wayback_sweep() {
    // Single-process mutex. The lock value is "<token>:<last_heartbeat_ts>".
    // - token: unique per invocation; lets each daemon verify ownership
    //   on each iteration and gracefully exit if another has taken over.
    // - last_heartbeat_ts: updated every iteration; a stale ts means
    //   the prior process is dead and we take over.
    $lock_key = 'op_wayback_sweep_lock';
    $raw      = (string) get_option( $lock_key, '' );
    $now      = time();
    if ( $raw !== '' && strpos( $raw, ':' ) !== false ) {
        list( , $lock_ts_str ) = explode( ':', $raw, 2 );
        $lock_ts = (int) $lock_ts_str;
        if ( $lock_ts > 0 && ( $now - $lock_ts ) < OP_WB_LOOP_LOCK_STALE_SEC ) {
            return; // another invocation is still alive
        }
    } elseif ( $raw !== '' ) {
        // Legacy lock value (plain ts). Treat like the new format.
        $lock_ts = (int) $raw;
        if ( $lock_ts > 0 && ( $now - $lock_ts ) < OP_WB_LOOP_LOCK_STALE_SEC ) {
            return;
        }
    }

    // Claim the lock with our own unique token.
    $token = wp_generate_password( 16, false, false );
    update_option( $lock_key, $token . ':' . $now, false );

    @set_time_limit( 0 );
    @ignore_user_abort( true );

    try {
        onionpress_wayback_sweep_loop( $token );
    } finally {
        // Only clear the lock if it's still ours — otherwise another
        // daemon has taken over and needs its lock preserved.
        $cur = (string) get_option( $lock_key, '' );
        if ( strpos( $cur, $token . ':' ) === 0 ) {
            delete_option( $lock_key );
        }
    }
}

/**
 * Sum queue totals across every subsite in the network. Returns an
 * array with 'archived', 'in_flight', 'remaining', and 'total' post
 * counts (counting publish posts + pages only).
 */
function onionpress_wayback_queue_totals() {
    $out = array( 'archived' => 0, 'in_flight' => 0, 'remaining' => 0, 'total' => 0 );
    $sites = function_exists( 'get_sites' ) ? get_sites() : array();
    if ( empty( $sites ) ) {
        $sites = array( (object) array( 'blog_id' => get_current_blog_id() ) );
    }
    foreach ( $sites as $site ) {
        $bid = (int) $site->blog_id;
        if ( function_exists( 'switch_to_blog' ) ) switch_to_blog( $bid );
        try {
            global $wpdb;
            $prefix = $wpdb->get_blog_prefix( $bid );
            $posts_table = $prefix . 'posts';
            $meta_table  = $prefix . 'postmeta';
            $total     = (int) $wpdb->get_var( "SELECT COUNT(*) FROM {$posts_table} WHERE post_status='publish' AND post_type IN ('post','page')" );
            $archived  = (int) $wpdb->get_var( $wpdb->prepare(
                "SELECT COUNT(DISTINCT m.post_id) FROM {$meta_table} m JOIN {$posts_table} p ON p.ID=m.post_id "
                . "WHERE m.meta_key=%s AND p.post_status='publish' AND p.post_type IN ('post','page')",
                OP_WB_META_ARCHIVED_AT
            ) );
            $in_flight = (int) $wpdb->get_var( $wpdb->prepare(
                "SELECT COUNT(DISTINCT m.post_id) FROM {$meta_table} m JOIN {$posts_table} p ON p.ID=m.post_id "
                . "WHERE m.meta_key=%s AND p.post_status='publish' AND p.post_type IN ('post','page')",
                OP_WB_META_JOB_ID
            ) );
            $out['total']     += $total;
            $out['archived']  += $archived;
            $out['in_flight'] += $in_flight;
        } finally {
            if ( function_exists( 'restore_current_blog' ) ) restore_current_blog();
        }
    }
    $out['remaining'] = max( 0, $out['total'] - $out['archived'] - $out['in_flight'] );
    return $out;
}

function onionpress_wayback_sweep_loop( $token ) {
    $lock_key     = 'op_wayback_sweep_lock';
    $loop_start   = microtime( true );
    // Every key here is summed from sweep_iteration()'s return, by name —
    // a key present in one and not the other silently stays 0, so the two
    // lists have to be kept in step. 'lost' is here because a sustained
    // poll outage is otherwise invisible in the summary lines an operator
    // actually reads: each sweep prints its own poll-lost, but the drained
    // and recycling lines are where you go to ask why nothing happened.
    $totals       = array(
        'submitted' => 0, 'success' => 0, 'cdx' => 0,
        'error'     => 0, 'forgotten' => 0, 'lost' => 0,
    );
    $last_progress = $loop_start;

    // Refresh the lock's timestamp, but only while it is still ours —
    // stamping our token onto a successor's claim would evict a daemon
    // that legitimately took over. Returns false once we have lost it.
    //
    // MUST be called in the invoking blog's context, never between a
    // switch_to_blog() and its restore: options are per-subsite, so a
    // heartbeat inside a switch writes a second lock into the wrong
    // options table and leaves the real one to go stale.
    $heartbeat = function () use ( $lock_key, $token ) {
        $cur = (string) get_option( $lock_key, '' );
        if ( strpos( $cur, $token . ':' ) !== 0 ) {
            return false;
        }
        update_option( $lock_key, $token . ':' . time(), false );
        return true;
    };

    onionpress_wayback_log( 'Loop: starting daemon sweep (token=' . substr( $token, 0, 6 ) . ')' );
    $iter = 0;
    while ( true ) {
        $iter++;
        // Lock ownership check — if another daemon has claimed the
        // lock (possible if our heartbeat lapsed past stale threshold),
        // exit gracefully. Also heartbeats the ts if we still own it.
        if ( ! $heartbeat() ) {
            onionpress_wayback_log( 'Loop: lock taken by another daemon, exiting (token=' . substr( $token, 0, 6 ) . ', iter=' . $iter . ')' );
            return;
        }

        // Lifetime cap. Exiting here is not giving up: the queue is
        // unchanged and the next cron tick starts a fresh daemon that picks
        // up exactly where this one stopped — but with an empty option and
        // query cache, so it sees the database as it actually is. See
        // OP_WB_LOOP_MAX_SEC. Deliberately below the ownership check: it
        // releases the lock, so it must first be sure the lock is ours.
        $runtime = (int) ( microtime( true ) - $loop_start );
        // Filtered so a test can shrink it to 0 and assert the handoff.
        // Everything else in this plugin is reachable from a mock filter;
        // a hard constant would have made the one mechanism that stops the
        // daemon wedging the only mechanism with no test.
        $max_sec = (int) apply_filters( 'onionpress_wayback_loop_max_sec', OP_WB_LOOP_MAX_SEC );
        if ( $runtime >= $max_sec ) {
            onionpress_wayback_log( sprintf(
                'Loop: recycling after %dm%02ds (%d iterations, cap=%ds) — '
                . 'submitted=%d archived=%d cdx-hit=%d forgotten=%d errors=%d poll-lost=%d; '
                . 'cron restarts a fresh daemon',
                intdiv( $runtime, 60 ), $runtime % 60, $iter - 1, $max_sec,
                $totals['submitted'], $totals['success'], $totals['cdx'],
                $totals['forgotten'], $totals['error'], $totals['lost']
            ) );
            // Hand off immediately rather than making the queue wait out
            // LOCK_STALE_SEC for a daemon we know has exited cleanly.
            delete_option( $lock_key );
            // Start the successor now rather than leaving it to the 300s
            // recurring schedule and whenever a page view next happens to
            // drive pseudo-cron. Deliberately NOT kick_now(): that also
            // clears the back-off, and a back-off set by the sweep we are
            // recycling out of is a decision worth keeping.
            //
            // Gated on a non-zero lifetime because a cap of 0 is the test
            // seam: firing a real loopback there would start a genuine
            // half-hour production daemon out of a unit test, and at cap=0
            // every successor would recycle on its own first iteration —
            // a restart loop rather than a handoff.
            if ( $runtime > 0 ) {
                onionpress_wayback_schedule_sweep_now( 'recycle' );
            }
            return;
        }

        // Visit every subsite in the network. The daemon may have been
        // invoked from any site's cron; we need to do work on whichever
        // subsite actually has unarchived posts. Skip subsites whose
        // queue is fully drained — they exit the iteration for free.
        $sites = function_exists( 'get_sites' ) ? get_sites() : array();
        if ( empty( $sites ) ) {
            // Not multisite — fall back to single-site check.
            $sites = array( (object) array( 'blog_id' => get_current_blog_id() ) );
        }

        $any_work = false;
        foreach ( $sites as $site ) {
            // Heartbeat per SUBSITE, not just per loop iteration. Each
            // sweep_iteration() below gets its own OP_WB_SWEEP_BUDGET_SEC,
            // so on a four-blog network one pass through this foreach can
            // run four full budgets — far past OP_WB_LOOP_LOCK_STALE_SEC —
            // while the lock's timestamp sat untouched from the top of the
            // iteration. A second daemon would then read the lock as dead
            // and both would write the same records. Before switch_to_blog,
            // because the lock lives in the invoking blog's options table.
            $heartbeat();
            $bid = (int) $site->blog_id;
            if ( function_exists( 'switch_to_blog' ) ) {
                switch_to_blog( $bid );
            }
            try {
                $remaining = get_posts( array(
                    'post_status' => 'publish',
                    'post_type'   => array( 'post', 'page' ),
                    'numberposts' => 1,
                    'meta_query'  => array(
                        'relation' => 'AND',
                        array( 'key' => OP_WB_META_ARCHIVED_AT, 'compare' => 'NOT EXISTS' ),
                        array( 'key' => OP_WB_META_JOB_ID,      'compare' => 'NOT EXISTS' ),
                    ),
                    'fields' => 'ids',
                ) );
                $in_flight = get_posts( array(
                    'post_status' => 'publish',
                    'post_type'   => array( 'post', 'page' ),
                    'numberposts' => 1,
                    'meta_query'  => array(
                        array( 'key' => OP_WB_META_JOB_ID, 'compare' => 'EXISTS' ),
                    ),
                    'fields' => 'ids',
                ) );
                // Sitewide URLs are tracked in wp_options, not post meta, so the
                // post probes above miss them. A subsite with every post archived
                // can still need work if save_post invalidated its home/feed
                // capture (state cleared), a job is in flight, or a moss publish
                // put new pages in the queue. Ask the records themselves rather
                // than reading home/feed options directly — that is what keeps
                // this in step with what the sweep will actually try to do.
                $sitewide_work = onionpress_wayback_sitewide_has_work();
                if ( empty( $remaining ) && empty( $in_flight ) && ! $sitewide_work ) {
                    // No work on this subsite — skip.
                    continue;
                }
                $any_work = true;
                $stats = onionpress_wayback_sweep_iteration();
                if ( is_array( $stats ) ) {
                    foreach ( $totals as $k => $_ ) {
                        $totals[ $k ] += (int) ( $stats[ $k ] ?? 0 );
                    }
                }
            } finally {
                if ( function_exists( 'restore_current_blog' ) ) {
                    restore_current_blog();
                }
            }
        }

        if ( ! $any_work ) {
            $runtime = (int) ( microtime( true ) - $loop_start );
            onionpress_wayback_log( sprintf(
                'Loop: drained — %d iterations, %dm%02ds, submitted=%d archived=%d cdx-hit=%d forgotten=%d errors=%d poll-lost=%d',
                $iter, intdiv( $runtime, 60 ), $runtime % 60,
                $totals['submitted'], $totals['success'], $totals['cdx'],
                $totals['forgotten'], $totals['error'], $totals['lost']
            ) );
            return;
        }

        // Periodic progress line — every 2 min of wall clock so the log
        // shows captures/min reliably without waiting for drain.
        // Includes queue-wide archived/remaining totals across all
        // subsites for a complete snapshot in one line.
        if ( microtime( true ) - $last_progress >= 120 ) {
            $last_progress = microtime( true );
            $runtime = max( 1, (int) ( microtime( true ) - $loop_start ) );
            $caps    = $totals['success'] + $totals['cdx'];
            $cap_per_min = round( ( $caps * 60 ) / $runtime, 1 );
            $sub_per_min = round( ( $totals['submitted'] * 60 ) / $runtime, 1 );
            $qstats = onionpress_wayback_queue_totals();
            onionpress_wayback_log( sprintf(
                'Loop progress: %dm%02ds iter=%d | archived=%d/%d (%s/min captured, %s remaining) | submitted=%d (%s/min) cdx=%d forgotten=%d errors=%d poll-lost=%d',
                intdiv( $runtime, 60 ), $runtime % 60, $iter,
                $qstats['archived'], $qstats['total'],
                $cap_per_min, $qstats['remaining'],
                $totals['submitted'], $sub_per_min,
                $totals['cdx'], $totals['forgotten'], $totals['error'],
                $totals['lost']
            ) );
        }

        // Pace between iterations. Also respect any backoff set by a
        // gate inside the iteration (available=0, 429, etc.).
        $now = time();
        $backoff_until = (int) get_option( OP_WB_OPT_BACKOFF_UNTIL, 0 );
        $sleep = ( $backoff_until > $now )
            ? ( $backoff_until - $now )
            : OP_WB_LOOP_IDLE_SLEEP;
        // Heartbeat before sleeping, not just at the top of the iteration.
        // The sleep sits OUTSIDE the work the sweep budget bounds, and a
        // back-off runs to 120s (OP_WB_BACKOFF_UNREACHABLE) or 300s
        // (OP_WB_BACKOFF_BAD_CREDENTIALS) — so a budget-length iteration
        // followed by a back-off leaves the lock untouched for well past
        // OP_WB_LOOP_LOCK_STALE_SEC on the nominal path, not in some worst
        // case. A second daemon then declares this one dead and both write
        // the same records. This heartbeat is also why no back-off constant
        // may approach LOCK_STALE: the sleep below is one uninterrupted gap.
        $heartbeat();
        sleep( max( 5, $sleep ) );
    }
}

/**
 * One sweep iteration. Formerly the body of onionpress_wayback_sweep;
 * called in a loop from the new entry point above.
 */
function onionpress_wayback_sweep_iteration() {
    $now = time();

    // Global back-off gate — either we recently saw available=0, failed
    // a self-reachability check, or SPN status was unreachable.
    $backoff_until = (int) get_option( OP_WB_OPT_BACKOFF_UNTIL, 0 );
    if ( $backoff_until > $now ) {
        return;
    }

    $onion = onionpress_wayback_onion_addr();
    if ( empty( $onion ) ) {
        onionpress_wayback_log( 'Sweep skipped: onion address not ready' );
        return;
    }

    // Start the clock here, not after the status call. The budget's whole
    // job is to keep one iteration inside OP_WB_LOOP_LOCK_STALE_SEC, and the
    // status call below can sit on a full OP_WB_REQUEST_TIMEOUT_SEC — time
    // the lock heartbeat experiences and, until this moved, the budget did
    // not. It also makes the elapsed= in the log the iteration's real cost.
    $deadline = microtime( true ) + OP_WB_SWEEP_BUDGET_SEC;

    // Gate: do we have SPN slots? If the check itself fails (Tor
    // jitter, SPN briefly unreachable), assume we have slots and
    // proceed. The worst case is one wasted submit batch — better
    // than a 60-120s global backoff starving every sweep while we
    // wait for Tor to recover. If the account is genuinely at
    // capacity, the submits will return 429 and we'll back off then.
    //
    // $spn_status is what makes that optimism auditable: it names which of
    // the several ways this call can fail actually happened, so the guessed
    // available=40 below is never again indistinguishable in the log from a
    // reading. The submit gate reads it too — two of those causes are ones
    // proceeding cannot survive; see the branch there for the argument.
    $spn_status = 'ok';
    $user = onionpress_wayback_user_status( $spn_status );
    $available = ( $user === null )
        ? OP_WB_SUBMIT_BATCH_MAX // optimistic default
        // The ?? is now only reachable through the test mock: a real body
        // without an `available` key comes back as null/'malformed', which is
        // the whole point of that branch. Kept so a mock returning a bare
        // array is a zero rather than a PHP warning.
        : (int) ( $user['available'] ?? 0 );
    if ( $user !== null && $available <= 0 ) {
        // time() rather than $now for the same reason as the unreachable
        // back-off below: $now predates the status call this branch reacts
        // to, and a pause dated before the call that prompted it is not a
        // pause — on a cold descriptor the call alone can outlast it.
        update_option( OP_WB_OPT_BACKOFF_UNTIL, time() + OP_WB_BACKOFF_NO_SLOTS, false );
        onionpress_wayback_log( 'Sweep paused: available=0 processing='
            . ( $user['processing'] ?? '?' ) . ', backing off ' . OP_WB_BACKOFF_NO_SLOTS . 's' );
        return;
    }

    // ---- Step A: poll all outstanding job_ids in batches ----
    $in_flight = onionpress_wayback_posts_with_in_flight();
    // Add home + feed in-flight jobs
    foreach ( onionpress_wayback_sitewide_records() as $rec ) {
        $state = $rec['read']();
        // Poll on job_id alone, exactly as posts_with_in_flight() does.
        // Requiring an empty archived_at here used to open a second door
        // into the same deadlock: finalize_success writes archived_at and
        // clears job_id in two separate writes, so a process that dies
        // between them (autoheal restart, Apache wedge) leaves a sitewide
        // record with BOTH set. Such a record was excluded from the poll
        // here, skipped by the submit step (which ignores anything already
        // archived), and refused by invalidate_sitewide (which will not
        // touch a record with a job_id) — while the loop's drain probe
        // counted its job_id as outstanding work. Unreachable and
        // undrainable: the daemon spins forever, logging a clean sweep.
        if ( ! empty( $state['job_id'] ) ) {
            $in_flight[ $state['job_id'] ] = array(
                'key'          => $rec['key'],
                'url'          => $rec['url'],
                'submitted_at' => (int) ( $state['submitted_at'] ?? 0 ),
                // Denormalised for the same reason as submitted_at: the poll
                // loop carries no 'read', and finalize_error needs the running
                // count to advance the back-off exponent.
                'error_count'  => (int) ( $state['error_count'] ?? 0 ),
                'write'        => $rec['write'],
            );
        }
    }

    // Skip jobs younger than OP_WB_YOUNG_JOB_SKIP_SEC — SPN's minimum
    // capture time is ~20s, so polling any sooner is a wasted round-trip.
    $ripe_job_ids = array();
    foreach ( $in_flight as $jid => $rec ) {
        // Unknown age counts as ripe: a record with no submitted_at is a
        // zombie the poll needs to see so the branches below can retire it.
        $age = onionpress_wayback_job_age( $rec, $now );
        if ( $age === null || $age >= OP_WB_YOUNG_JOB_SKIP_SEC ) {
            $ripe_job_ids[] = $jid;
        }
    }
    // Counted before the budget cap below trims the list, so the log keeps
    // telling these two apart: "too young to be worth polling" and "no time
    // left to poll it this iteration" are different problems.
    $skipped_young = count( $in_flight ) - count( $ripe_job_ids );

    // Bound the poll by the time left, the same way the submit below is
    // bounded. poll_parallel runs ceil(N / (CONCURRENT_MAX * STATUS_BATCH_MAX))
    // SEQUENTIAL groups, each able to sit on a full OP_WB_REQUEST_TIMEOUT_SEC,
    // so its cost grows with the backlog without limit — it was the one phase
    // of the iteration with no budget check at all, and the phase most likely
    // to blow the budget is exactly the one that runs when the queue is
    // deepest. Jobs cut here are simply absent from $ripe_job_ids, so they
    // are neither polled nor judged forgotten; the next iteration takes
    // them. Floor at one group so a huge backlog still makes progress — that
    // floor is this phase's whole contribution to the overshoot the budget
    // comment accounts for, one request timeout.
    $poll_per_group  = OP_WB_CONCURRENT_MAX * OP_WB_STATUS_BATCH_MAX;
    $poll_groups_fit = max( 1, (int) floor(
        max( 0, $deadline - microtime( true ) ) / OP_WB_REQUEST_TIMEOUT_SEC
    ) );
    $poll_cap     = $poll_groups_fit * $poll_per_group;
    $polled_capped = max( 0, count( $ripe_job_ids ) - $poll_cap );
    if ( $polled_capped > 0 ) {
        $ripe_job_ids = array_slice( $ripe_job_ids, 0, $poll_cap );
    }

    $polled_success = 0;
    $polled_error   = 0;
    $polled_pending = 0;
    $polled_cdx_hit = 0;
    $polled_unknown = 0;
    $polled_lost    = 0;
    $poll_covered   = array();
    $results        = onionpress_wayback_poll_parallel( $ripe_job_ids, $poll_covered );

    // First pass: finalize successes and pending; collect error-jobs for
    // a CDX fallback check. SPN's job memory is unreliable — it flips
    // success→error after a few minutes even when the capture persists
    // in CDX. Before trashing a post we verify against CDX directly.
    $cdx_check_urls  = array();
    $cdx_check_recs  = array();
    $cdx_check_exts  = array();
    $answered        = array();
    foreach ( $results as $res ) {
        if ( ! is_array( $res ) ) continue;
        $jid = (string) ( $res['job_id'] ?? '' );
        if ( ! isset( $in_flight[ $jid ] ) ) continue;
        $answered[ $jid ] = true;
        $rec    = $in_flight[ $jid ];
        $status = (string) ( $res['status'] ?? '' );

        if ( $status === 'success' ) {
            onionpress_wayback_finalize_success( $rec['write'], $rec['url'], $res );
            $rec['write']( array( 'job_id' => '', 'submitted_at' => '',
                'last_error_ext' => '', 'last_error_at' => '', 'error_count' => '' ) );
            $polled_success++;
            onionpress_wayback_log( 'Archived ' . $rec['key'] . ' ts=' . (string) ( $res['timestamp'] ?? '' )
                . ' dur=' . (string) ( $res['duration_sec'] ?? '' ) );
        } elseif ( $status === 'error' ) {
            $ext = (string) ( $res['status_ext'] ?? 'error' );
            // Queue for CDX verification before deciding this is a real loss.
            $cdx_check_urls[ $jid ] = $rec['url'];
            $cdx_check_recs[ $jid ] = $rec;
            $cdx_check_exts[ $jid ] = $ext;
        } else {
            // Clear job_id if either:
            //  (a) submitted_at is set and older than STALE_PENDING_SEC — SPN
            //      should have resolved by now, something's stuck.
            //  (b) submitted_at is missing — v3-era zombie with a job_id SPN
            //      no longer remembers; it will poll "pending" forever.
            $age    = onionpress_wayback_job_age( $rec, $now );
            $stale  = $age !== null && $age > OP_WB_STALE_PENDING_SEC;
            $zombie = $age === null;
            if ( $stale || $zombie ) {
                $rec['write']( array( 'job_id' => '', 'submitted_at' => '' ) );
                onionpress_wayback_log( 'SPN stale-pending ' . $rec['key']
                    . ( $zombie ? ' (zombie, no submitted_at)' : ' (age=' . $age . 's)' )
                    . ', clearing for resubmit' );
            } else {
                $polled_pending++;
            }
        }
    }

    // Every branch above keys off a status dict SPN actually returned. But
    // SPN has a fourth behaviour its API doesn't document: a job_id it has
    // entirely forgotten is simply ABSENT from the /save/status response —
    // no 'success', no 'error', not even 'pending'. Such a job matches no
    // branch, so it is never finalized and never cleared, while Step B
    // skips any record still carrying a job_id. That is a permanent
    // deadlock, and it is not theoretical: this site's home and feed sat
    // on forgotten job_ids for five days, archiving nothing, while the
    // sweep logged a healthy avail=40 every single minute.
    //
    // Treat "ripe, answered-about, and unmentioned" exactly like
    // stale-pending. Two independent guards keep a transient failure from
    // being read as amnesia:
    //
    //  - $poll_covered — only batches that came back HTTP 200 and parsed
    //    count. Without this, one timed-out Tor request carrying a 20-job
    //    batch would reclassify all 20 as forgotten and resubmit them, and
    //    at 100+ in flight that is several batches per sweep.
    //  - STALE_PENDING_SEC — the same age at which the branch above gives
    //    up on a job SPN *did* answer 'pending' for. A job SPN has genuinely
    //    forgotten cannot be younger than that in any useful sense.
    foreach ( $ripe_job_ids as $jid ) {
        if ( isset( $answered[ $jid ] ) || ! isset( $in_flight[ $jid ] ) ) continue;
        $rec = $in_flight[ $jid ];
        if ( ! isset( $poll_covered[ $jid ] ) ) {
            // We never got an answer covering this job — say so rather
            // than guessing. Counted so the log can show the shortfall.
            $polled_lost++;
            continue;
        }
        $age = onionpress_wayback_job_age( $rec, $now );
        if ( $age !== null && $age <= OP_WB_STALE_PENDING_SEC ) continue;
        $rec['write']( array( 'job_id' => '', 'submitted_at' => '' ) );
        $polled_unknown++;
        onionpress_wayback_log( 'SPN forgot ' . $rec['key'] . ' (job_id absent from status response'
            . ( $age === null ? ', no submitted_at' : ', age=' . $age . 's' )
            . '), clearing for resubmit' );
    }

    // Second pass: for each SPN-errored job, verify against Wayback's
    // /wayback/available (with CDX fallback). If there's a capture,
    // mark archived; otherwise resubmit next tick.
    //
    // Cap the rescue burst so Tor SOCKS stays responsive to other
    // consumers (heartbeat, reachability). Jobs beyond the cap get
    // their error recorded and job_id cleared immediately — they'll
    // run through rescue on a later sweep when they're re-errored.
    // Out of budget: leave these records exactly as they are and let the
    // next sweep re-poll them. Deliberately NOT the $cdx_defer path — that
    // one records an error and clears the job_id, and doing that without
    // having checked CDX would throw away captures that do exist. Better
    // to do nothing than to record a verdict we did not verify.
    $over_budget = microtime( true ) >= $deadline;
    $cdx_skipped = $over_budget ? count( $cdx_check_urls ) : 0;
    $cdx_do_now  = $over_budget ? array() : array_slice( $cdx_check_urls, 0, 5, true );
    $cdx_defer   = $over_budget ? array() : array_slice( $cdx_check_urls, 5, null, true );
    foreach ( $cdx_defer as $jid => $url ) {
        $rec = $cdx_check_recs[ $jid ];
        onionpress_wayback_finalize_error( $rec['write'], $cdx_check_exts[ $jid ], $rec );
        $rec['write']( array( 'job_id' => '', 'submitted_at' => '' ) );
        $polled_error++;
    }
    if ( ! empty( $cdx_do_now ) ) {
        $cdx = onionpress_wayback_cdx_lookup_parallel( $cdx_do_now );
        foreach ( $cdx_do_now as $jid => $url ) {
            $rec = $cdx_check_recs[ $jid ];
            $ts  = (string) ( $cdx[ $jid ] ?? '' );
            if ( $ts !== '' ) {
                $rec['write']( array(
                    'archived_at'    => time(),
                    'snapshot_ts'    => $ts,
                    'job_id'         => '',
                    'submitted_at'   => '',
                    'last_error_ext' => '',
                    'error_count'    => '',
                    'last_error_at'  => '',
                ) );
                $polled_cdx_hit++;
                onionpress_wayback_log( 'CDX rescued ' . $rec['key'] . ' ts=' . $ts
                    . ' (SPN said ' . $cdx_check_exts[ $jid ] . ')' );
            } else {
                onionpress_wayback_finalize_error( $rec['write'], $cdx_check_exts[ $jid ], $rec );
                $rec['write']( array( 'job_id' => '', 'submitted_at' => '' ) );
                $polled_error++;
            }
        }
    }

    // ---- Step B: submit fresh work up to available slots, in parallel ----
    // SPN's `available` is already net of `processing`, so we use it as
    // the submission budget directly. Sites-wide (home + feed) first so
    // they never starve, then fresh post URLs.
    // Bound the batch by the time left as well as by slots. submit_parallel
    // runs ceil(N / OP_WB_CONCURRENT_MAX) SEQUENTIAL groups, each able to
    // sit on a full OP_WB_REQUEST_TIMEOUT_SEC, so at the full 40-URL budget
    // one call can run 600s on its own — long enough to outlive the lock
    // heartbeat no matter what the phase-entry check said. Floor at one
    // group: making no progress at all is worse than one slightly long
    // iteration. The reachability probe below runs before the submit and is
    // itself a full request, so the batch has to be sized out of what is left
    // AFTER paying for it — otherwise the probe spends time the budget had
    // already granted to the submit.
    $groups_that_fit = max( 1, (int) floor(
        max( 0, $deadline - microtime( true ) - OP_WB_REQUEST_TIMEOUT_SEC )
        / OP_WB_REQUEST_TIMEOUT_SEC
    ) );
    $budget = max( 0, min(
        OP_WB_SUBMIT_BATCH_MAX,
        $available,
        $groups_that_fit * OP_WB_CONCURRENT_MAX
    ) );
    $to_submit = array();      // map: key → url
    $records_by_key = array(); // map: key → record (for write-back)
    $cooling     = 0;          // records skipped by the per-URL retry back-off

    foreach ( onionpress_wayback_sitewide_records() as $rec ) {
        if ( count( $to_submit ) >= $budget ) break;
        $state = $rec['read']();
        if ( ! empty( $state['archived_at'] ) ) continue;
        if ( ! empty( $state['job_id'] ) ) continue;
        // A URL SPN has just failed is still unarchived and still has no
        // job_id — the exact state this loop selects on. Without the gate it
        // is resubmitted every sweep for as long as it keeps failing.
        // time(), not $now: $now predates the poll above, and the poll is
        // where a failure is recorded. Against the stale $now a last_error_at
        // just written reads as a FUTURE timestamp, takes the clock-skew
        // escape hatch in retry_ready, and the URL that failed seconds ago is
        // resubmitted in the very same iteration — the storm, narrowed to one
        // resubmit per sweep rather than removed. Observed live before this.
        if ( ! onionpress_wayback_retry_ready( $state, time() ) ) { $cooling++; continue; }
        $to_submit[ $rec['key'] ] = $rec['url'];
        $records_by_key[ $rec['key'] ] = $rec;
    }

    if ( count( $to_submit ) < $budget ) {
        $posts = onionpress_wayback_posts_needing_submit( $budget - count( $to_submit ) );
        foreach ( $posts as $rec ) {
            if ( count( $to_submit ) >= $budget ) break;
            if ( ! onionpress_wayback_retry_ready( $rec['read'](), time() ) ) { $cooling++; continue; }
            $to_submit[ $rec['key'] ] = $rec['url'];
            $records_by_key[ $rec['key'] ] = $rec;
        }
    }

    $submitted   = 0;
    $discarded   = 0;          // job_ids SPN issued that we could not record
    $hit_429     = false;
    $submit_skip = '';
    if ( ! empty( $to_submit )
         && ( $spn_status === 'no-credentials' || $spn_status === 'rejected' ) ) {
        // The one place where the optimistic-proceed above is the wrong
        // answer, and the reason splitting user_status()'s null was worth
        // doing at all. submit_parallel presents the SAME Authorization
        // header this status call just used: with no keys it returns '' for
        // every URL without touching the network, and with keys SPN has just
        // refused it would spend eight sequential Tor groups collecting eight
        // batches of 401s — every 30s, for as long as the daemon lives.
        //
        // The optimism elsewhere is optimism about OUR knowledge. A status
        // call that timed out tells us nothing about SPN, so we guess in the
        // direction that keeps the queue moving and let a real 429 correct
        // us. A 401/403 is not missing knowledge: it is SPN answering, and
        // the answer is no. Treating an explicit refusal as "we didn't hear
        // back, carry on" is not optimism, it is discarding a reply.
        //
        // It still self-heals if the refusal was not about us — an
        // archive.org maintenance or rate-limit page can 403 without ever
        // reading our keys. The back-off expires on its own and the admin
        // page's Kick button clears it outright, which is what someone who
        // has just corrected their keys will press. Deliberately NOT a latch.
        //
        // Only the submit is gated. Polling still runs: job_ids submitted
        // while the keys were good are answered by /save/status regardless,
        // and abandoning in-flight work over a credential problem would turn
        // a fixable misconfiguration into lost captures.
        update_option( OP_WB_OPT_BACKOFF_UNTIL,
            time() + OP_WB_BACKOFF_BAD_CREDENTIALS, false );
        $submit_skip = 'spn ' . $spn_status;
        onionpress_wayback_log( 'Sweep paused: SPN ' . (
            $spn_status === 'rejected'
                ? 'rejected our Save Page Now credentials (401/403)'
                : 'credentials not configured'
            ) . ', backing off ' . OP_WB_BACKOFF_BAD_CREDENTIALS . 's rather than '
            . 'submitting ' . count( $to_submit ) . ' URL(s) it would refuse the same way' );
    } elseif ( ! empty( $to_submit ) && microtime( true ) >= $deadline ) {
        // Step A ate the whole budget. Submitting now would push the
        // iteration past OP_WB_LOOP_LOCK_STALE_SEC without heartbeating the
        // lock, at which point a second daemon claims it and two of them
        // run at once against the same queue.
        $submit_skip = 'over budget';
    } elseif ( ! empty( $to_submit ) && ! onionpress_wayback_self_reachable( $onion ) ) {
        // The gate this plugin's header has always advertised, finally
        // wired up — it was defined, documented, mocked by the tests, and
        // called from nowhere. SPN's crawler reaches us the same way a
        // client does, so submitting while our own onion is unreachable
        // spends a slot to archive a failure page and comes back
        // error:no-captures. Back off instead, using the constant that
        // existed for this and was likewise never referenced.
        // time(), not $now: $now was captured at the top of the iteration,
        // before a status call, a poll and a CDX pass that between them can
        // burn the whole budget. Dating the back-off from then makes a 120s
        // pause into a 40s one on a normal sweep, and into no pause at all
        // on the slow sweeps this gate exists for — the daemon would go
        // straight back to probing every 30s while logging "backing off".
        update_option( OP_WB_OPT_BACKOFF_UNTIL, time() + OP_WB_BACKOFF_UNREACHABLE, false );
        $submit_skip = 'own onion unreachable';
        onionpress_wayback_log( 'Sweep paused: own onion not answering over Tor, '
            . 'backing off ' . OP_WB_BACKOFF_UNREACHABLE . 's rather than '
            . 'submitting ' . count( $to_submit ) . ' URL(s) SPN would fail to capture' );
    } elseif ( ! empty( $to_submit ) && microtime( true ) >= $deadline ) {
        // The probe above is a full request in its own right, and it runs
        // AFTER the gate that admitted it — on a cold descriptor it can spend
        // OP_WB_REQUEST_TIMEOUT_SEC before returning true. Re-check rather
        // than assume the pre-probe verdict still holds: without this the
        // phase's worst case is probe + group = two request timeouts of
        // overshoot, and the arithmetic on OP_WB_SWEEP_BUDGET_SEC — and so
        // OP_WB_LOOP_LOCK_STALE_SEC derived from it — is stated in terms of
        // one. Named apart from plain 'over budget' because the two say
        // different things about where the time went.
        $submit_skip = 'over budget after reachability probe';
    }
    if ( ! empty( $to_submit ) && $submit_skip === '' ) {
        $submit_results = onionpress_wayback_submit_parallel( $to_submit );
        foreach ( $submit_results as $key => $result ) {
            $rec = $records_by_key[ $key ] ?? null;
            if ( $rec === null ) continue;
            if ( $result === 'RATE_LIMITED' ) {
                $hit_429 = true;
                continue;
            }
            if ( $result !== '' ) {
                // A write can be REFUSED, and then this is not a submission we
                // may count. A moss record's write goes into the generation it
                // was built against (see moss_write), so a publish landing
                // between building the batch and writing its results back
                // makes the write a no-op: the job_id is dropped, SPN still
                // runs the capture and still holds the slot, nothing ever
                // polls it, and the sweep logged submitted=N as if all was
                // well. Count what landed, and say out loud what did not.
                if ( $rec['write']( array( 'job_id' => $result, 'submitted_at' => time() ) ) === false ) {
                    $discarded++;
                    onionpress_wayback_log( 'Submitted ' . $rec['key'] . ' but could not record its '
                        . 'job_id (the generation it belongs to is no longer live); the capture '
                        . 'will complete at SPN unpolled and the page is resubmitted under the '
                        . 'new generation' );
                    continue;
                }
                $submitted++;
            }
        }
        if ( $hit_429 ) {
            update_option( OP_WB_OPT_BACKOFF_UNTIL, time() + OP_WB_BACKOFF_NO_SLOTS, false );
            onionpress_wayback_log( 'Submit batch hit 429, backing off ' . OP_WB_BACKOFF_NO_SLOTS . 's' );
        }
    }

    // The line that has to be self-diagnosing. Replay the five-day outage
    // against the OLD format and every counter reads zero — the two jobs
    // stuck in flight appeared nowhere, so "nothing to do" and "everything
    // is wedged" printed identically, 1621 times a day. in-flight and
    // queued are what make those two distinguishable at a glance, and
    // avail now says outright when it is a guess rather than a reading.
    $elapsed = round( microtime( true ) - ( $deadline - OP_WB_SWEEP_BUDGET_SEC ), 2 );
    $notes   = array();
    // Unconditional, unlike every other note here. The others report an
    // exception and are silent on the nominal path; this one has to say
    // something on every line, because the failure it describes is precisely
    // that a healthy-looking line and a broken one were byte-identical. An
    // operator should never have to infer from an absent token whether the
    // slot count was read or invented.
    $notes[] = 'spn-status=' . $spn_status;
    // A queue that is full of cooling records looks exactly like an empty
    // one from queued=/submitted= alone — which is how the storm went
    // unnoticed in the first place, only in the other direction.
    if ( $cooling > 0 )         $notes[] = 'retry-cooling=' . $cooling;
    // Submitted, but the job_id could not be stored — a slot spent for
    // nothing. queued= minus submitted= would show a gap and blame the
    // submit; this names the real cause.
    if ( $discarded > 0 )       $notes[] = 'submit-discarded=' . $discarded;
    if ( $polled_lost > 0 )     $notes[] = 'poll-lost=' . $polled_lost;
    if ( $polled_capped > 0 )   $notes[] = 'poll-capped=' . $polled_capped;
    // "skipped", not "deferred": the $cdx_defer path retires its records,
    // this one deliberately leaves them for the next sweep. Borrowing the
    // other path's name would tell a reader the opposite of what happened.
    if ( $cdx_skipped > 0 )     $notes[] = 'cdx-skipped=' . $cdx_skipped;
    if ( $submit_skip !== '' )  $notes[] = 'submit-skipped=' . str_replace( ' ', '-', $submit_skip );
    onionpress_wayback_log( sprintf(
        'Sweep: avail=%d%s in-flight=%d polled(success=%d cdx-hit=%d err=%d pending=%d '
        . 'forgotten=%d skipped-young=%d) queued=%d submitted=%d elapsed=%ss%s',
        $available, ( $user === null ? '?' : '' ), count( $in_flight ),
        $polled_success, $polled_cdx_hit, $polled_error, $polled_pending, $polled_unknown,
        $skipped_young,
        count( $to_submit ), $submitted, $elapsed,
        $notes ? ' [' . implode( ' ', $notes ) . ']' : ''
    ) );

    return array(
        'submitted' => $submitted,
        'success'   => $polled_success,
        'cdx'       => $polled_cdx_hit,
        'error'     => $polled_error,
        'forgotten' => $polled_unknown,
        'lost'      => $polled_lost,
    );
}
add_action( 'onionpress_wayback_sweep', 'onionpress_wayback_sweep' );

/**
 * Make wp-cron run `onionpress_wayback_sweep` as soon as possible.
 * $reason is a short slug ('kick', 'recycle', 'comment') identifying the
 * caller; it is passed as the event's argument and MUST be distinct per
 * caller — see below.
 */
function onionpress_wayback_schedule_sweep_now( $reason ) {
    // The argument is load-bearing, not annotation. wp_schedule_single_event()
    // silently refuses — returns false, and every caller here discarded that
    // return — when an event for the same hook with the same args already
    // exists within 10 minutes. The recurring watchdog entry has args=[] and
    // a 300s interval, so it is ALWAYS inside that window: every no-args
    // call this plugin made scheduled precisely nothing, and what actually
    // ran the sweep was the recurring event happening to be due. That made
    // "archive right now" mean "within the next five minutes", which is not
    // what publish-then-archive is supposed to feel like. Confirmed against
    // the running WordPress rather than inferred from the docs.
    wp_schedule_single_event( time(), 'onionpress_wayback_sweep', array( $reason ) );
    // WP-Cron is pseudo-cron: scheduling only marks the event due, and
    // nothing runs it until some request triggers WP's cron check. On a
    // low-traffic onion site that request may not arrive for a long time,
    // so fire the loopback ourselves and don't wait on it.
    $cron_url = site_url( 'wp-cron.php?doing_wp_cron=' . microtime( true ) );
    wp_remote_post( $cron_url, array( 'timeout' => 0.01, 'blocking' => false, 'sslverify' => false ) );
}

/**
 * Clear the sweep back-off and start a sweep immediately. Used by the
 * admin "kick" button and by the moss commit route.
 */
function onionpress_wayback_kick_now() {
    delete_option( OP_WB_OPT_BACKOFF_UNTIL );
    onionpress_wayback_schedule_sweep_now( 'kick' );
}

/**
 * Invalidate the home page + feed captures so the next kick re-archives
 * them. Skipped when a capture is already in flight — see save_post's
 * comment below for why that matters.
 */
function onionpress_wayback_invalidate_sitewide() {
    $home_state = onionpress_wayback_opt_read( OP_WB_OPT_HOME );
    $feed_state = onionpress_wayback_opt_read( OP_WB_OPT_FEED );
    if ( empty( $home_state['job_id'] ) ) {
        delete_option( OP_WB_OPT_HOME );
    }
    if ( empty( $feed_state['job_id'] ) ) {
        delete_option( OP_WB_OPT_FEED );
    }
}

// ────────────── save_post hook: invalidate home/feed + retry post ───
// Imported social posts (Twitter/Mastodon/Bluesky — anything with a
// `_source_id` meta) are captured exactly once and never re-archived
// on subsequent save_post events. Their content is frozen historical
// data, but the importers themselves call wp_update_post repeatedly
// (media-attach, thread fix-ups, etc.); treating those as "edit →
// re-capture" produced rounds of duplicate Wayback submissions for
// posts whose original content never changed.
//
// Original posts (no `_source_id`) keep the "edit → re-capture"
// behaviour — for hand-written blog content that's the right thing.
//
// Home page + feed always re-archive when content changes, regardless
// of source — they're a moving window that legitimately changes.

add_action( 'save_post', function ( $post_id, $post, $update ) {
    if ( defined( 'DOING_AUTOSAVE' ) && DOING_AUTOSAVE ) return;
    if ( wp_is_post_revision( $post_id ) ) return;
    if ( $post->post_status !== 'publish' ) return;
    if ( ! in_array( $post->post_type, array( 'post', 'page' ), true ) ) return;

    // Home page + feed content just changed — invalidate their captures.
    // Skip invalidation if a capture is already in flight: SPN's crawl
    // happens many seconds after submission, so the in-flight job will
    // already render this save_post's content. Wiping the option would
    // drop the job_id, causing the next sweep to resubmit and burn an
    // SPN slot. Coalescing bursts is structural, not explicit — one row
    // per subsite already collapses N saves into ≤1 fresh submission.
    onionpress_wayback_invalidate_sitewide();

    // Re-archive on edit, but only for original posts. Imported social
    // posts (anything with _source_id) stay archived once.
    $is_imported = (string) get_post_meta( $post_id, '_source_id', true ) !== '';
    if ( $update && ! $is_imported && get_post_meta( $post_id, OP_WB_META_ARCHIVED_AT, true ) ) {
        onionpress_wayback_post_write( $post_id, array(
            'archived_at'     => '',
            'snapshot_ts'     => '',
            'job_id'          => '',
            'submitted_at'    => '',
            'original_url'    => '',
            'duration_sec'    => '',
            'resources_count' => '',
            'outlinks_count'  => '',
            'last_error_ext'  => '',
            'last_error_at'   => '',
            'error_count'     => '',
        ) );
    }

    // Kick the sweep now rather than waiting for wp-cron's next organic
    // fire — a fresh publish is exactly when a human is likely watching.
    onionpress_wayback_kick_now();
    onionpress_wayback_log( 'save_post ' . $post_id . ': cleared home/feed'
        . ( $update && ! $is_imported ? ' + post meta' : '' ) . ', scheduled immediate sweep' );
}, 10, 3 );

// ───── wp_insert_comment hook: one re-archive per post after threading ─
// Social-importer threading folds replies into comments on parent posts.
// The parent's rendered HTML changes (now shows the conversation), so
// the existing SPN snapshot becomes stale — but our save_post policy
// keeps imported posts at "archive once" to avoid the importer-fix-up
// re-archive loop. The fix: invalidate the parent's snapshot exactly
// once when the FIRST comment lands, then never again. A thread of 12
// self-replies re-archives the parent one time; further comments are a
// no-op. Original posts (no _source_id) follow the existing edit-aware
// save_post path and don't need this hook.
add_action( 'wp_insert_comment', function ( $comment_id, $comment ) {
    $post_id = (int) $comment->comment_post_ID;
    if ( ! $post_id ) return;
    $post = get_post( $post_id );
    if ( ! $post || $post->post_status !== 'publish' ) return;
    if ( ! in_array( $post->post_type, array( 'post', 'page' ), true ) ) return;
    // Only matters for imported posts. Originals get re-archived via
    // save_post when actually edited, which doesn't fire on comment add.
    if ( (string) get_post_meta( $post_id, '_source_id', true ) === '' ) return;
    // Already re-snapshotted once after threading → skip.
    if ( (string) get_post_meta( $post_id, OP_WB_META_RESNAPSHOT_DONE, true ) === '1' ) return;
    // No prior snapshot exists yet → save_post will pick it up the
    // normal way; no need to re-snapshot something that hasn't been
    // captured at all.
    if ( (string) get_post_meta( $post_id, OP_WB_META_ARCHIVED_AT, true ) === '' ) return;

    update_post_meta( $post_id, OP_WB_META_RESNAPSHOT_DONE, '1' );
    onionpress_wayback_post_write( $post_id, array(
        'archived_at'     => '',
        'snapshot_ts'     => '',
        'job_id'          => '',
        'submitted_at'    => '',
        'original_url'    => '',
        'duration_sec'    => '',
        'resources_count' => '',
        'outlinks_count'  => '',
        'last_error_ext'  => '',
        'last_error_at'   => '',
        'error_count'     => '',
    ) );
    delete_option( OP_WB_OPT_BACKOFF_UNTIL );
    onionpress_wayback_schedule_sweep_now( 'comment' );
    onionpress_wayback_log( 'wp_insert_comment ' . $comment_id
        . ' on post ' . $post_id . ': cleared snapshot for re-archive (one-shot)' );
}, 10, 2 );

// ────────────────────────────── cron ────────────────────────────────

add_filter( 'cron_schedules', function ( $schedules ) {
    $schedules['onionpress_wayback_watchdog'] = array(
        'interval' => OP_WB_CRON_INTERVAL,
        'display'  => 'OnionPress Wayback watchdog',
    );
    return $schedules;
} );

add_action( 'init', function () {
    // (Re)schedule the sweep on the current watchdog schedule. Unschedule
    // any prior-schedule instances so we don't end up with two cron
    // entries for the same hook under different intervals.
    $existing = wp_next_scheduled( 'onionpress_wayback_sweep' );
    if ( $existing ) {
        $cron = _get_cron_array();
        foreach ( (array) $cron as $ts => $hooks ) {
            if ( isset( $hooks['onionpress_wayback_sweep'] ) ) {
                foreach ( $hooks['onionpress_wayback_sweep'] as $sig => $entry ) {
                    // Only RECURRING entries under the wrong interval. A
                    // one-shot has schedule === false, and every immediate
                    // sweep — kick, recycle handoff, comment re-snapshot —
                    // is a one-shot. This ran on every init, including the
                    // init inside the very wp-cron.php request the kick had
                    // just fired, so it deleted those events moments before
                    // wp_cron() would have run them. Between that and the
                    // dedupe documented on schedule_sweep_now(), "sweep now"
                    // had two independent reasons to do nothing at all.
                    $schedule = $entry['schedule'] ?? '';
                    if ( $schedule && $schedule !== 'onionpress_wayback_watchdog' ) {
                        wp_unschedule_event( $ts, 'onionpress_wayback_sweep', $entry['args'] ?? array() );
                    }
                }
            }
        }
    }
    if ( ! wp_next_scheduled( 'onionpress_wayback_sweep' ) ) {
        wp_schedule_event( time(), 'onionpress_wayback_watchdog', 'onionpress_wayback_sweep' );
    }

    // One-time v3 → v4 migration: drop the retry-machine postmeta we no
    // longer use. Preserve archived_at + snapshot_ts (the only real
    // outcome record) and job_id (active in-flight work).
    if ( get_option( 'op_wayback_v4_migrated' ) !== 'yes' ) {
        global $wpdb;
        $stale_keys = array(
            '_op_wayback_retry_count',
            '_op_wayback_retry_after',
            '_op_wayback_failed_at',
            '_op_wayback_failed_reason',
        );
        foreach ( $stale_keys as $k ) {
            $wpdb->delete( $wpdb->postmeta, array( 'meta_key' => $k ) );
        }
        @unlink( '/var/lib/onionpress/wayback-queue.json' );
        @unlink( '/var/lib/onionpress/wayback-archived.json' );

        $legacy_ts = wp_next_scheduled( 'onionpress_drain_wayback_queue' );
        if ( $legacy_ts ) {
            wp_unschedule_event( $legacy_ts, 'onionpress_drain_wayback_queue' );
        }
        update_option( 'op_wayback_v4_migrated', 'yes' );
        onionpress_wayback_log( 'v4 migration: stale retry-state meta cleared' );
    }
} );

// ───────────────────────── admin page ───────────────────────────────

/**
 * Register a Wayback admin submenu under the Social Archive top-level
 * menu. Uses late priority (20) so the Social Archive plugin has
 * registered the parent menu first.
 */
add_action( 'admin_menu', function () {
    if ( ! defined( 'ONIONPRESS_SOCIAL_ADMIN_SLUG' ) ) {
        // Social Archive plugin not loaded — fall back to a top-level menu.
        add_menu_page(
            'Wayback Archive',
            'Wayback Archive',
            'manage_options',
            'onionpress-wayback',
            'onionpress_wayback_admin_page',
            'dashicons-backup',
            26
        );
        return;
    }
    add_submenu_page(
        ONIONPRESS_SOCIAL_ADMIN_SLUG,
        'Wayback Archive',
        'Wayback',
        'manage_options',
        'onionpress-wayback',
        'onionpress_wayback_admin_page'
    );
}, 20 );

/**
 * Handle POST actions from the admin page (kick daemon, clear lock).
 * Registered before admin_menu render so redirects fire cleanly.
 */
add_action( 'admin_post_onionpress_wayback_action', function () {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_die( 'Forbidden', 403 );
    }
    check_admin_referer( 'onionpress_wayback_action' );

    $action = sanitize_text_field( $_POST['op_action'] ?? '' );
    switch ( $action ) {
        case 'kick':
            // Clear the lock and any backoff, then trigger the sweep
            // directly. If the sweep is already running under a valid
            // lock, the token-ownership check will make this invocation
            // exit cleanly without disrupting it — but clearing the
            // lock first breaks any truly-stuck state.
            delete_option( 'op_wayback_sweep_lock' );
            // Clear WP's `doing_cron` lock too. If a previous wp-cron
            // spawn died mid-run without cleaning up (container restart,
            // pkill), this transient blocks new wp-cron fires for up to
            // 60s from its timestamp. Each subsequent page load just
            // refreshes it via race, so it can stay stuck for a long
            // time in practice. Clearing it lets cron fire immediately.
            delete_transient( 'doing_cron' );
            onionpress_wayback_kick_now();
            $msg = 'Daemon kicked — archiving will begin within a few seconds.';
            break;
        case 'clear_backoff':
            delete_option( OP_WB_OPT_BACKOFF_UNTIL );
            $msg = 'Backoff cleared.';
            break;
        case 'clear_lock':
            delete_option( 'op_wayback_sweep_lock' );
            $msg = 'Lock cleared. A stuck daemon (if any) will exit on its next heartbeat.';
            break;
        default:
            $msg = '';
    }
    wp_safe_redirect( add_query_arg(
        array( 'op_msg' => rawurlencode( $msg ) ),
        admin_url( 'admin.php?page=onionpress-wayback' )
    ) );
    exit;
} );

function onionpress_wayback_admin_page() {
    $totals        = onionpress_wayback_queue_totals();
    $backoff_until = (int) get_option( OP_WB_OPT_BACKOFF_UNTIL, 0 );
    $backoff_secs  = max( 0, $backoff_until - time() );
    $lock_raw      = (string) get_option( 'op_wayback_sweep_lock', '' );
    $lock_ts       = 0;
    $lock_token    = '';
    if ( strpos( $lock_raw, ':' ) !== false ) {
        list( $lock_token, $lock_ts_str ) = explode( ':', $lock_raw, 2 );
        $lock_ts = (int) $lock_ts_str;
    }
    $lock_age      = $lock_ts > 0 ? time() - $lock_ts : 0;
    $lock_active   = $lock_age > 0 && $lock_age < OP_WB_LOOP_LOCK_STALE_SEC;
    $next_cron     = wp_next_scheduled( 'onionpress_wayback_sweep' );
    $msg           = isset( $_GET['op_msg'] ) ? wp_unslash( $_GET['op_msg'] ) : '';
    $pct           = $totals['total'] > 0 ? round( $totals['archived'] * 100 / $totals['total'], 1 ) : 0;

    // Pull the most recent N log lines written by error_log.
    ?>
    <div class="wrap">
        <h1>Wayback Archive</h1>

        <?php if ( $msg ) : ?>
            <div class="notice notice-success is-dismissible"><p><?php echo esc_html( $msg ); ?></p></div>
        <?php endif; ?>

        <?php
        // Context-aware Wayback link: onion when viewing via .onion,
        // clearnet otherwise. Uses the helper from the Social Archive
        // plugin if loaded, else builds inline.
        if ( function_exists( 'onionpress_social_wayback_home_url' ) ) {
            $wb_home = onionpress_social_wayback_home_url();
        } else {
            $host = (string) ( $_SERVER['HTTP_HOST'] ?? '' );
            $wb_home = substr( $host, -6 ) === '.onion'
                ? 'https://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/'
                : 'https://web.archive.org/';
        }
        ?>
        <p>This site continuously submits its posts to the
            <a href="<?php echo esc_url( $wb_home ); ?>" target="_blank" rel="noopener">Internet Archive's Wayback Machine</a>
            via Save Page Now. The captures persist independent of this onion — so even if
            this server goes offline, your posts remain publicly accessible via
            <code>web.archive.org/web/&lt;timestamp&gt;/&lt;post-url&gt;</code>.</p>

        <h2>Progress</h2>
        <table class="wp-list-table widefat striped" style="max-width:720px;">
            <tbody>
                <tr>
                    <th style="width:220px;">Total posts</th>
                    <td><?php echo number_format_i18n( $totals['total'] ); ?></td>
                </tr>
                <tr>
                    <th>Archived in Wayback</th>
                    <td><strong><?php echo number_format_i18n( $totals['archived'] ); ?></strong>
                        <?php if ( $totals['total'] > 0 ) : ?>
                            &middot; <?php echo esc_html( $pct ); ?>%
                        <?php endif; ?>
                    </td>
                </tr>
                <tr>
                    <th>In flight at SPN</th>
                    <td><?php echo number_format_i18n( $totals['in_flight'] ); ?> (submitted, awaiting capture result)</td>
                </tr>
                <tr>
                    <th>Remaining</th>
                    <td><?php echo number_format_i18n( $totals['remaining'] ); ?></td>
                </tr>
            </tbody>
        </table>

        <h2>Daemon status</h2>
        <table class="wp-list-table widefat striped" style="max-width:720px;">
            <tbody>
                <tr>
                    <th style="width:220px;">Daemon</th>
                    <td>
                        <?php if ( $lock_active ) : ?>
                            <span style="color:#008000;">● Running</span>
                            &middot; token <code><?php echo esc_html( substr( $lock_token, 0, 6 ) ); ?></code>
                            &middot; last heartbeat <?php echo esc_html( $lock_age ); ?>s ago
                        <?php elseif ( $lock_ts > 0 ) : ?>
                            <span style="color:#a00;">● Stale lock</span>
                            (<?php echo esc_html( $lock_age ); ?>s since last heartbeat;
                            next cron fire will take over)
                        <?php else : ?>
                            <span style="color:#666;">● Idle</span>
                            — waits for next cron fire or the Kick button below
                        <?php endif; ?>
                    </td>
                </tr>
                <tr>
                    <th>Back-off</th>
                    <td>
                        <?php if ( $backoff_secs > 0 ) : ?>
                            <span style="color:#a00;">Active</span> — pauses sweeps for <?php echo esc_html( $backoff_secs ); ?> more seconds.
                            Typically set when SPN reports <code>available=0</code>, when this
                            site's own onion is not answering over Tor, or when SPN will not
                            accept the Save Page Now credentials. The last one is the case to
                            act on: check the Archive.org S3 keys, then press
                            <em>Kick the daemon now</em> to clear this immediately.
                            The exact cause is on the most recent <code>Sweep:</code> log
                            line, as <code>spn-status=…</code>.
                        <?php else : ?>
                            <span style="color:#008000;">None</span>
                        <?php endif; ?>
                    </td>
                </tr>
                <tr>
                    <th>Next scheduled cron</th>
                    <td>
                        <?php if ( $next_cron ) :
                            // human_time_diff() is direction-agnostic, so an
                            // overdue event reads identically to a future one
                            // — confusing when the site has had no traffic
                            // for a while (WP-Cron only fires on web hits).
                            // Disambiguate explicitly.
                            $diff_label = human_time_diff( time(), $next_cron );
                            $is_overdue = ( $next_cron < time() );
                            ?>
                            <?php if ( $is_overdue ) : ?>
                                <strong>Overdue</strong> by <?php echo esc_html( $diff_label ); ?>
                                <small style="color:#666;">(scheduled for <?php echo esc_html( date( 'Y-m-d H:i:s', $next_cron ) ); ?>; will fire on the next page load — WP-Cron requires HTTP traffic to tick)</small>
                            <?php else : ?>
                                in <?php echo esc_html( $diff_label ); ?>
                                (<?php echo esc_html( date( 'H:i:s', $next_cron ) ); ?>)
                            <?php endif; ?>
                        <?php else : ?>
                            <em>Not scheduled — will be re-registered on next page load.</em>
                        <?php endif; ?>
                    </td>
                </tr>
            </tbody>
        </table>

        <h2>Actions</h2>
        <form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="display:inline-block; margin-right:8px;">
            <?php wp_nonce_field( 'onionpress_wayback_action' ); ?>
            <input type="hidden" name="action" value="onionpress_wayback_action">
            <input type="hidden" name="op_action" value="kick">
            <?php submit_button( 'Kick the daemon now', 'primary', 'submit', false ); ?>
        </form>
        <form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="display:inline-block; margin-right:8px;">
            <?php wp_nonce_field( 'onionpress_wayback_action' ); ?>
            <input type="hidden" name="action" value="onionpress_wayback_action">
            <input type="hidden" name="op_action" value="clear_backoff">
            <?php submit_button( 'Clear backoff', 'secondary', 'submit', false ); ?>
        </form>
        <form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="display:inline-block;">
            <?php wp_nonce_field( 'onionpress_wayback_action' ); ?>
            <input type="hidden" name="action" value="onionpress_wayback_action">
            <input type="hidden" name="op_action" value="clear_lock">
            <?php submit_button( 'Clear lock (force takeover)', 'secondary', 'submit', false ); ?>
        </form>

        <h2>How it works</h2>
        <p>A background daemon runs continuously, batching up to 40 post URLs per iteration
            through Save Page Now. Each submission returns a <code>job_id</code>;
            a subsequent poll tells us <code>success</code> (capture made),
            <code>pending</code> (SPN still crawling) or <code>error</code> (SPN crawler
            couldn't reach the URL).</p>
        <p>On <code>error:no-captures</code> the daemon falls back to CDX (Wayback's own
            index): if Wayback already has the URL captured (possibly from a prior attempt),
            the post is marked archived without resubmission. Otherwise the post is requeued
            for the next pass.</p>
        <p>Outgoing SPN traffic is routed through a separate Tor instance
            (<code>onionheaven</code>) so that heavy archival bursts don't starve the Tor
            daemon serving your onion. The <strong>heartbeat</strong> to OnionHeaven keeps your
            address registered as <em>online</em>; if heartbeats lapse for too long,
            OnionHeaven activates a <strong>takeover redirector</strong> at your address — so
            visitors get a polite "this site is offline, try again later" page instead of a
            timeout. The moment your heartbeat resumes, the takeover lifts and your real
            WordPress serves again.</p>
        <p>If the daemon dies (crash, reboot, Mac sleep), the mutex lock goes stale after
            <?php echo esc_html( round( OP_WB_LOOP_LOCK_STALE_SEC / 60 ) ); ?> minutes and
            the next WordPress page view causes wp-cron to restart the daemon
            from the persisted cursor. No progress is lost; no duplicates are created.
            The threshold has to clear the slowest thing a live daemon can be doing between
            heartbeats — a single Tor request to a cold onion service takes up to
            <?php echo esc_html( OP_WB_REQUEST_TIMEOUT_SEC ); ?> seconds — so it is generous
            on purpose: declaring a working daemon dead is worse than waiting, because two
            daemons would then archive the same pages twice.</p>
    </div>
    <?php
}
