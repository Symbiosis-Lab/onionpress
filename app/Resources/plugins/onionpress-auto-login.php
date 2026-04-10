<?php
/**
 * Plugin Name: OnionPress Auto-Login
 * Description: One-time magic login links for the OnionPress menubar app.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * Handle ?op_login=TOKEN on any front-end request.
 *
 * The menubar app generates a random token, stores it as a WordPress transient
 * (2-minute TTL), and opens the URL with ?op_login=TOKEN.  This plugin
 * validates the token, logs the admin in, deletes the token (single-use),
 * and redirects to the clean URL.
 */
add_action( 'init', function () {
    if ( empty( $_GET['op_login'] ) ) {
        return;
    }

    $token  = sanitize_text_field( $_GET['op_login'] );
    $stored = get_transient( 'op_login_' . $token );
    if ( $stored === false ) {
        return;
    }

    delete_transient( 'op_login_' . $token );

    $user = get_user_by( 'id', (int) $stored );
    if ( ! $user ) {
        return;
    }

    // Set cookie domain to match the actual request host (.onion or localhost)
    // instead of the stored home_url, which may differ.
    $host          = isset( $_SERVER['HTTP_HOST'] ) ? $_SERVER['HTTP_HOST'] : '';
    $cookie_domain = preg_replace( '/:\d+$/', '', $host );

    wp_set_current_user( $user->ID );

    $expiration       = time() + YEAR_IN_SECONDS;
    $auth_cookie      = wp_generate_auth_cookie( $user->ID, $expiration, 'auth' );
    $logged_in_cookie = wp_generate_auth_cookie( $user->ID, $expiration, 'logged_in' );

    setcookie( AUTH_COOKIE,      $auth_cookie,      $expiration, PLUGINS_COOKIE_PATH, $cookie_domain, false, true );
    setcookie( AUTH_COOKIE,      $auth_cookie,      $expiration, ADMIN_COOKIE_PATH,   $cookie_domain, false, true );
    setcookie( LOGGED_IN_COOKIE, $logged_in_cookie, $expiration, COOKIEPATH,          $cookie_domain, false, true );
    setcookie( LOGGED_IN_COOKIE, $logged_in_cookie, $expiration, SITECOOKIEPATH,      $cookie_domain, false, true );

    wp_redirect( remove_query_arg( 'op_login' ) );
    exit;
} );
