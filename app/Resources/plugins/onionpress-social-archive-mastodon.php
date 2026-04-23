<?php
/**
 * Plugin Name: OnionPress Social Archive — Mastodon Importer
 * Description: Pull a Mastodon account's public posts into the unified
 *              social_post archive via the public API. One handle, one
 *              click: backfills full history, then polls every few
 *              minutes to stay current. No takeout ZIP, no credentials.
 *              Safe to re-run — status IDs are idempotency keys.
 * Version:     0.1
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

add_action( 'plugins_loaded', function () {
    if ( function_exists( 'onionpress_social_register_importer' ) ) {
        onionpress_social_register_importer( 'mastodon' );
    }
} );

const ONIONPRESS_MASTODON_ADMIN_SLUG  = 'onionpress-social-archive-mastodon';
const ONIONPRESS_MASTODON_HANDLE_OPT  = 'onionpress_social_mastodon_handle';      // full "user@server" for card display
const ONIONPRESS_MASTODON_SERVER_OPT  = 'onionpress_social_mastodon_server';
const ONIONPRESS_MASTODON_USER_OPT    = 'onionpress_social_mastodon_username';
const ONIONPRESS_MASTODON_ACCT_ID_OPT = 'onionpress_social_mastodon_account_id';
const ONIONPRESS_MASTODON_OLDEST_OPT  = 'onionpress_social_mastodon_oldest_id';
const ONIONPRESS_MASTODON_NEWEST_OPT  = 'onionpress_social_mastodon_newest_id';
const ONIONPRESS_MASTODON_STATUSES_OPT = 'onionpress_social_mastodon_total_statuses';
const ONIONPRESS_MASTODON_LAST_SYNC   = 'onionpress_social_mastodon_last_sync';
const ONIONPRESS_MASTODON_LAST_NOTE   = 'onionpress_social_mastodon_last_note';
const ONIONPRESS_MASTODON_LOCK        = 'onionpress_social_mastodon_lock';
const ONIONPRESS_MASTODON_OPTS_OPT    = 'onionpress_social_mastodon_opts';
const ONIONPRESS_MASTODON_CRON_HOOK   = 'onionpress_social_mastodon_sync';

// Route everything through the bulk-outgoing Tor daemon in the onionheaven
// container, per the "separate Tor in/out" architecture. onionpress-tor is
// reserved for the hosted onion service.
const ONIONPRESS_MASTODON_SOCKS_PROXY = 'onionheaven:9050';

// Budget per tick. Most of the cost is media sideload (~3-5s per
// attachment over Tor); a strict wall-clock ceiling keeps the tick
// finishing cleanly regardless of how heavy the posts are. The page
// cap is a hard upper bound so a tick on a text-only account can't
// run the full history in one go either. 25s clears PHP's default 30s
// max_execution_time with a margin.
const ONIONPRESS_MASTODON_TICK_BUDGET_SEC = 25;
const ONIONPRESS_MASTODON_PAGES_PER_TICK  = 8;
const ONIONPRESS_MASTODON_PER_PAGE        = 40;
const ONIONPRESS_MASTODON_HTTP_TIMEOUT    = 45;

add_action( 'admin_menu', function () {
    if ( ! defined( 'ONIONPRESS_SOCIAL_ADMIN_SLUG' ) ) {
        return;
    }
    add_submenu_page(
        ONIONPRESS_SOCIAL_ADMIN_SLUG,
        'Import Mastodon',
        'Mastodon',
        'manage_options',
        ONIONPRESS_MASTODON_ADMIN_SLUG,
        'onionpress_mastodon_import_page'
    );
}, 20 );

/**
 * Schedule the recurring poll on first admin hit after a handle is
 * configured. mu-plugins have no activation hook, so we lazy-schedule.
 */
add_action( 'admin_init', function () {
    if ( ! wp_next_scheduled( ONIONPRESS_MASTODON_CRON_HOOK )
         && get_option( ONIONPRESS_MASTODON_ACCT_ID_OPT ) ) {
        wp_schedule_event( time() + 60, 'onionpress_mastodon_5min', ONIONPRESS_MASTODON_CRON_HOOK );
    }
} );

add_filter( 'cron_schedules', function ( $schedules ) {
    if ( ! isset( $schedules['onionpress_mastodon_5min'] ) ) {
        $schedules['onionpress_mastodon_5min'] = array(
            'interval' => 300,
            'display'  => '5 min (OnionPress Mastodon poll)',
        );
    }
    return $schedules;
} );

add_action( ONIONPRESS_MASTODON_CRON_HOOK, 'onionpress_mastodon_run_sync_tick' );

