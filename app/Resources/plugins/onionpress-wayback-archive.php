<?php
/**
 * Plugin Name: OnionPress Wayback Archive
 * Description: Automatically archives published posts and the homepage to the
 *              Internet Archive Wayback Machine.
 * Version:     1.4
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Log a Wayback message to both PHP error_log and the persistent host log.
 */
function onionpress_wayback_log( $msg ) {
    $line = '[OnionPress Wayback] ' . $msg;
    error_log( $line );
    $ts   = gmdate( 'Y-m-d H:i:s' );
    @file_put_contents(
        '/var/lib/onionpress/wayback.log',
        '[' . $ts . '] ' . $msg . "\n",
        FILE_APPEND | LOCK_EX
    );
}

// OnionPress version — read once from the shared volume, cached per request.
function onionpress_version() {
    static $ver = null;
    if ( $ver === null ) {
        $f = '/var/lib/onionpress/version';
        $ver = file_exists( $f ) ? trim( file_get_contents( $f ) ) : 'unknown';
    }
    return $ver;
}

/**
 * Get the archive.org S3 API authorization header, if credentials are configured.
 *
 * Returns "LOW access:secret" string or empty string if not configured.
 * Reads from wp_options (set during OnionPress setup).
 */
function onionpress_wayback_auth_header() {
    static $header = null;
    if ( $header !== null ) {
        return $header;
    }

    // Use main site options (blog 1) for network-wide credentials
    $access = get_blog_option( 1, 'onionpress_archive_s3_access', '' );
    $secret = get_blog_option( 1, 'onionpress_archive_s3_secret', '' );

    if ( $access && $secret ) {
        $header = 'LOW ' . $access . ':' . $secret;
    } else {
        $header = '';
    }

    return $header;
}


/**
 * Archive to the Wayback Machine when a post or page is published/updated.
 *
 * URL strategy: trust WordPress. `get_permalink()` and `home_url()` already
 * return the correct URL for wherever the post lives — on a subsite
 * (/alice/...) the paths come back prefixed; on a single-site install they
 * don't. We just swap the returned host for the .onion address when we have
 * one, since WordPress may have been configured with a clearnet siteurl.
 */
add_action( 'save_post', function ( $post_id, $post, $update ) {
    // Skip autosaves and revisions
    if ( defined( 'DOING_AUTOSAVE' ) && DOING_AUTOSAVE ) {
        return;
    }
    if ( wp_is_post_revision( $post_id ) ) {
        return;
    }

    // Only archive published posts and pages
    if ( $post->post_status !== 'publish' ) {
        return;
    }
    if ( ! in_array( $post->post_type, array( 'post', 'page' ), true ) ) {
        return;
    }

    $post_path = wp_parse_url( get_permalink( $post_id ), PHP_URL_PATH ) ?: '/';
    $home_path = wp_parse_url( home_url( '/' ), PHP_URL_PATH ) ?: '/';
    $feed_path = rtrim( $home_path, '/' ) . '/feed/';

    // Read the .onion address from the shared volume
    $onion_file = '/var/lib/onionpress/onion_address';
    if ( ! file_exists( $onion_file ) ) {
        // Tor not ready — queue the path for later; the drain resolver
        // prepends the .onion address when it fires.
        onionpress_wayback_queue_path( $post_path, $home_path );
        return;
    }
    $onion_addr = trim( file_get_contents( $onion_file ) );
    if ( empty( $onion_addr ) ) {
        onionpress_wayback_queue_path( $post_path, $home_path );
        return;
    }

    $urls = array_unique( array(
        'http://' . $onion_addr . $post_path,
        'http://' . $onion_addr . $home_path,
        'http://' . $onion_addr . $feed_path,
    ) );

    onionpress_wayback_queue_urls( $urls );
}, 10, 3 );

/**
 * Submit a URL to the Wayback Machine Save Page Now API.
 *
 * Uses PHP curl directly (not wp_remote_post) because WordPress HTTP API
 * does not support SOCKS5 proxies.
 *
 * Tries each endpoint in order; stops on first success.
 * Returns:
 *   array('status' => 'ok',       'job_id' => '<spn2-…>')  — submission accepted
 *   array('status' => 'cooldown')                           — SPN said "same snapshot"
 *   array('status' => 'failed')                             — all endpoints rejected
 */
