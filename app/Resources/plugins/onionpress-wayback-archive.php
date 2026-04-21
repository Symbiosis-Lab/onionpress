<?php
/**
 * Plugin Name: OnionPress Wayback Archive
 * Description: Continuously archives the site's posts, home page, and RSS
 *              feed to the Internet Archive's Wayback Machine. Per-post
 *              archive state lives in wp_postmeta (queryable, backed up
 *              with the DB, restored on migration). Home + feed state
 *              lives in wp_options. A scheduled sweep walks anything
 *              not-yet-archived and advances each URL through a three-
 *              state SPN pipeline (submit → poll → success|retry|give-up).
 * Version:     3.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

// ───────────────────────────── tunables ─────────────────────────────

if ( ! defined( 'ONIONPRESS_WAYBACK_MAX_RETRIES' ) ) {
    // Max SPN crawl failures per URL before we give up. Each retry waits
    // 65 min (past SPN's dedup cache), so 20 ≈ 22 h of trying.
    define( 'ONIONPRESS_WAYBACK_MAX_RETRIES', 20 );
}
if ( ! defined( 'ONIONPRESS_WAYBACK_RETRY_INTERVAL' ) ) {
    // Seconds to wait after an SPN error before resubmitting. Must be
    // > SPN's ~60 min dedup cache or resubmission returns the same
    // failed job_id.
    define( 'ONIONPRESS_WAYBACK_RETRY_INTERVAL', 3900 );
}
if ( ! defined( 'ONIONPRESS_WAYBACK_POLL_INTERVAL' ) ) {
    // Seconds between /save/status/<job> polls while a job is pending.
    define( 'ONIONPRESS_WAYBACK_POLL_INTERVAL', 60 );
}
if ( ! defined( 'ONIONPRESS_WAYBACK_URLS_PER_SWEEP' ) ) {
    // Safety-net count cap per sweep tick. The real guardrail is the
    // wall-clock budget below — this just prevents runaway on a blog
    // with thousands of posts. 25 comfortably covers catchup for a
    // normal-sized blog without breaching the time budget.
    define( 'ONIONPRESS_WAYBACK_URLS_PER_SWEEP', 25 );
}
if ( ! defined( 'ONIONPRESS_WAYBACK_SWEEP_TIME_BUDGET' ) ) {
    // Seconds a single sweep tick may spend before yielding to the next
    // cron firing. Each URL is a Tor round-trip (CDX check + submit or
    // poll), typically 5-30s. 90s comfortably fits 3-20 URLs depending
    // on Tor health, well under PHP's default max_execution_time of 30
    // (we run inside wp-cron which uses its own longer timeout).
    define( 'ONIONPRESS_WAYBACK_SWEEP_TIME_BUDGET', 90 );
}

// Post-meta keys (leading underscore hides them from the edit-post UI).
define( 'OP_WB_META_ARCHIVED_AT', '_op_wayback_archived_at' );
define( 'OP_WB_META_SNAPSHOT_TS', '_op_wayback_snapshot_ts' );
define( 'OP_WB_META_JOB_ID',      '_op_wayback_job_id' );
define( 'OP_WB_META_RETRY_COUNT', '_op_wayback_retry_count' );
define( 'OP_WB_META_RETRY_AFTER', '_op_wayback_retry_after' );
define( 'OP_WB_META_FAILED_AT',   '_op_wayback_failed_at' );
define( 'OP_WB_META_FAILED_REASON', '_op_wayback_failed_reason' );

// wp_options keys for home + feed (URLs not tied to a post).
define( 'OP_WB_OPT_HOME', 'op_wayback_home_state' );
define( 'OP_WB_OPT_FEED', 'op_wayback_feed_state' );

// ─────────────────────────── logging + helpers ──────────────────────

function onionpress_wayback_log( $msg ) {
    // Apache error log → container stderr → container-wordpress-*.log on
    // the host (rotated, gzipped, shipped via analytics). No need for a
    // separate file on disk, which would grow unbounded.
    error_log( '[OnionPress Wayback] ' . $msg );
}

function onionpress_version() {
    static $ver = null;
    if ( $ver === null ) {
        $f = '/var/lib/onionpress/version';
        $ver = file_exists( $f ) ? trim( @file_get_contents( $f ) ) : 'dev';
    }
    return $ver;
}

/**
 * Read archive.org S3 credentials from wp_options. Empty string if not
 * configured — we still try without auth (public SPN).
 */
