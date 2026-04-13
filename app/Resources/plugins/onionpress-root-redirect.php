<?php
/**
 * Plugin Name: OnionPress Root Redirect
 * Description: Redirects / to /<primary_onionname> when no static front page
 *              is configured. Lets users see their blog at their onionname URL
 *              by default, but respects any custom front page they set up.
 * Version:     1.0
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

add_action( 'template_redirect', function () {
    // Only redirect the exact root URL
    if ( ! is_front_page() || ! is_home() ) {
        return;
    }

    // If the user set a static front page, respect it — don't redirect
    if ( get_option( 'show_on_front' ) === 'page' && get_option( 'page_on_front' ) ) {
        return;
    }

    // Get the primary onionname: first administrator's user_login
    $admins = get_users( array(
        'role'    => 'administrator',
        'number'  => 1,
        'orderby' => 'ID',
        'order'   => 'ASC',
        'fields'  => array( 'ID', 'user_login' ),
    ) );
    if ( empty( $admins ) || empty( $admins[0]->user_login ) ) {
        return;
    }
    $onionname = $admins[0]->user_login;

    // Don't redirect generic "admin" username — only redirect meaningful onionnames
    if ( $onionname === 'admin' ) {
        return;
    }

    // Don't redirect if we're already at /onionname
    $path = trim( parse_url( $_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH ), '/' );
    if ( strtolower( $path ) === strtolower( $onionname ) ) {
        return;
    }

    wp_redirect( home_url( '/' . $onionname . '/' ), 302 );
    exit;
} );
