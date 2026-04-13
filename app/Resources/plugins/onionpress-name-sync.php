<?php
/**
 * Plugin Name: OnionPress Name Sync
 * Description: Registers each new WordPress username with OnionHome as an
 *              onionname. Does NOT release names when users are deleted —
 *              a registered onionname stays claimed forever so that
 *              Wayback-Machine archives of that user's posts remain
 *              resolvable via onionpress.org/<onionname>. The tor
 *              container does the signing (it has the HS private key);
 *              this plugin just tells the tor container which user was
 *              created.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

// Local Docker-network endpoint on the tor container. Source-IP-filtered
// on the server side so it is not reachable from outside the compose
// network even though it rides on the same port as the onion-exposed API.
if ( ! defined( 'ONIONPRESS_NAMESYNC_ENDPOINT' ) ) {
    define( 'ONIONPRESS_NAMESYNC_ENDPOINT', 'http://onionpress-tor:8083' );
}

/**
 * Log through WP's error_log so the entry lands in docker logs alongside
 * other WordPress output and onionpress-wordpress container stderr.
 */
function onionpress_namesync_log( $message ) {
    error_log( '[onionpress-name-sync] ' . $message );
}

/**
 * POST {"onionname": <login>} to /api/name/{register,release}-local and
 * record the result. Never throws — user creation/deletion must not be
 * blocked by a registrar outage.
 *
 * Uses direct curl rather than wp_remote_post because onionpress-tor-proxy
 * globally routes WP HTTP through the Tor SOCKS proxy, which would turn a
 * cheap Docker-network call into a full Tor round-trip and fail when the
 * SOCKS proxy doesn't know how to reach docker-internal hostnames.
 */
function onionpress_namesync_call( $action, $onionname ) {
    $url  = ONIONPRESS_NAMESYNC_ENDPOINT . '/api/name/' . $action . '-local';
    $body = wp_json_encode( array( 'onionname' => $onionname ) );

    $ch = curl_init( $url );
    if ( ! $ch ) {
        onionpress_namesync_log( "$action $onionname: curl_init failed" );
        return;
    }
    curl_setopt_array( $ch, array(
        CURLOPT_POST           => 1,
        CURLOPT_POSTFIELDS     => $body,
        CURLOPT_HTTPHEADER     => array( 'Content-Type: application/json' ),
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 35,
        CURLOPT_CONNECTTIMEOUT => 5,
        CURLOPT_USERAGENT      => 'onionpress-name-sync/1.0',
    ) );
    $response = curl_exec( $ch );
    $code     = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    $err      = curl_error( $ch );
    curl_close( $ch );

    if ( $response === false || $err ) {
        onionpress_namesync_log( "$action $onionname: ERROR $err" );
        return;
    }

    $short = is_string( $response ) ? substr( $response, 0, 300 ) : '';
    onionpress_namesync_log( "$action $onionname: $code $short" );
}

/**
 * Fires when a new WordPress user is created — single-site and multisite
 * both dispatch this hook. Multisite-specific hooks like wpmu_new_user
 * also fire, but always in addition to user_register, so we only hook
 * user_register and rely on the server's idempotent register to absorb
 * any accidental double-calls (same address + same name = 200 OK).
 */
function onionpress_namesync_on_register( $user_id ) {
    $user = get_userdata( $user_id );
    if ( ! $user || empty( $user->user_login ) ) {
        return;
    }
    onionpress_namesync_call( 'register', $user->user_login );
}
add_action( 'user_register', 'onionpress_namesync_on_register', 10, 1 );

// Note: we intentionally do NOT hook delete_user / wpmu_delete_user.
// Once an onionname is registered with OnionHome it stays registered
// forever so that Wayback-Machine archives of that user's posts remain
// resolvable via onionpress.org/<onionname>. Explicit release is only
// possible via the signed /api/name/release endpoint (operator action).