function onionpress_wayback_auth_header() {
    $access = get_blog_option( 1, 'onionpress_archive_s3_access', '' );
    $secret = get_blog_option( 1, 'onionpress_archive_s3_secret', '' );
    if ( empty( $access ) || empty( $secret ) ) {
        return '';
    }
    return 'LOW ' . $access . ':' . $secret;
}

/**
 * Read this blog's .onion address from the shared volume. Empty string if
 * Tor isn't up yet.
 */
function onionpress_wayback_onion_addr() {
    $f = '/var/lib/onionpress/onion_address';
    if ( ! file_exists( $f ) ) {
        return '';
    }
    return trim( (string) @file_get_contents( $f ) );
}

/**
 * Build the .onion URL for a post (trusts get_permalink's path).
 */
function onionpress_wayback_post_url( $post_id ) {
    $onion = onionpress_wayback_onion_addr();
    if ( empty( $onion ) ) {
        return '';
    }
    $path = wp_parse_url( get_permalink( $post_id ), PHP_URL_PATH );
    if ( ! $path ) {
        return '';
    }
    return 'http://' . $onion . $path;
}

function onionpress_wayback_home_url_full() {
    $onion = onionpress_wayback_onion_addr();
    if ( empty( $onion ) ) {
        return '';
    }
    $path = wp_parse_url( home_url( '/' ), PHP_URL_PATH ) ?: '/';
    return 'http://' . $onion . $path;
}

function onionpress_wayback_feed_url_full() {
    $onion = onionpress_wayback_onion_addr();
    if ( empty( $onion ) ) {
        return '';
    }
    $path = wp_parse_url( home_url( '/' ), PHP_URL_PATH ) ?: '/';
    return 'http://' . $onion . rtrim( $path, '/' ) . '/feed/';
}

// ──────────────────────────── SPN calls ─────────────────────────────

/**
 * Submit a URL to SPN. Tries the .onion endpoint first, falls back to
 * clearnet. Both go through onionpress-tor:9050.
 *
 * Returns:
 *   ['status' => 'ok', 'job_id' => 'spn2-…']
 *   ['status' => 'cooldown']   — SPN returned "same snapshot"
 *   ['status' => 'failed']     — all endpoints rejected us
 */
function onionpress_wayback_submit( $url ) {
    if ( ! function_exists( 'curl_init' ) ) {
        return array( 'status' => 'failed' );
    }
    $endpoints = array(
        'http://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/save',
        'https://web.archive.org/save',
    );
    $auth       = onionpress_wayback_auth_header();
    $user_agent = 'OnionPress/' . onionpress_version() . ' (+https://github.com/brewsterkahle/onionpress)';
    foreach ( $endpoints as $ep ) {
        onionpress_wayback_log( 'Submit: ' . $url . ' via ' . $ep
            . ( $auth ? ' (auth)' : ' (no auth)' ) );
        $headers = array( 'Accept: application/json' );
        if ( $auth ) {
            $headers[] = 'Authorization: ' . $auth;
        }
        $ch = curl_init();
        curl_setopt_array( $ch, array(
            CURLOPT_URL            => $ep,
            CURLOPT_POST           => true,
            CURLOPT_POSTFIELDS     => http_build_query( array( 'url' => $url ) ),
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT        => 30,
            CURLOPT_CONNECTTIMEOUT => 15,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_UNRESTRICTED_AUTH => true,
            CURLOPT_MAXREDIRS      => 3,
            CURLOPT_USERAGENT      => $user_agent,
            CURLOPT_HTTPHEADER     => $headers,
            CURLOPT_SSL_VERIFYPEER => false,
            CURLOPT_SSL_VERIFYHOST => 0,
            CURLOPT_PROXY          => 'socks5h://onionpress-tor:9050',
            CURLOPT_PROXYTYPE      => CURLPROXY_SOCKS5_HOSTNAME,
        ) );
        $response  = curl_exec( $ch );
        $http_code = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
        $err       = curl_error( $ch );
        curl_close( $ch );
        if ( $err ) {
            onionpress_wayback_log( 'Submit error: ' . $err );
            continue;
        }
        onionpress_wayback_log( 'Submit resp: HTTP ' . $http_code . ' — '
            . substr( (string) $response, 0, 300 ) );
        if ( $http_code === 401 || $http_code === 403 ) {
            continue; // next endpoint
        }
        if ( $http_code === 429 ) {
            return array( 'status' => 'failed' ); // rate-limited
        }
        if ( $http_code >= 200 && $http_code < 400 ) {
            $body = @json_decode( (string) $response, true );
            if ( is_array( $body ) && isset( $body['message'] )
                    && strpos( $body['message'], 'same snapshot' ) !== false ) {
                return array( 'status' => 'cooldown' );
            }
            $job_id = ( is_array( $body ) && ! empty( $body['job_id'] ) )
                ? $body['job_id'] : '';
            return array( 'status' => 'ok', 'job_id' => $job_id );
        }
    }
    return array( 'status' => 'failed' );
}