function onionpress_wayback_submit( $endpoints, $url, $auth = '' ) {
    if ( ! function_exists( 'curl_init' ) ) {
        onionpress_wayback_log( 'curl extension not available' );
        return false;
    }

    $user_agent = 'OnionPress/' . onionpress_version() . ' (+https://github.com/brewsterkahle/onionpress)';

    foreach ( $endpoints as $ep ) {
        onionpress_wayback_log( 'Archiving: ' . $url . ' via ' . $ep['url'] . ( $auth ? ' (authenticated)' : ' (no auth)' ) );

        $headers = array( 'Accept: application/json' );
        if ( $auth ) {
            $headers[] = 'Authorization: ' . $auth;
        }

        $ch = curl_init();
        $opts = array(
            CURLOPT_URL            => $ep['url'],
            CURLOPT_POST           => true,
            CURLOPT_POSTFIELDS     => http_build_query( array( 'url' => $url ) ),
            CURLOPT_RETURNTRANSFER => true,
            CURLOPT_TIMEOUT        => 30,
            CURLOPT_CONNECTTIMEOUT => 15,
            CURLOPT_FOLLOWLOCATION => true,
            CURLOPT_UNRESTRICTED_AUTH => true,  // Keep Authorization header across redirects (.onion 307)
            CURLOPT_MAXREDIRS      => 3,
            CURLOPT_USERAGENT      => $user_agent,
            CURLOPT_HTTPHEADER     => $headers,
            // .onion HTTPS uses self-signed certs; safe because Tor provides
            // end-to-end encryption already.
            CURLOPT_SSL_VERIFYPEER => false,
            CURLOPT_SSL_VERIFYHOST => 0,
        );

        if ( $ep['proxy'] ) {
            $opts[ CURLOPT_PROXY ]     = $ep['proxy'];
            $opts[ CURLOPT_PROXYTYPE ] = CURLPROXY_SOCKS5_HOSTNAME;
        }

        curl_setopt_array( $ch, $opts );

        $response  = curl_exec( $ch );
        $http_code = curl_getinfo( $ch, CURLINFO_HTTP_CODE );
        $err       = curl_error( $ch );
        curl_close( $ch );

        if ( $err ) {
            onionpress_wayback_log( 'Curl error for ' . $url . ' via ' . $ep['url'] . ': ' . $err );
            continue; // Try next endpoint
        }

        onionpress_wayback_log( 'Submitted ' . $url . ' — HTTP ' . $http_code . ' — ' . substr( $response, 0, 500 ) );

        // Check for auth failure
        if ( $http_code === 401 || $http_code === 403 ) {
            $msg = @json_decode( $response, true );
            $reason = isset( $msg['message'] ) ? $msg['message'] : 'authentication required';
            onionpress_wayback_log( 'Auth failed: ' . $reason . ' — trying next endpoint' );
            continue;
        }

        // Rate-limited — do NOT treat as success; queue for retry
        if ( $http_code === 429 ) {
            onionpress_wayback_log( 'Rate-limited (HTTP 429) for ' . $url . ' — will queue for retry' );
            return array( 'status' => 'failed' );
        }

        // Any 2xx/3xx response means the endpoint accepted it
        if ( $http_code >= 200 && $http_code < 400 ) {
            $body = @json_decode( $response, true );
            // Detect SPN dedup cooldown: "The same snapshot had been made X ago"
            if ( is_array( $body ) && isset( $body['message'] ) && strpos( $body['message'], 'same snapshot' ) !== false ) {
                onionpress_wayback_log( 'SPN cooldown for ' . $url . ' — will retry in ~65 minutes' );
                return array( 'status' => 'cooldown' );
            }
            $job_id = ( is_array( $body ) && ! empty( $body['job_id'] ) ) ? $body['job_id'] : '';
            return array( 'status' => 'ok', 'job_id' => $job_id );
        }

        // 4xx client error (other than auth/rate-limit) — log and try next
        if ( $http_code >= 400 && $http_code < 500 ) {
            onionpress_wayback_log( 'Client error (HTTP ' . $http_code . ') for ' . $url . ', trying next endpoint' );
            continue;
        }

        // 5xx: server error, try next endpoint
        onionpress_wayback_log( 'Server error (HTTP ' . $http_code . '), trying next endpoint' );
    }

    return array( 'status' => 'failed' ); // All endpoints failed
}

