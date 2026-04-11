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