/**
 * Poll SPN for an in-flight job.
 *
 * Returns:
 *   ['state' => 'success', 'timestamp' => '20260419010526']
 *   ['state' => 'pending']
 *   ['state' => 'error', 'ext' => '…', 'message' => '…']
 *   ['state' => 'unknown', 'message' => '<why>']
 */
function onionpress_wayback_poll_status( $job_id ) {
    if ( ! function_exists( 'curl_init' ) ) {
        return array( 'state' => 'unknown', 'message' => 'no curl' );
    }
    $ch = curl_init( 'https://web.archive.org/save/status/' . $job_id );
    curl_setopt_array( $ch, array(
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 30,
        CURLOPT_CONNECTTIMEOUT => 15,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 3,
        CURLOPT_USERAGENT      => 'OnionPress/' . onionpress_version(),
        CURLOPT_PROXY          => 'socks5h://onionpress-tor:9050',
        CURLOPT_PROXYTYPE      => CURLPROXY_SOCKS5_HOSTNAME,
        CURLOPT_SSL_VERIFYPEER => false,
        CURLOPT_SSL_VERIFYHOST => 0,
    ) );
    $response  = curl_exec( $ch );
    $http_code = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    $err       = curl_error( $ch );
    curl_close( $ch );
    if ( $err || $http_code !== 200 || ! $response ) {
        return array( 'state' => 'unknown',
                      'message' => $err ?: ( 'HTTP ' . $http_code ) );
    }
    $body = @json_decode( (string) $response, true );
    if ( ! is_array( $body ) || empty( $body['status'] ) ) {
        return array( 'state' => 'unknown', 'message' => 'malformed SPN response' );
    }
    if ( $body['status'] === 'success' ) {
        return array( 'state'     => 'success',
                      'timestamp' => $body['timestamp'] ?? '' );
    }
    if ( $body['status'] === 'error' ) {
        return array(
            'state'   => 'error',
            'ext'     => $body['status_ext'] ?? 'error',
            'message' => $body['message'] ?? '',
        );
    }
    return array( 'state' => 'pending' );
}

/**
 * Ask CDX (the Wayback index) for the latest capture timestamp of a URL,
 * or an empty string if none. CDX is the ground truth — SPN's
 * /save/status/ endpoint has been observed to flip from "success" to
 * "error:no-captures" for the same job_id over time, while the capture
 * in CDX persists. Trust CDX over SPN status.
 */
function onionpress_wayback_cdx_latest( $url ) {
    if ( ! function_exists( 'curl_init' ) ) {
        return '';
    }
    $url_no_scheme = preg_replace( '#^https?://#', '', $url );
    $ch = curl_init( 'https://web.archive.org/cdx/search/cdx?'
        . 'url=' . urlencode( $url_no_scheme )
        . '&output=json&limit=1' );
    curl_setopt_array( $ch, array(
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 45,
        CURLOPT_CONNECTTIMEOUT => 15,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 3,
        CURLOPT_USERAGENT      => 'OnionPress/' . onionpress_version(),
        CURLOPT_PROXY          => 'socks5h://onionpress-tor:9050',
        CURLOPT_PROXYTYPE      => CURLPROXY_SOCKS5_HOSTNAME,
        CURLOPT_SSL_VERIFYPEER => false,
        CURLOPT_SSL_VERIFYHOST => 0,
    ) );
    $response  = curl_exec( $ch );
    $http_code = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    curl_close( $ch );
    if ( $http_code !== 200 || ! $response ) {
        return '';
    }
    $data = @json_decode( (string) $response, true );
    if ( ! is_array( $data ) || count( $data ) < 2 ) {
        return '';
    }
    // CDX JSON format: first row is header, subsequent rows are captures.
    // Row schema: urlkey, timestamp, original, mimetype, statuscode,
    //             digest, length. Timestamp is index 1.
    $last = end( $data );
    return is_array( $last ) && ! empty( $last[1] ) ? (string) $last[1] : '';
}