/**
 * Poll SPN's /save/status/<job_id> endpoint through Tor.
 *
 * Returns one of:
 *   array('state' => 'success',   'timestamp' => '20260419010526')
 *   array('state' => 'pending')                                         (still crawling)
 *   array('state' => 'error',     'ext' => 'error:no-captures', 'message' => '…')
 *   array('state' => 'unknown',   'message' => '<why>')                 (network / parse failure)
 */
function onionpress_wayback_poll_status( $job_id ) {
    if ( ! function_exists( 'curl_init' ) ) {
        return array( 'state' => 'unknown', 'message' => 'no curl' );
    }
    $ch = curl_init();
    curl_setopt_array( $ch, array(
        CURLOPT_URL            => 'https://web.archive.org/save/status/' . $job_id,
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
    $http_code = curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    $err       = curl_error( $ch );
    curl_close( $ch );

    if ( $err || $http_code !== 200 || ! $response ) {
        return array( 'state' => 'unknown', 'message' => $err ?: ( 'HTTP ' . $http_code ) );
    }
    $body = @json_decode( $response, true );
    if ( ! is_array( $body ) || empty( $body['status'] ) ) {
        return array( 'state' => 'unknown', 'message' => 'malformed SPN response' );
    }
    if ( $body['status'] === 'success' ) {
        return array( 'state' => 'success', 'timestamp' => $body['timestamp'] ?? '' );
    }
    if ( $body['status'] === 'error' ) {
        return array(
            'state'   => 'error',
            'ext'     => $body['status_ext'] ?? 'error',
            'message' => $body['message'] ?? '',
        );
    }
    // pending / running / anything else
    return array( 'state' => 'pending' );
}

/**
 * Queue URLs for later Wayback archiving (deduplicated by URL).
 *
 * The menubar app polls this file and drains it when the onion service
 * is reachable (purple state).
 */
function onionpress_wayback_queue_urls( $urls, $retry_after = '' ) {
    $queue_file = '/var/lib/onionpress/wayback-queue.json';

    // Read existing queue
    $queue = array();
    if ( file_exists( $queue_file ) ) {
        $data = @file_get_contents( $queue_file );
        if ( $data ) {
            $decoded = json_decode( $data, true );
            if ( is_array( $decoded ) ) {
                $queue = $decoded;
            }
        }
    }

    // Build set of existing URLs for dedup
    $existing = array();
    foreach ( $queue as $item ) {
        if ( isset( $item['url'] ) ) {
            $existing[ $item['url'] ] = true;
        }
    }

    // Add new URLs (deduplicated)
    $now = gmdate( 'Y-m-d\TH:i:s\Z' );
    foreach ( $urls as $url ) {
        $entry = array( 'url' => $url, 'queued_at' => $now );
        if ( $retry_after ) {
            $entry['retry_after'] = $retry_after;
        }

        if ( isset( $existing[ $url ] ) ) {
            // Update timestamp and retry_after for existing entry
            foreach ( $queue as &$item ) {
                if ( $item['url'] === $url ) {
                    $item['queued_at'] = $now;
                    if ( $retry_after ) {
                        $item['retry_after'] = $retry_after;
                    }
                    break;
                }
            }
            unset( $item );
        } else {
            $queue[] = $entry;
            $existing[ $url ] = true;
        }
    }

    @file_put_contents( $queue_file, json_encode( $queue ) );
    onionpress_wayback_log( 'Queued ' . count( $urls ) . ' URL(s) for later archiving (' . count( $queue ) . ' total in queue)' );
}

/**
 * Queue a post path for later archiving (when onion address becomes available).
 *
 * Stores the path and the subsite path; the wp_cron drain handler will resolve
 * it to full .onion (and clearnet) URLs once the onion_address file appears.
 */
function onionpress_wayback_queue_path( $path, $site_path = '/' ) {
    $queue_file = '/var/lib/onionpress/wayback-queue.json';

    $queue = array();
    if ( file_exists( $queue_file ) ) {
        $data = @file_get_contents( $queue_file );
        if ( $data ) {
            $decoded = json_decode( $data, true );
            if ( is_array( $decoded ) ) {
                $queue = $decoded;
            }
        }
    }

    // Check for duplicate path
    foreach ( $queue as $item ) {
        if ( isset( $item['path'] ) && $item['path'] === $path ) {
            return; // Already queued
        }
    }

    $queue[] = array(
        'path'      => $path,
        'site_path' => $site_path,
        'queued_at' => gmdate( 'Y-m-d\TH:i:s\Z' ),
    );
    @file_put_contents( $queue_file, json_encode( $queue ) );
    onionpress_wayback_log( 'Queued path ' . $path . ' for archiving once Tor is ready' );
}

// ── wp_cron queue drain ──────────────────────────────────────────────

/**
 * Register a 5-minute cron schedule for queue draining.
 */
add_filter( 'cron_schedules', function ( $schedules ) {
    $schedules['onionpress_every_5_minutes'] = array(
        'interval' => 300,
        'display'  => 'Every 5 minutes',
    );
    return $schedules;
} );

/**
 * Ensure the drain event is scheduled.
 */
add_action( 'init', function () {
    if ( ! wp_next_scheduled( 'onionpress_drain_wayback_queue' ) ) {
        wp_schedule_event( time(), 'onionpress_every_5_minutes', 'onionpress_drain_wayback_queue' );
    }
} );

/**
 * Drain one item from the Wayback queue.
 *
 * Processes one URL (or path) per run to stay within SPN rate limits.
 * Runs every 5 minutes via wp_cron — works on both macOS and Linux.
 */
add_action( 'onionpress_drain_wayback_queue', function () {
    $queue_file = '/var/lib/onionpress/wayback-queue.json';

    if ( ! file_exists( $queue_file ) ) {
        return;
    }

    $data = @file_get_contents( $queue_file );
    if ( ! $data ) {
        return;
    }

    $queue = json_decode( $data, true );
    if ( ! is_array( $queue ) || empty( $queue ) ) {
        return;
    }

    // Find the first item that's ready to process (retry_after has passed)
    $now_ts = time();
    $item   = null;
    $idx    = null;
    foreach ( $queue as $i => $candidate ) {
        if ( isset( $candidate['retry_after'] ) ) {
            $retry_ts = strtotime( $candidate['retry_after'] );
            if ( $retry_ts && $retry_ts > $now_ts ) {
                continue; // Not ready yet
            }
        }
        $item = $candidate;
        $idx  = $i;
        break;
    }

    if ( $item === null ) {
        return; // All items are waiting for cooldown
    }

    // Remove the item from its position (might not be index 0)
    array_splice( $queue, $idx, 1 );

    // Handle path-only items (queued before onion address was available)
    if ( isset( $item['path'] ) && ! isset( $item['url'] ) ) {
        $onion_file = '/var/lib/onionpress/onion_address';
        if ( ! file_exists( $onion_file ) ) {
            return; // Still no onion address — try again next cycle
        }
        $onion_addr = trim( file_get_contents( $onion_file ) );
        if ( empty( $onion_addr ) ) {
            return;
        }

        // Resolve path to full URLs and re-queue them
        $path      = $item['path'];
        $site_path = isset( $item['site_path'] ) && ! empty( $item['site_path'] ) ? $item['site_path'] : '/';
        $home_path = $site_path;
        $feed_path = rtrim( $site_path, '/' ) . '/feed/';

        $urls = array( 'http://' . $onion_addr . $path );

        // Also queue subsite homepage and feed if the path isn't already one of them
        if ( $path !== $home_path ) {
            $urls[] = 'http://' . $onion_addr . $home_path;
        }
        $urls[] = 'http://' . $onion_addr . $feed_path;

        // Add clearnet URLs if available
        $clearnet_file = '/var/lib/onionpress/clearnet_domain';
        if ( file_exists( $clearnet_file ) ) {
            $clearnet_domain = trim( file_get_contents( $clearnet_file ) );
            if ( ! empty( $clearnet_domain ) ) {
                $urls[] = 'https://' . $clearnet_domain . $path;
                if ( $path !== $home_path ) {
                    $urls[] = 'https://' . $clearnet_domain . $home_path;
                }
                $urls[] = 'https://' . $clearnet_domain . $feed_path;
            }
        }

        // Save queue (item already removed), then queue the resolved URLs
        @file_put_contents( $queue_file, json_encode( array_values( $queue ) ) );
        onionpress_wayback_queue_urls( array_unique( $urls ) );

        onionpress_wayback_log( 'Resolved path ' . $path . ' to ' . count( $urls ) . ' URL(s)' );
        return;
    }

    // Handle normal URL items
    $url = isset( $item['url'] ) ? $item['url'] : '';
    if ( empty( $url ) ) {
        // Invalid item — already removed, just save
        @file_put_contents( $queue_file, json_encode( array_values( $queue ) ) );
        return;
    }

    // ── State 2: already submitted, poll SPN for the job's outcome ──
    //
    // SPN accepts the POST quickly and returns a job_id, then crawls the
    // URL over Tor in the background. That crawl can succeed, fail with
    // error:no-captures ("URL unreachable"), or stay pending for
    // minutes. We can't know until we poll /save/status/<job_id>.
    if ( ! empty( $item['job_id'] ) ) {
        $job_id = $item['job_id'];
        $status = onionpress_wayback_poll_status( $job_id );
        if ( $status['state'] === 'success' ) {
            onionpress_wayback_log( 'Queue drain: archived ' . $url
                . ' (job ' . $job_id . ', ts ' . $status['timestamp'] . ')' );
            // item already spliced out; just persist
            @file_put_contents( $queue_file, json_encode( array_values( $queue ) ) );
            return;
        }
        if ( $status['state'] === 'error' ) {
            $retry_count = (int) ( $item['retry_count'] ?? 0 ) + 1;
            $max_retries = 3;
            onionpress_wayback_log( 'Queue drain: SPN crawl failed for ' . $url
                . ' (job ' . $job_id . ', ' . $status['ext'] . '): '
                . $status['message'] . ' — attempt ' . $retry_count . '/' . $max_retries );
            if ( $retry_count >= $max_retries ) {
                onionpress_wayback_log( 'Queue drain: giving up on ' . $url
                    . ' after ' . $max_retries . ' SPN crawl failures' );
                @file_put_contents( $queue_file, json_encode( array_values( $queue ) ) );
                return;
            }
            // Strip job_id so the next drain tick resubmits fresh. Wait past
            // the SPN 65-minute cache window so SPN won't just return the
            // same failed job_id.
            unset( $item['job_id'] );
            $item['retry_count'] = $retry_count;
            $item['retry_after'] = gmdate( 'Y-m-d\TH:i:s\Z', time() + 3900 );
            $queue[] = $item;
            @file_put_contents( $queue_file, json_encode( array_values( $queue ) ) );
            return;
        }
        // pending / unknown — give SPN more time. Put the item back at the
        // end with a short check_after so we come back after other work.
        $item['retry_after'] = gmdate( 'Y-m-d\TH:i:s\Z', time() + 60 );
        $queue[] = $item;
        @file_put_contents( $queue_file, json_encode( array_values( $queue ) ) );
        return;
    }

    // ── State 1: not yet submitted — POST to SPN, store the job_id ──
    $endpoints = array(
        array(
            'url'   => 'http://web.archivep75mbjunhxc6x4j5mwjmomyxb573v42baldlqu56ruil2oiad.onion/save',
            'proxy' => 'socks5h://onionpress-tor:9050',
        ),
        array(
            'url'   => 'https://web.archive.org/save',
            'proxy' => 'socks5h://onionpress-tor:9050',
        ),
    );

    $auth = onionpress_wayback_auth_header();
    $result = onionpress_wayback_submit( $endpoints, $url, $auth );

    if ( $result['status'] === 'ok' ) {
        // Submission accepted. Mark with job_id + check_after so a future
        // drain tick polls /save/status/. Keep retry_count untouched.
        $item['job_id']      = $result['job_id'];
        $item['retry_after'] = gmdate( 'Y-m-d\TH:i:s\Z', time() + 60 );
        $queue[] = $item;
        @file_put_contents( $queue_file, json_encode( array_values( $queue ) ) );
        return;
    }

    if ( $result['status'] === 'cooldown' ) {
        // Re-queue at end with 65-minute delay (SPN's own dedup window).
        $item['retry_after'] = gmdate( 'Y-m-d\TH:i:s\Z', time() + 3900 );
        $queue[] = $item;
        @file_put_contents( $queue_file, json_encode( array_values( $queue ) ) );
        return;
    }

    onionpress_wayback_log( 'Queue drain: submit failed for ' . $url . ' — will retry next cycle' );
    $queue[] = $item;
    @file_put_contents( $queue_file, json_encode( array_values( $queue ) ) );
} );
