<?php
/**
 * Plugin Name: OnionPress Directory
 * Description: Turns this instance into a naming directory — resolves
 *              GET /follow?name=NAME and GET /NAME lookups against the
 *              local onionnames registry on the tor container, and 302s
 *              to the target .onion when the name resolves to a
 *              different address. No-op when this instance is not
 *              OnionHome: the local registry endpoint is source-IP
 *              protected AND returns 404 off-OnionHome anyway.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

// OnionHome's .onion address. Duplicated from app/Resources/docker/tor/
// web-server.py so the plugin has the same notion of "are we OnionHome?"
// without needing to read from disk.
if ( ! defined( 'ONIONPRESS_ONIONHOME_ADDRESS' ) ) {
    define(
        'ONIONPRESS_ONIONHOME_ADDRESS',
        'op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion'
    );
}
if ( ! defined( 'ONIONPRESS_CLEARNET_HOST' ) ) {
    define( 'ONIONPRESS_CLEARNET_HOST', 'onionpress.org' );
}

// Hosts on which this plugin activates. Anywhere else we exit early.
function onionpress_directory_is_onionhome_host( $host ) {
    $host = strtolower( (string) $host );
    if ( strpos( $host, ':' ) !== false ) {
        $host = substr( $host, 0, strpos( $host, ':' ) );
    }
    return (
        $host === ONIONPRESS_ONIONHOME_ADDRESS
        || $host === ONIONPRESS_CLEARNET_HOST
        || $host === 'www.' . ONIONPRESS_CLEARNET_HOST
    );
}

/**
 * Look up NAME in the registry via the tor container's internal API.
 * Returns an associative array on success (onionname, onionaddress, url,
 * registered_at, last_seen_at) or null on miss / error.
 *
 * Direct curl (not wp_remote_*) so onionpress-tor-proxy's SOCKS routing
 * doesn't try to tunnel a docker-internal hop through Tor.
 */
function onionpress_directory_lookup( $name ) {
    if ( ! is_string( $name ) || $name === '' ) {
        return null;
    }
    $url = 'http://onionpress-tor:8083/api/name/lookup/' . rawurlencode( $name );
    $ch  = curl_init( $url );
    if ( ! $ch ) {
        return null;
    }
    curl_setopt_array( $ch, array(
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 5,
        CURLOPT_CONNECTTIMEOUT => 2,
    ) );
    $body = curl_exec( $ch );
    $code = (int) curl_getinfo( $ch, CURLINFO_HTTP_CODE );
    curl_close( $ch );
    if ( $code !== 200 || ! is_string( $body ) ) {
        return null;
    }
    $data = json_decode( $body, true );
    if ( ! is_array( $data ) || empty( $data['onionaddress'] ) ) {
        return null;
    }
    return $data;
}

/**
 * Redirect to the target .onion's /follow page, or 404 if the name is
 * unknown. Only called when the caller explicitly asked to follow a name.
 */
function onionpress_directory_handle_follow_by_name( $name ) {
    $info = onionpress_directory_lookup( $name );
    if ( ! $info ) {
        status_header( 404 );
        nocache_headers();
        header( 'Content-Type: text/html; charset=utf-8' );
        echo '<!doctype html><meta charset="utf-8"><title>Onionname not found</title>';
        echo '<body style="font-family:system-ui,sans-serif;padding:2em">';
        echo '<h1>Onionname not found</h1>';
        echo '<p>No site is registered for <code>' . esc_html( $name ) . '</code>.</p>';
        echo '</body>';
        exit;
    }
    $target = 'http://' . $info['onionaddress'] . '/follow?address='
        . rawurlencode( $info['onionaddress'] );
    wp_redirect( $target, 302 );
    exit;
}

/**
 * Bare-NAME redirect: if the incoming path is a single segment that could
 * be an onionname AND the registry has an entry AND the target is some
 * OTHER .onion, 302 to that site. If the target is this instance, fall
 * through so WP multisite serves the local blog.
 */