// ─────────────────────── state read/write helpers ───────────────────

/**
 * Read a post's wayback state as an associative array.
 */
function onionpress_wayback_post_state( $post_id ) {
    return array(
        'archived_at'   => (int) get_post_meta( $post_id, OP_WB_META_ARCHIVED_AT, true ),
        'snapshot_ts'   => (string) get_post_meta( $post_id, OP_WB_META_SNAPSHOT_TS, true ),
        'job_id'        => (string) get_post_meta( $post_id, OP_WB_META_JOB_ID, true ),
        'retry_count'   => (int) get_post_meta( $post_id, OP_WB_META_RETRY_COUNT, true ),
        'retry_after'   => (int) get_post_meta( $post_id, OP_WB_META_RETRY_AFTER, true ),
        'failed_at'     => (int) get_post_meta( $post_id, OP_WB_META_FAILED_AT, true ),
        'failed_reason' => (string) get_post_meta( $post_id, OP_WB_META_FAILED_REASON, true ),
    );
}

/**
 * Write a post's wayback state. Keys absent from $state are DELETED from
 * postmeta — callers pass the full state they want stored.
 */
function onionpress_wayback_post_state_set( $post_id, $state ) {
    $mapping = array(
        'archived_at'   => OP_WB_META_ARCHIVED_AT,
        'snapshot_ts'   => OP_WB_META_SNAPSHOT_TS,
        'job_id'        => OP_WB_META_JOB_ID,
        'retry_count'   => OP_WB_META_RETRY_COUNT,
        'retry_after'   => OP_WB_META_RETRY_AFTER,
        'failed_at'     => OP_WB_META_FAILED_AT,
        'failed_reason' => OP_WB_META_FAILED_REASON,
    );
    foreach ( $mapping as $key => $meta ) {
        if ( array_key_exists( $key, $state ) && $state[ $key ] !== '' && $state[ $key ] !== 0 ) {
            update_post_meta( $post_id, $meta, $state[ $key ] );
        } else {
            delete_post_meta( $post_id, $meta );
        }
    }
}

/**
 * Read the state for a non-post URL (home or feed) from wp_options.
 */
function onionpress_wayback_opt_state( $option_key ) {
    $raw = get_option( $option_key, array() );
    if ( ! is_array( $raw ) ) {
        $raw = array();
    }
    return array(
        'archived_at'   => (int) ( $raw['archived_at'] ?? 0 ),
        'snapshot_ts'   => (string) ( $raw['snapshot_ts'] ?? '' ),
        'job_id'        => (string) ( $raw['job_id'] ?? '' ),
        'retry_count'   => (int) ( $raw['retry_count'] ?? 0 ),
        'retry_after'   => (int) ( $raw['retry_after'] ?? 0 ),
        'failed_at'     => (int) ( $raw['failed_at'] ?? 0 ),
        'failed_reason' => (string) ( $raw['failed_reason'] ?? '' ),
    );
}

function onionpress_wayback_opt_state_set( $option_key, $state ) {
    update_option( $option_key, $state, false /* autoload */ );
}

// ─────────────────────────── state machine ──────────────────────────

/**
 * Advance one URL through the state machine, reading/writing state via
 * the provided callables. Returns the action label, for logging.
 */
