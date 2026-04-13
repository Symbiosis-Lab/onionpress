<?php
/**
 * Plugin Name: OnionPress User Path
 * Description: Makes /<onionname>/ serve that user's author archive on every
 *              OnionPress install, so a visitor landing on
 *              op2abc.onion/brewsterkahle sees Brewster's posts instead of
 *              a 404. OnionPress is single-blog-per-install by default; WP
 *              multisite sub-blog creation on user_register is future work.
 *              Until then, the author archive is the stable per-user page.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Rewrite /<name>/ (single-segment path) to an author-archive query when
 * NAME matches a WP user. We do this by populating $wp->query_vars at
 * parse_request time so WP's normal template hierarchy takes over — no
 * extra redirect, no custom template.
 *
 * Priority 30 so this runs AFTER onionpress-directory (priority 10 default),
 * which on OnionHome may 302-redirect away for remote names before we even
 * get a chance to look at the path.
 */
add_action( 'parse_request', function ( $wp ) {
    // Only GETs; leave wp-admin, XML-RPC, login, etc. untouched.
    if ( ( $_SERVER['REQUEST_METHOD'] ?? 'GET' ) !== 'GET' ) {
        return;
    }

    $uri  = $_SERVER['REQUEST_URI'] ?? '/';
    $path = parse_url( $uri, PHP_URL_PATH );
    if ( ! is_string( $path ) ) {
        return;
    }
    $segment = trim( $path, '/' );

    // Must be a single non-empty segment.
    if ( $segment === '' || strpos( $segment, '/' ) !== false ) {
        return;
    }

    // Length / charset filter matches the server-side validate_name rules,
    // so we don't pound the DB for obviously-invalid segments.
    if (
        strlen( $segment ) < 5 || strlen( $segment ) > 40
        || ! preg_match( '/^[A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9]$/', $segment )
        || preg_match( '/^[0-9]+$/', $segment )
    ) {
        return;
    }

    // Skip obvious WP paths — get_user_by doesn't return anything for these
    // anyway, but the short-circuit avoids a DB hit.
    $reserved = array(
        'wp-admin', 'wp-content', 'wp-login', 'wp-includes', 'wp-json',
        'wp-cron', 'wp-signup', 'wp-activate', 'feed', 'xmlrpc',
        'follow', 'onionpress-status', 'onionpress-settings',
    );
    if ( in_array( strtolower( $segment ), $reserved, true ) ) {
        return;
    }

    // Look up by login. Note: WP usernames are not case-insensitive at the
    // DB level by default, but our registry is — so we first try exact, then
    // iterate over any user whose lowercased login matches.
    $user = get_user_by( 'login', $segment );
    if ( ! $user ) {
        $lower = strtolower( $segment );
        $users = get_users( array(
            'search'         => $segment,
            'search_columns' => array( 'user_login' ),
            'number'         => 5,
            'fields'         => array( 'user_login', 'ID' ),
        ) );
        foreach ( $users as $candidate ) {
            if ( strtolower( $candidate->user_login ) === $lower ) {
                $user = get_userdata( $candidate->ID );
                break;
            }
        }
    }
    if ( ! $user ) {
        return;
    }

    // Set the author query on WP. Using nicename because that's what WP's
    // author-archive logic matches against; it falls back to user_login for
    // users who haven't customized their display name.
    $wp->query_vars = array(
        'author_name' => $user->user_nicename
            ? $user->user_nicename
            : $user->user_login,
    );
}, 30 );