function onionpress_directory_handle_name_lookup( $name ) {
    // Cheap client-side filter — avoids a curl hop for obviously-invalid
    // segments. Mirrors the server's validate_name rules.
    if (
        strlen( $name ) < 5 || strlen( $name ) > 40
        || ! preg_match( '/^[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]$/', $name )
        || preg_match( '/^[0-9]+$/', $name )
    ) {
        return;
    }
    $info = onionpress_directory_lookup( $name );
    if ( ! $info ) {
        return; // Fall through; WP may serve a local blog or 404.
    }

    // If the name is registered to this very instance, let WP handle the
    // request as it would normally (the multisite path will match this
    // user's blog).
    $own = strtolower( (string) ( $_SERVER['HTTP_HOST'] ?? '' ) );
    if ( strpos( $own, ':' ) !== false ) {
        $own = substr( $own, 0, strpos( $own, ':' ) );
    }
    if ( $own === strtolower( $info['onionaddress'] ) ) {
        return;
    }

    // Clearnet bridge special case: if the visitor came in on
    // onionpress.org we don't ship them to a raw .onion URL in their
    // clearnet browser (which would just error). Surface a simple page
    // pointing at Tor Browser instead. Indexers explicitly blocked.
    if ( $own === ONIONPRESS_CLEARNET_HOST || $own === 'www.' . ONIONPRESS_CLEARNET_HOST ) {
        status_header( 200 );
        header( 'X-Robots-Tag: noindex, nofollow' );
        header( 'Content-Type: text/html; charset=utf-8' );
        $name_h  = esc_html( $info['onionname'] );
        $addr_h  = esc_html( $info['onionaddress'] );
        $addr_a  = esc_attr( $info['onionaddress'] );
        $onion_h = esc_html( 'http://' . $info['onionaddress'] . '/' . $info['onionname'] );
        echo '<!doctype html><meta charset="utf-8"><title>@' . $name_h . ' — OnionPress</title>';
        echo '<meta name="robots" content="noindex, nofollow">';
        echo '<body style="font-family:system-ui,sans-serif;padding:2em;max-width:640px;margin:auto">';
        echo '<h1>@' . $name_h . '</h1>';
        echo '<p>This OnionPress site publishes as a Tor onion service. To read it you need';
        echo ' <a href="https://www.torproject.org/download/">Tor Browser</a> and the address:</p>';
        echo '<p style="background:#f3f4f6;padding:1em;border-radius:6px;word-break:break-all;font-family:ui-monospace,monospace">' . $addr_h . '</p>';
        echo '<p>Direct link in Tor Browser: <code style="word-break:break-all">' . $onion_h . '</code></p>';
        echo '</body>';
        exit;
    }

    wp_redirect( 'http://' . $info['onionaddress'] . '/' . $info['onionname'], 302 );
    exit;
}

/**
 * Dispatch on every request. Runs AFTER WP has parsed the URL into
 * query vars, which is early enough to intercept before the template
 * layer picks up the blog-path.
 */
add_action( 'parse_request', function ( $wp ) {
    if ( ! onionpress_directory_is_onionhome_host( $_SERVER['HTTP_HOST'] ?? '' ) ) {
        return;
    }

    // Only respond to bare GETs. POSTs, admin, XML-RPC, etc. pass through.
    if ( ( $_SERVER['REQUEST_METHOD'] ?? 'GET' ) !== 'GET' ) {
        return;
    }

    $uri  = $_SERVER['REQUEST_URI'] ?? '/';
    $path = parse_url( $uri, PHP_URL_PATH );
    if ( ! is_string( $path ) ) {
        return;
    }
    $path = trim( $path, '/' );

    // /follow?name=NAME — the named-follow entry point linked by theme headers.
    if ( $path === 'follow' && ! empty( $_GET['name'] ) ) {
        onionpress_directory_handle_follow_by_name(
            sanitize_text_field( $_GET['name'] )
        );
        return;
    }

    // /NAME — single path segment. Skip if the URL has more than one segment
    // (posts, categories, etc.) or looks like a reserved path.
    if ( $path !== '' && strpos( $path, '/' ) === false ) {
        onionpress_directory_handle_name_lookup( $path );
    }
} );