function onionpress_wayback_advance( $url, callable $read, callable $write ) {
    $state = $read();

    if ( ! empty( $state['archived_at'] ) ) {
        return 'already-archived';
    }
    if ( ! empty( $state['failed_at'] ) ) {
        return 'given-up';
    }
    $now = time();
    if ( ! empty( $state['retry_after'] ) && $state['retry_after'] > $now ) {
        return 'waiting';
    }

    // State 2: in-flight job — poll SPN.
    if ( ! empty( $state['job_id'] ) ) {
        $status = onionpress_wayback_poll_status( $state['job_id'] );
        if ( $status['state'] === 'success' ) {
            $write( array(
                'archived_at' => $now,
                'snapshot_ts' => $status['timestamp'],
                'job_id'      => $state['job_id'],
            ) );
            onionpress_wayback_log( 'Archived ' . $url
                . ' (job ' . $state['job_id']
                . ', ts ' . $status['timestamp'] . ')' );
            return 'success';
        }
        if ( $status['state'] === 'error' ) {
            // SPN said "no capture" — but SPN's job-status memory is
            // unreliable (we've seen jobs flip success→error hours later
            // even when the actual capture persists in CDX). Before
            // bumping retry_count, ask CDX directly; if it has a capture
            // we're done, regardless of what SPN claims.
            $cdx_ts = onionpress_wayback_cdx_latest( $url );
            if ( ! empty( $cdx_ts ) ) {
                $write( array(
                    'archived_at' => $now,
                    'snapshot_ts' => $cdx_ts,
                ) );
                onionpress_wayback_log( 'CDX has capture for ' . $url
                    . ' (ts ' . $cdx_ts . ') despite SPN '
                    . $status['ext'] . ' — marking archived' );
                return 'cdx-hit';
            }
            $retry = (int) ( $state['retry_count'] ?? 0 ) + 1;
            $max   = ONIONPRESS_WAYBACK_MAX_RETRIES;
            if ( $retry >= $max ) {
                $write( array(
                    'failed_at'     => $now,
                    'failed_reason' => $status['ext'] . ': ' . $status['message'],
                    'retry_count'   => $retry,
                ) );
                onionpress_wayback_log( 'Giving up on ' . $url . ' after '
                    . $retry . ' SPN crawl failures: ' . $status['ext'] );
                return 'given-up';
            }
            $write( array(
                'retry_count' => $retry,
                'retry_after' => $now + ONIONPRESS_WAYBACK_RETRY_INTERVAL,
                // clear job_id so next sweep submits fresh
            ) );
            onionpress_wayback_log( 'SPN error for ' . $url
                . ' (' . $status['ext'] . '), will retry in '
                . round( ONIONPRESS_WAYBACK_RETRY_INTERVAL / 60 )
                . ' min (attempt ' . $retry . '/' . $max . ')' );
            return 'retry-scheduled';
        }
        // pending / unknown
        $write( array_merge( $state, array(
            'retry_after' => $now + ONIONPRESS_WAYBACK_POLL_INTERVAL,
        ) ) );
        return 'polling';
    }

    // State 1: no job_id — check CDX first (free dedup; also catches
    // URLs that SPN successfully archived on a previous machine or run
    // where we didn't track it). If already captured, skip submission.
    $cdx_ts = onionpress_wayback_cdx_latest( $url );
    if ( ! empty( $cdx_ts ) ) {
        $write( array(
            'archived_at' => $now,
            'snapshot_ts' => $cdx_ts,
        ) );
        onionpress_wayback_log( 'CDX has capture for ' . $url
            . ' (ts ' . $cdx_ts . ') — no submission needed' );
        return 'cdx-hit';
    }
    $result = onionpress_wayback_submit( $url );
    if ( $result['status'] === 'ok' && ! empty( $result['job_id'] ) ) {
        $write( array_merge( $state, array(
            'job_id'      => $result['job_id'],
            'retry_after' => $now + ONIONPRESS_WAYBACK_POLL_INTERVAL,
        ) ) );
        return 'submitted';
    }
    if ( $result['status'] === 'cooldown' ) {
        $write( array_merge( $state, array(
            'retry_after' => $now + ONIONPRESS_WAYBACK_RETRY_INTERVAL,
        ) ) );
        return 'cooldown';
    }
    // submission failed — retry in 5 min (next sweep tick)
    $write( array_merge( $state, array(
        'retry_after' => $now + 300,
    ) ) );
    return 'submit-failed';
}

// ───────────────────────── sweep (cron tick) ────────────────────────

