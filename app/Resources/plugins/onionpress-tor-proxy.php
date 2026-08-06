<?php
/**
 * Plugin Name: OnionPress Tor Proxy
 * Description: Routes all outbound WordPress HTTP requests through the Tor SOCKS proxy to prevent clearnet leaks.
 * Version: 1.0.0
 * Author: OnionPress
 * License: AGPL-3.0
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Force all WordPress HTTP API requests through the Tor container's
 * SOCKS5 proxy.  Using CURLPROXY_SOCKS5_HOSTNAME ensures DNS resolution
 * also goes through Tor (no DNS leak).
 *
 * The tor container is reachable at "onionpress-tor" on the Docker
 * network, port 9050.
 */
add_action( 'http_api_curl', function ( $handle ) {
    curl_setopt( $handle, CURLOPT_PROXY, 'onionpress-tor' );
    curl_setopt( $handle, CURLOPT_PROXYPORT, 9050 );
    curl_setopt( $handle, CURLOPT_PROXYTYPE, CURLPROXY_SOCKS5_HOSTNAME );

    // Tor is slower than direct — give requests more time
    curl_setopt( $handle, CURLOPT_TIMEOUT, 30 );
    curl_setopt( $handle, CURLOPT_CONNECTTIMEOUT, 15 );
} );

/**
 * Let .onion URLs through WordPress's safe-request URL check.
 *
 * wp_http_validate_url() (applied whenever reject_unsafe_urls is set,
 * e.g. by fetch_feed()/wp_safe_remote_get()) resolves the host with
 * gethostbyname() and rejects the URL when resolution fails. A .onion
 * host can never resolve in clearnet DNS, so every safe request to an
 * onion service fails before the transport runs. The check exists to
 * block SSRF against local/private addresses — moot for .onion here,
 * because the http_api_curl hook above forces the request through the
 * Tor SOCKS proxy with proxy-side name resolution: it egresses via the
 * Tor network and cannot reach anything on the local network.
 */
add_filter( 'http_request_args', function ( $args, $url ) {
    $host = strtolower( (string) parse_url( $url, PHP_URL_HOST ) );
    if ( '' !== $host && '.onion' === substr( $host, -6 ) ) {
        $args['reject_unsafe_urls'] = false;
    }
    return $args;
}, 10, 2 );