function onionpress_mastodon_import_page() {
    if ( ! current_user_can( 'manage_options' ) ) {
        wp_die( 'Unauthorized' );
    }

    $notice = null;
    if ( $_SERVER['REQUEST_METHOD'] === 'POST' ) {
        if ( isset( $_POST['onionpress_mastodon_save_handle'] ) ) {
            check_admin_referer( 'onionpress_mastodon_save_handle', 'onionpress_mastodon_handle_nonce' );
            $notice = onionpress_mastodon_handle_save_handle_post();
        } elseif ( isset( $_POST['onionpress_mastodon_sync_now'] ) ) {
            check_admin_referer( 'onionpress_mastodon_sync_now', 'onionpress_mastodon_sync_nonce' );
            $notice = onionpress_mastodon_run_sync_tick( true /* from_admin */ );
        } elseif ( isset( $_POST['onionpress_mastodon_reset'] ) ) {
            check_admin_referer( 'onionpress_mastodon_reset', 'onionpress_mastodon_reset_nonce' );
            $notice = onionpress_mastodon_reset_cursors();
        }
    }

    $handle        = (string) get_option( ONIONPRESS_MASTODON_HANDLE_OPT, '' );
    $server        = (string) get_option( ONIONPRESS_MASTODON_SERVER_OPT, '' );
    $account_id    = (string) get_option( ONIONPRESS_MASTODON_ACCT_ID_OPT, '' );
    $total         = (int) get_option( ONIONPRESS_MASTODON_STATUSES_OPT, 0 );
    $imported_now  = onionpress_social_count_for_source( 'mastodon' );
    $last_sync     = (int) get_option( ONIONPRESS_MASTODON_LAST_SYNC, 0 );
    $last_note     = (string) get_option( ONIONPRESS_MASTODON_LAST_NOTE, '' );
    $oldest_id     = (string) get_option( ONIONPRESS_MASTODON_OLDEST_OPT, '' );
    $opts          = (array) get_option( ONIONPRESS_MASTODON_OPTS_OPT, array() );

    $backfill_done = ( $account_id !== '' ) && ( $oldest_id === '' || $oldest_id === 'done' );

    ?>
    <div class="wrap">
        <h1>Import Mastodon</h1>

        <?php if ( $notice ) : ?>
            <div class="notice notice-<?php echo esc_attr( $notice['level'] ); ?>">
                <p><?php echo wp_kses_post( $notice['message'] ); ?></p>
            </div>
        <?php endif; ?>

        <h2>Step 1 &mdash; Your Mastodon address</h2>
        <p>Enter your full Mastodon address, including the server. Example: <code>@brewsterkahle@mastodon.archive.org</code>.</p>
        <form method="post" style="margin-bottom:1.25em;">
            <?php wp_nonce_field( 'onionpress_mastodon_save_handle', 'onionpress_mastodon_handle_nonce' ); ?>
            <input type="hidden" name="onionpress_mastodon_save_handle" value="1">
            <table class="form-table" role="presentation">
                <tr>
                    <th><label for="mastodon_handle">Address</label></th>
                    <td>
                        <input type="text" id="mastodon_handle" name="mastodon_handle"
                               value="<?php echo esc_attr( $handle ); ?>"
                               placeholder="@you@mastodon.archive.org"
                               class="regular-text" style="max-width:380px;">
                        <fieldset style="margin-top:0.5em;">
                            <label><input type="checkbox" name="include_boosts" value="1" <?php checked( ! empty( $opts['include_boosts'] ) ); ?>> Include boosts (reblogs of other people's posts)</label><br>
                            <label><input type="checkbox" name="include_replies" value="1" <?php checked( ! empty( $opts['include_replies'] ) ); ?>> Include replies to other people</label>
                        </fieldset>
                        <p class="description">Originals and self-threads are always imported. Public posts only &mdash; followers-only and DMs need a future OAuth step.</p>
                        <?php submit_button( $account_id ? 'Update' : 'Save & look up account', 'primary', 'submit', false ); ?>
                    </td>
                </tr>
            </table>
        </form>

        <?php if ( $account_id ) : ?>
            <h2>Step 2 &mdash; Sync</h2>
            <table class="wp-list-table widefat" style="max-width:620px;">
                <tbody>
                    <tr><th style="width:200px;">Account</th>
                        <td><code>@<?php echo esc_html( $handle ); ?></code> <span style="color:#888;">(id <?php echo esc_html( $account_id ); ?>)</span></td></tr>
                    <tr><th>Server reports</th><td><?php echo number_format_i18n( $total ); ?> total statuses</td></tr>
                    <tr><th>Imported here</th><td><?php echo number_format_i18n( $imported_now ); ?></td></tr>
                    <tr><th>Backfill</th><td><?php echo $backfill_done ? '<strong style="color:#2a7b2a;">complete</strong>' : '<em>in progress &mdash; click "Sync now" a few times until complete, or wait for the 5-minute poll</em>'; ?></td></tr>
                    <tr><th>Last sync</th><td><?php echo $last_sync ? esc_html( human_time_diff( $last_sync ) ) . ' ago' : '&mdash;'; ?>
                        <?php if ( $last_note ) : ?><br><small style="color:#666;"><?php echo esc_html( $last_note ); ?></small><?php endif; ?></td></tr>
                </tbody>
            </table>

            <form method="post" style="margin-top:1em;display:inline-block;">
                <?php wp_nonce_field( 'onionpress_mastodon_sync_now', 'onionpress_mastodon_sync_nonce' ); ?>
                <input type="hidden" name="onionpress_mastodon_sync_now" value="1">
                <?php submit_button( 'Sync now', 'primary', 'submit', false ); ?>
            </form>
            <form method="post" style="margin-top:1em;display:inline-block;margin-left:0.5em;"
                  onsubmit="return confirm('This clears the sync cursors (not the imported posts). Next sync will rescan from scratch but skip everything already imported. Continue?');">
                <?php wp_nonce_field( 'onionpress_mastodon_reset', 'onionpress_mastodon_reset_nonce' ); ?>
                <input type="hidden" name="onionpress_mastodon_reset" value="1">
                <?php submit_button( 'Reset cursors', 'secondary', 'submit', false ); ?>
            </form>

            <h2 style="margin-top:2em;">Recent imports</h2>
            <?php onionpress_mastodon_render_recent(); ?>
        <?php endif; ?>
    </div>
    <?php
}

/**
 * Save the user's Mastodon handle. Parses "@user@server" (leading @
 * optional), validates both halves, stores split pieces, and does a
 * live account lookup so we catch typos immediately. Also saves the
 * per-click import options (boosts / replies).
 */
function onionpress_mastodon_handle_save_handle_post() {
    $raw = isset( $_POST['mastodon_handle'] ) ? wp_unslash( $_POST['mastodon_handle'] ) : '';
    $raw = trim( (string) $raw );
    // Allow "user@server", "@user@server", or a full profile URL.
    if ( preg_match( '~^https?://([^/]+)/@([^/?#]+)~i', $raw, $m ) ) {
        $server   = strtolower( $m[1] );
        $username = $m[2];
    } else {
        $stripped = ltrim( $raw, '@' );
        $parts    = explode( '@', $stripped, 2 );
        if ( count( $parts ) !== 2 ) {
            return array( 'level' => 'error', 'message' => 'Enter your full address including the server, like <code>@you@mastodon.archive.org</code>.' );
        }
        list( $username, $server ) = $parts;
        $server = strtolower( trim( $server ) );
    }

    if ( ! preg_match( '/^[A-Za-z0-9_]{1,30}$/', $username ) ) {
        return array( 'level' => 'error', 'message' => 'The username part must be letters, digits, or underscores (up to 30 chars).' );
    }
    if ( ! preg_match( '/^[a-z0-9]([a-z0-9\-]{0,62}\.)+[a-z]{2,}$/', $server ) ) {
        return array( 'level' => 'error', 'message' => 'The server must look like a domain, e.g. <code>mastodon.archive.org</code>.' );
    }

    // Save the options (boosts/replies) regardless of lookup outcome so
    // the user doesn't have to re-tick them if the lookup fails once.
    $opts = array(
        'include_boosts'  => ! empty( $_POST['include_boosts'] ),
        'include_replies' => ! empty( $_POST['include_replies'] ),
    );
    update_option( ONIONPRESS_MASTODON_OPTS_OPT, $opts );

    // Live lookup against the declared server.
    $lookup = onionpress_mastodon_api_get(
        'https://' . $server . '/api/v1/accounts/lookup?acct=' . rawurlencode( $username )
    );
    if ( is_wp_error( $lookup ) ) {
        return array( 'level' => 'error', 'message' => 'Could not reach <code>' . esc_html( $server ) . '</code> over Tor: ' . esc_html( $lookup->get_error_message() ) );
    }
    if ( (int) $lookup['code'] !== 200 || empty( $lookup['json']['id'] ) ) {
        return array( 'level' => 'error', 'message' => 'Server returned HTTP ' . intval( $lookup['code'] ) . ' for <code>@' . esc_html( $username ) . '@' . esc_html( $server ) . '</code>. Check spelling.' );
    }

    $acct_full  = $username . '@' . $server;
    $account_id = (string) $lookup['json']['id'];
    $total      = isset( $lookup['json']['statuses_count'] ) ? (int) $lookup['json']['statuses_count'] : 0;

    // If the user is switching to a different account, reset cursors so
    // a fresh backfill walks the new account's history.
    $prev_id = (string) get_option( ONIONPRESS_MASTODON_ACCT_ID_OPT, '' );
    if ( $prev_id !== '' && $prev_id !== $account_id ) {
        delete_option( ONIONPRESS_MASTODON_OLDEST_OPT );
        delete_option( ONIONPRESS_MASTODON_NEWEST_OPT );
    }

    update_option( ONIONPRESS_MASTODON_HANDLE_OPT,  $acct_full );
    update_option( ONIONPRESS_MASTODON_USER_OPT,    $username );
    update_option( ONIONPRESS_MASTODON_SERVER_OPT,  $server );
    update_option( ONIONPRESS_MASTODON_ACCT_ID_OPT, $account_id );
    update_option( ONIONPRESS_MASTODON_STATUSES_OPT, $total );

    // Kick off the recurring poll immediately if it wasn't scheduled yet.
    if ( ! wp_next_scheduled( ONIONPRESS_MASTODON_CRON_HOOK ) ) {
        wp_schedule_event( time() + 30, 'onionpress_mastodon_5min', ONIONPRESS_MASTODON_CRON_HOOK );
    }

    return array(
        'level'   => 'success',
        'message' => 'Saved <code>@' . esc_html( $acct_full ) . '</code>. Server reports <strong>' . number_format_i18n( $total ) . '</strong> statuses. Click <strong>Sync now</strong> below to start importing.',
    );
}

/**
 * Discard the backfill/since cursors without deleting imported posts.
 * Useful if a previous import was interrupted in a bad state.
 */
function onionpress_mastodon_reset_cursors() {
    delete_option( ONIONPRESS_MASTODON_OLDEST_OPT );
    delete_option( ONIONPRESS_MASTODON_NEWEST_OPT );
    return array( 'level' => 'success', 'message' => 'Cursors cleared. Next sync will rescan from scratch (but will skip already-imported statuses).' );
}

/**
 * One sync tick: catch up forward from newest_id, then walk backward
 * from oldest_id for up to PAGES_PER_TICK pages. Idempotent — posts
 * whose source_id is already present are skipped silently. Takes a
 * mutex via a transient so overlapping cron ticks don't duplicate work.
 *
 * Returns an admin-notice-shaped array when called from the admin page;
 * ignored by the cron invocation.
 */
function onionpress_mastodon_run_sync_tick( $from_admin = false ) {
    $server     = (string) get_option( ONIONPRESS_MASTODON_SERVER_OPT, '' );
    $account_id = (string) get_option( ONIONPRESS_MASTODON_ACCT_ID_OPT, '' );
    if ( $server === '' || $account_id === '' ) {
        return array( 'level' => 'error', 'message' => 'Enter your Mastodon address first.' );
    }

    $lock = get_transient( ONIONPRESS_MASTODON_LOCK );
    if ( $lock ) {
        return array( 'level' => 'warning', 'message' => 'Another sync is already running. Try again in a minute.' );
    }
    set_transient( ONIONPRESS_MASTODON_LOCK, time(), 10 * MINUTE_IN_SECONDS );

    // Cron and CLI contexts inherit shorter time limits; bump to something
    // that won't abort mid-import. The wall-clock deadline below is the
    // real budget — this is just a safety net against php.ini defaults.
    @set_time_limit( ONIONPRESS_MASTODON_TICK_BUDGET_SEC + 30 );
    $deadline = microtime( true ) + ONIONPRESS_MASTODON_TICK_BUDGET_SEC;

    $opts = (array) get_option( ONIONPRESS_MASTODON_OPTS_OPT, array() );

    $stats = array( 'imported' => 0, 'skipped' => 0, 'errors' => 0, 'pages' => 0 );
    $errors = array();

    try {
        // Forward catch-up: fetch since_id=newest_id until empty.
        $newest_id = (string) get_option( ONIONPRESS_MASTODON_NEWEST_OPT, '' );
        if ( $newest_id !== '' ) {
            $rounds = 0;
            while ( $stats['pages'] < ONIONPRESS_MASTODON_PAGES_PER_TICK
                    && microtime( true ) < $deadline
                    && $rounds < 50 ) {
                $rounds++;
                $page = onionpress_mastodon_fetch_statuses( $server, $account_id, array( 'since_id' => $newest_id ) );
                if ( is_wp_error( $page ) ) { $errors[] = $page->get_error_message(); break; }
                $stats['pages']++;
                if ( empty( $page ) ) break;
                // Statuses come newest-first; iterate reverse so newest_id
                // advances monotonically and we don't "lose" posts if the
                // loop is interrupted.
                $reversed = array_reverse( $page );
                foreach ( $reversed as $status ) {
                    if ( microtime( true ) >= $deadline ) break;
                    $r = onionpress_mastodon_import_status( $status, $opts );
                    $stats[ $r ] = ( $stats[ $r ] ?? 0 ) + 1;
                    if ( $r !== 'errors' ) {
                        $newest_id = (string) $status['id'];
                    }
                }
                update_option( ONIONPRESS_MASTODON_NEWEST_OPT, $newest_id );
                if ( count( $page ) < ONIONPRESS_MASTODON_PER_PAGE ) break;
            }
        }

        // Backward backfill: walk max_id=oldest_id until empty.
        $oldest_id = (string) get_option( ONIONPRESS_MASTODON_OLDEST_OPT, '' );
        if ( $oldest_id !== 'done'
             && $stats['pages'] < ONIONPRESS_MASTODON_PAGES_PER_TICK
             && microtime( true ) < $deadline ) {
            while ( $stats['pages'] < ONIONPRESS_MASTODON_PAGES_PER_TICK
                    && microtime( true ) < $deadline ) {
                $params = array();
                if ( $oldest_id !== '' ) {
                    $params['max_id'] = $oldest_id;
                }
                $page = onionpress_mastodon_fetch_statuses( $server, $account_id, $params );
                if ( is_wp_error( $page ) ) { $errors[] = $page->get_error_message(); break; }
                $stats['pages']++;
                if ( empty( $page ) ) {
                    // Walked off the start of history — mark backfill done.
                    update_option( ONIONPRESS_MASTODON_OLDEST_OPT, 'done' );
                    break;
                }
                foreach ( $page as $status ) {
                    if ( microtime( true ) >= $deadline ) break;
                    $r = onionpress_mastodon_import_status( $status, $opts );
                    $stats[ $r ] = ( $stats[ $r ] ?? 0 ) + 1;
                    $oldest_id = (string) $status['id'];
                    // Seed newest_id on the very first imported status.
                    if ( get_option( ONIONPRESS_MASTODON_NEWEST_OPT, '' ) === '' ) {
                        update_option( ONIONPRESS_MASTODON_NEWEST_OPT, (string) $status['id'] );
                    }
                }
                update_option( ONIONPRESS_MASTODON_OLDEST_OPT, $oldest_id );
                if ( count( $page ) < ONIONPRESS_MASTODON_PER_PAGE ) {
                    update_option( ONIONPRESS_MASTODON_OLDEST_OPT, 'done' );
                    break;
                }
            }
        }
    } finally {
        delete_transient( ONIONPRESS_MASTODON_LOCK );
    }

    update_option( ONIONPRESS_MASTODON_LAST_SYNC, time() );
    $note = sprintf(
        '%d imported, %d skipped, %d errors across %d pages',
        intval( $stats['imported'] ?? 0 ),
        intval( $stats['skipped']  ?? 0 ),
        intval( $stats['errors']   ?? 0 ),
        intval( $stats['pages']    ?? 0 )
    );
    if ( $errors ) { $note .= ' — last error: ' . $errors[ count($errors) - 1 ]; }
    update_option( ONIONPRESS_MASTODON_LAST_NOTE, $note );

    if ( ! $from_admin ) {
        return; // cron path
    }
    $level = ( ! empty( $errors ) && ( $stats['imported'] ?? 0 ) === 0 ) ? 'error' : 'success';
    return array( 'level' => $level, 'message' => 'Sync tick: ' . esc_html( $note ) );
}

/**
 * Fetch one page of the account's statuses. Returns the parsed array
 * of status objects, or WP_Error on transport/HTTP failure. Empty
 * array means "no more data past this cursor."
 */
function onionpress_mastodon_fetch_statuses( $server, $account_id, $params ) {
    $params = array_merge( array(
        'limit'            => ONIONPRESS_MASTODON_PER_PAGE,
        'exclude_reblogs'  => 'false',
        'exclude_replies'  => 'false',
    ), $params );
    $url = sprintf(
        'https://%s/api/v1/accounts/%s/statuses?%s',
        $server,
        rawurlencode( $account_id ),
        http_build_query( $params )
    );
    $r = onionpress_mastodon_api_get( $url );
    if ( is_wp_error( $r ) ) return $r;
    if ( (int) $r['code'] !== 200 ) {
        return new WP_Error( 'mastodon_http', 'HTTP ' . $r['code'] . ' from ' . $server );
    }
    return is_array( $r['json'] ) ? $r['json'] : array();
}

/**
 * Tor-over-SOCKS HTTP GET via the onionheaven daemon. Returns
 * ['code'=>int, 'body'=>string, 'json'=>array|null], or WP_Error on
 * transport failure. We can't use wp_remote_get here because it offers
 * no SOCKS option and we must never egress clearnet directly.
 */
function onionpress_mastodon_api_get( $url ) {
    if ( ! function_exists( 'curl_init' ) ) {
        return new WP_Error( 'no_curl', 'curl extension required for Mastodon import' );
    }
    $ch = curl_init( $url );
    curl_setopt_array( $ch, array(
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 3,
        CURLOPT_PROXY          => ONIONPRESS_MASTODON_SOCKS_PROXY,
        CURLOPT_PROXYTYPE      => CURLPROXY_SOCKS5_HOSTNAME,
        CURLOPT_TIMEOUT        => ONIONPRESS_MASTODON_HTTP_TIMEOUT,
        CURLOPT_CONNECTTIMEOUT => 30,
        CURLOPT_USERAGENT      => 'OnionPress/SocialArchive (+https://onionpress.org)',
        CURLOPT_HTTPHEADER     => array( 'Accept: application/json' ),
    ) );
    $body = curl_exec( $ch );
    if ( $body === false ) {
        $err = curl_error( $ch );
        curl_close( $ch );
        return new WP_Error( 'curl', $err ?: 'curl failure' );
    }
    $code = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    curl_close( $ch );
    $json = json_decode( $body, true );
    return array( 'code' => $code, 'body' => $body, 'json' => is_array( $json ) ? $json : null );
}

/**
 * Download a file over Tor into a temp path. Used for media sideloads.
 * Returns the local file path or WP_Error.
 */
function onionpress_mastodon_fetch_file( $url, $dest_path ) {
    $fh = @fopen( $dest_path, 'wb' );
    if ( ! $fh ) return new WP_Error( 'io', 'open temp failed' );
    $ch = curl_init( $url );
    curl_setopt_array( $ch, array(
        CURLOPT_FILE           => $fh,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS      => 5,
        CURLOPT_PROXY          => ONIONPRESS_MASTODON_SOCKS_PROXY,
        CURLOPT_PROXYTYPE      => CURLPROXY_SOCKS5_HOSTNAME,
        CURLOPT_TIMEOUT        => 120,
        CURLOPT_CONNECTTIMEOUT => 30,
        CURLOPT_USERAGENT      => 'OnionPress/SocialArchive (+https://onionpress.org)',
    ) );
    $ok = curl_exec( $ch );
    $code = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    $err = curl_error( $ch );
    curl_close( $ch );
    fclose( $fh );
    if ( ! $ok || $code < 200 || $code >= 300 ) {
        @unlink( $dest_path );
        return new WP_Error( 'curl', 'HTTP ' . $code . ' ' . $err );
    }
    return $dest_path;
}

/**
 * Import one Mastodon status object. Returns 'imported' | 'skipped' |
 * 'errors' for the caller's tally.
 *
 * Mastodon's `reblog` field holds the boosted status; when it's set,
 * the outer status is a boost shell with no original content. Replies
 * are detected by `in_reply_to_id`; self-replies (to the same account)
 * form threads and are always imported.
 */
function onionpress_mastodon_import_status( $status, $opts ) {
    $status_id = isset( $status['id'] ) ? (string) $status['id'] : '';
    if ( $status_id === '' ) return 'errors';

    $source_id = 'mastodon:' . $status_id;
    $existing  = get_posts( array(
        'post_type'      => 'post',
        'meta_key'       => '_source_id',
        'meta_value'     => $source_id,
        'post_status'    => 'any',
        'posts_per_page' => 1,
        'fields'         => 'ids',
    ) );
    if ( ! empty( $existing ) ) return 'skipped';

    $is_boost = ! empty( $status['reblog'] );
    $in_reply = ! empty( $status['in_reply_to_id'] );
    $our_id   = (string) get_option( ONIONPRESS_MASTODON_ACCT_ID_OPT, '' );
    $reply_to_acct = isset( $status['in_reply_to_account_id'] ) ? (string) $status['in_reply_to_account_id'] : '';
    $is_self_reply = $in_reply && $our_id !== '' && $reply_to_acct === $our_id;

    if ( $is_boost && empty( $opts['include_boosts'] ) ) {
        return 'skipped';
    }
    if ( $in_reply && ! $is_self_reply && empty( $opts['include_replies'] ) ) {
        return 'skipped';
    }

    $ts = strtotime( $status['created_at'] ?? '' );
    if ( ! $ts ) return 'errors';

    list( $content_html, $preview_text ) = onionpress_mastodon_render_content( $status );
    $title = wp_trim_words( $preview_text, 10, '…' );
    if ( $title === '' ) $title = gmdate( 'Y-m-d', $ts ) . ' toot';

    $source_url = $status['url'] ?? '';
    // Boost shells have `url` pointing at the /activity endpoint; prefer
    // the boosted status's permalink so "View on mastodon.…" leads
    // somewhere useful.
    if ( $is_boost && ! empty( $status['reblog']['url'] ) ) {
        $source_url = $status['reblog']['url'];
    }

    $post_date_gmt = gmdate( 'Y-m-d H:i:s', $ts );
    $post_id = wp_insert_post( array(
        'post_type'     => 'post',
        'post_status'   => 'publish',
        'post_title'    => $title,
        'post_content'  => $content_html,
        'post_excerpt'  => wp_trim_words( $preview_text, 35, '…' ),
        'post_date_gmt' => $post_date_gmt,
        'post_date'     => get_date_from_gmt( $post_date_gmt ),
        'meta_input'    => array(
            '_source_id'      => $source_id,
            '_source_url'     => $source_url,
            '_is_repost'      => $is_boost  ? '1' : '0',
            '_is_reply'       => $in_reply  ? '1' : '0',
            '_reply_to_id'    => $in_reply  ? (string) $status['in_reply_to_id'] : '',
            '_thread_root_id' => $is_self_reply ? '' : $status_id,
            '_raw'            => wp_json_encode( $status ),
        ),
    ), true );

    if ( is_wp_error( $post_id ) ) return 'errors';

    if ( function_exists( 'onionpress_social_ensure_category' ) ) {
        $cat_id = onionpress_social_ensure_category( 'mastodon' );
        if ( $cat_id ) {
            wp_set_post_categories( $post_id, array( $cat_id ), false );
        }
    }

    // Tags: Mastodon posts carry explicit hashtag entities.
    $tag_names = array();
    foreach ( ( $status['tags'] ?? array() ) as $t ) {
        if ( ! empty( $t['name'] ) ) $tag_names[] = $t['name'];
    }
    if ( $tag_names ) {
        wp_set_post_tags( $post_id, $tag_names, false );
    }

    // Media: sideload each attachment from the source URL.
    $media = $is_boost
        ? ( $status['reblog']['media_attachments'] ?? array() )
        : ( $status['media_attachments'] ?? array() );
    if ( $media ) {
        onionpress_mastodon_sideload_media( $post_id, $status_id, $media );
    }

    return 'imported';
}

/**
 * Turn a status into ( $content_html, $preview_text ).
 *
 * Mastodon already returns sanitized HTML in `content`. We keep it as
 * is (wp_kses_post on display, no double-escape) and build the
 * preview by stripping tags. For boost shells, the outer content is
 * empty; we render a small "boosted by" wrapper above the boosted
 * post's content so the card conveys what's going on.
 */
function onionpress_mastodon_render_content( $status ) {
    if ( ! empty( $status['reblog'] ) ) {
        $inner = (string) ( $status['reblog']['content'] ?? '' );
        $boosted_acct = (string) ( $status['reblog']['account']['acct'] ?? '' );
        $boosted_url  = (string) ( $status['reblog']['url']             ?? '' );
        $header = $boosted_acct
            ? sprintf(
                '<p><em>Boosted <a href="%s" rel="nofollow noopener">@%s</a>:</em></p>',
                esc_url( $boosted_url ),
                esc_html( $boosted_acct )
            )
            : '<p><em>Boosted:</em></p>';
        $html = $header . $inner;
    } else {
        $html = (string) ( $status['content'] ?? '' );
    }
    // The Mastodon `content` field wraps paragraphs in <p>; that's fine
    // for direct insertion as post_content (theme + wpautop won't re-wrap).
    $preview = trim( html_entity_decode( wp_strip_all_tags( $html ), ENT_QUOTES, 'UTF-8' ) );
    return array( $html, $preview );
}

/**
 * Sideload media_attachments into uploads. Downloads each over Tor into
 * /tmp, then uses WP's media helpers to register + attach. Appends
 * <img>/<video> tags to the post content so the card renders them.
 * First image becomes the featured image so archive listings have a
 * thumbnail.
 */
function onionpress_mastodon_sideload_media( $post_id, $status_id, $media ) {
    require_once ABSPATH . 'wp-admin/includes/image.php';
    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/media.php';

    $upload = wp_upload_dir();
    $tags   = array();
    foreach ( $media as $m ) {
        $url  = (string) ( $m['url'] ?? '' );
        $type = (string) ( $m['type'] ?? '' );
        if ( $url === '' ) continue;

        $ext = strtolower( pathinfo( parse_url( $url, PHP_URL_PATH ) ?? '', PATHINFO_EXTENSION ) );
        if ( ! preg_match( '/^[a-z0-9]{1,5}$/', $ext ) ) {
            // Guess an extension from MIME type.
            $ext = ( $type === 'video' || $type === 'gifv' ) ? 'mp4'
                 : ( $type === 'audio' ? 'mp3' : 'jpg' );
        }
        $basename  = $status_id . '-' . wp_generate_password( 6, false ) . '.' . $ext;
        $tmp_path  = sys_get_temp_dir() . '/' . $basename;
        $fetched   = onionpress_mastodon_fetch_file( $url, $tmp_path );
        if ( is_wp_error( $fetched ) ) continue;

        $dest_name = wp_unique_filename( $upload['path'], $basename );
        $dest_path = $upload['path'] . '/' . $dest_name;
        if ( ! @rename( $tmp_path, $dest_path ) && ! @copy( $tmp_path, $dest_path ) ) {
            @unlink( $tmp_path );
            continue;
        }
        @unlink( $tmp_path );

        $filetype = wp_check_filetype( $dest_name );
        $attach_id = wp_insert_attachment( array(
            'post_mime_type' => $filetype['type'] ?? 'application/octet-stream',
            'post_title'     => sanitize_title( pathinfo( $dest_name, PATHINFO_FILENAME ) ),
            'post_content'   => '',
            'post_status'    => 'inherit',
        ), $dest_path, $post_id );
        if ( is_wp_error( $attach_id ) ) continue;

        $meta = wp_generate_attachment_metadata( $attach_id, $dest_path );
        wp_update_attachment_metadata( $attach_id, $meta );

        $mime = $filetype['type'] ?? '';
        if ( empty( $tags ) && strpos( $mime, 'image/' ) === 0 ) {
            set_post_thumbnail( $post_id, $attach_id );
        }

        if ( strpos( $mime, 'image/' ) === 0 ) {
            $img = wp_get_attachment_image(
                $attach_id, 'large', false,
                array( 'loading' => 'lazy', 'style' => 'max-width:100%;height:auto;' )
            );
            $tags[] = preg_replace( '~https?://[^/\s"\']+/~', '/', $img );
        } elseif ( strpos( $mime, 'video/' ) === 0 || $type === 'gifv' ) {
            $url_rel = wp_make_link_relative( wp_get_attachment_url( $attach_id ) );
            $loop = ( $type === 'gifv' ) ? ' autoplay loop muted playsinline' : '';
            $tags[] = sprintf(
                '<video controls preload="metadata" style="max-width:100%%;"%s><source src="%s" type="%s"></video>',
                $loop, esc_url( $url_rel ), esc_attr( $mime )
            );
        } elseif ( strpos( $mime, 'audio/' ) === 0 ) {
            $url_rel = wp_make_link_relative( wp_get_attachment_url( $attach_id ) );
            $tags[] = sprintf(
                '<audio controls preload="metadata" style="max-width:100%%;"><source src="%s" type="%s"></audio>',
                esc_url( $url_rel ), esc_attr( $mime )
            );
        }
    }

    if ( $tags ) {
        $post = get_post( $post_id );
        wp_update_post( array(
            'ID'           => $post_id,
            'post_content' => $post->post_content . "\n\n" . implode( "\n\n", $tags ),
        ) );
    }
}

function onionpress_mastodon_render_recent() {
    $recent = get_posts( array(
        'post_type'      => 'post',
        'posts_per_page' => 10,
        'orderby'        => 'date',
        'order'          => 'DESC',
        'category_name'  => 'mastodon',
    ) );
    if ( empty( $recent ) ) {
        echo '<p><em>No Mastodon posts imported yet.</em></p>';
        return;
    }
    echo '<ul>';
    foreach ( $recent as $p ) {
        printf(
            '<li><a href="%s">%s</a> &middot; <small>%s</small></li>',
            esc_url( get_permalink( $p->ID ) ),
            esc_html( get_the_title( $p ) ),
            esc_html( get_the_date( '', $p ) )
        );
    }
    echo '</ul>';
}