/**
 * Every 5 minutes, pick up to N URLs that are neither archived nor given-
 * up-on, and advance each by one state machine step. The wp_postmeta
 * query lets the DB do the "what needs work" lookup — no JSON queue.
 */
function onionpress_wayback_sweep() {
    $onion = onionpress_wayback_onion_addr();
    if ( empty( $onion ) ) {
        onionpress_wayback_log( 'Sweep skipped: onion address not ready' );
        return;
    }

    $budget = ONIONPRESS_WAYBACK_URLS_PER_SWEEP;
    $deadline = microtime( true ) + ONIONPRESS_WAYBACK_SWEEP_TIME_BUDGET;
    $processed = 0;

    // 1) Home and feed first. These are one-shot catchup URLs: once each
    //    is successfully archived (or given up on), advance() short-
    //    circuits to 'already-archived' or 'given-up' without doing any
    //    work — they don't consume budget in subsequent ticks. Running
    //    them first avoids the starvation bug where a blog with many
    //    posts would never archive its homepage.
    $url_map = array();
    $home = onionpress_wayback_home_url_full();
    $feed = onionpress_wayback_feed_url_full();
    if ( $home ) { $url_map[ OP_WB_OPT_HOME ] = $home; }
    if ( $feed ) { $url_map[ OP_WB_OPT_FEED ] = $feed; }
    foreach ( $url_map as $opt_key => $url ) {
        $state = onionpress_wayback_opt_state( $opt_key );
        if ( ! empty( $state['archived_at'] ) || ! empty( $state['failed_at'] ) ) {
            continue; // no-op, doesn't count against budget
        }
        if ( ! empty( $state['retry_after'] ) && $state['retry_after'] > time() ) {
            continue; // waiting, doesn't count against budget
        }
        if ( $processed >= $budget || microtime( true ) >= $deadline ) {
            break;
        }
        $action = onionpress_wayback_advance(
            $url,
            function () use ( $opt_key ) { return onionpress_wayback_opt_state( $opt_key ); },
            function ( $state ) use ( $opt_key ) { onionpress_wayback_opt_state_set( $opt_key, $state ); }
        );
        onionpress_wayback_log( 'Sweep: ' . $opt_key . ' → ' . $action );
        $processed++;
    }

    // 2) Posts, in two priority queues:
    //
    //    Queue A (fresh): posts that have never been attempted
    //      (retry_count meta doesn't exist). Processed first so a
    //      just-published post always gets its shot before we spend
    //      budget on chronically-failing old posts.
    //
    //    Queue B (retrying): posts that have been attempted at least
    //      once but aren't archived or given up on. Processed with
    //      leftover budget, ordered oldest-attempt-first so nothing
    //      starves — the URL whose retry_after is furthest in the
    //      past has been waiting longest for another try.
    //
    //    Both queues exclude archived_at and failed_at. The in-PHP
    //    retry_after check below skips URLs still in their backoff
    //    window without counting against the budget.
    $queue_a = get_posts( array(
        'post_status' => 'publish',
        'post_type'   => array( 'post', 'page' ),
        'numberposts' => 100,
        'orderby'     => 'date',
        'order'       => 'DESC',
        'meta_query'  => array(
            'relation' => 'AND',
            array( 'key' => OP_WB_META_ARCHIVED_AT, 'compare' => 'NOT EXISTS' ),
            array( 'key' => OP_WB_META_FAILED_AT,   'compare' => 'NOT EXISTS' ),
            array( 'key' => OP_WB_META_RETRY_COUNT, 'compare' => 'NOT EXISTS' ),
        ),
        'suppress_filters' => false,
    ) );
    $queue_b = get_posts( array(
        'post_status' => 'publish',
        'post_type'   => array( 'post', 'page' ),
        'numberposts' => 100,
        'meta_key'    => OP_WB_META_RETRY_AFTER,
        'orderby'     => 'meta_value_num',
        'order'       => 'ASC',
        'meta_query'  => array(
            'relation' => 'AND',
            array( 'key' => OP_WB_META_ARCHIVED_AT, 'compare' => 'NOT EXISTS' ),
            array( 'key' => OP_WB_META_FAILED_AT,   'compare' => 'NOT EXISTS' ),
            array( 'key' => OP_WB_META_RETRY_COUNT, 'compare' => 'EXISTS' ),
        ),
        'suppress_filters' => false,
    ) );

    foreach ( array( $queue_a, $queue_b ) as $queue ) {
        foreach ( $queue as $post ) {
            if ( $processed >= $budget || microtime( true ) >= $deadline ) {
                break 2;
            }
            $url = onionpress_wayback_post_url( $post->ID );
            if ( empty( $url ) ) {
                continue;
            }
            $state = onionpress_wayback_post_state( $post->ID );
            if ( ! empty( $state['retry_after'] ) && $state['retry_after'] > time() ) {
                continue;
            }
            $action = onionpress_wayback_advance(
                $url,
                function () use ( $post ) { return onionpress_wayback_post_state( $post->ID ); },
                function ( $state ) use ( $post ) { onionpress_wayback_post_state_set( $post->ID, $state ); }
            );
            onionpress_wayback_log( 'Sweep: post ' . $post->ID . ' → ' . $action );
            $processed++;
        }
    }
}
add_action( 'onionpress_wayback_sweep', 'onionpress_wayback_sweep' );

/**
 * When a post is published (or updated), the home page and RSS feed now
 * list different content, so their previous Wayback snapshot is stale.
 * Invalidate their per-blog option state so the next sweep tick picks
 * them up and submits fresh captures. The post itself enters the sweep
 * naturally (it has no archived_at meta yet).
 *
 * Scoped to publish-status posts/pages only — autosaves, revisions,
 * trashed-and-restored actions don't count.
 */
add_action( 'save_post', function ( $post_id, $post, $update ) {
    if ( defined( 'DOING_AUTOSAVE' ) && DOING_AUTOSAVE ) {
        return;
    }
    if ( wp_is_post_revision( $post_id ) ) {
        return;
    }
    if ( $post->post_status !== 'publish' ) {
        return;
    }
    if ( ! in_array( $post->post_type, array( 'post', 'page' ), true ) ) {
        return;
    }
    delete_option( OP_WB_OPT_HOME );
    delete_option( OP_WB_OPT_FEED );
    // If the edited post was previously archived, re-archive it too —
    // its content may have materially changed since the old snapshot.
    if ( $update && get_post_meta( $post_id, OP_WB_META_ARCHIVED_AT, true ) ) {
        onionpress_wayback_post_state_set( $post_id, array() );
    }
    // Kick a sweep immediately so the new/updated post gets archived
    // without waiting for the next 5-min cron tick. The author may put
    // their laptop to sleep right after hitting Publish — we want SPN
    // to have received the submission before that happens. This single-
    // event cron queues behind whatever request is already in flight
    // (almost always the admin's own post-save redirect), so it runs
    // within a second or two of the publish.
    wp_schedule_single_event( time(), 'onionpress_wayback_sweep' );
    onionpress_wayback_log( 'save_post: post ' . $post_id . ' — cleared home/feed state'
        . ( $update ? ' and post meta' : '' ) . ', scheduled immediate sweep' );
}, 10, 3 );

// ────────────────────────────── cron ────────────────────────────────

add_filter( 'cron_schedules', function ( $schedules ) {
    $schedules['onionpress_every_5_minutes'] = array(
        'interval' => 300,
        'display'  => 'Every 5 minutes',
    );
    return $schedules;
} );

add_action( 'init', function () {
    if ( ! wp_next_scheduled( 'onionpress_wayback_sweep' ) ) {
        wp_schedule_event( time(), 'onionpress_every_5_minutes',
            'onionpress_wayback_sweep' );
    }
    // One-time cleanup: the 2.x plugin used JSON-file queue and ledger.
    // Remove them so users don't get confused when looking at the
    // filesystem and seeing stale state files.
    if ( get_option( 'op_wayback_v3_migrated' ) !== 'yes' ) {
        @unlink( '/var/lib/onionpress/wayback-queue.json' );
        @unlink( '/var/lib/onionpress/wayback-archived.json' );
        update_option( 'op_wayback_v3_migrated', 'yes' );
    }
    // Retire the legacy drain cron hook if it's still scheduled.
    $legacy_ts = wp_next_scheduled( 'onionpress_drain_wayback_queue' );
    if ( $legacy_ts ) {
        wp_unschedule_event( $legacy_ts, 'onionpress_drain_wayback_queue' );
    }
} );
