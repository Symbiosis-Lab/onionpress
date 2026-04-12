<?php
/**
 * OnionPress Theme Functions
 *
 * @package OnionPress
 */

if (!defined('ABSPATH')) {
    exit;
}

/**
 * Theme setup
 */
function onionpress_setup() {
    add_theme_support('title-tag');
    add_theme_support('post-thumbnails');
    add_theme_support('custom-header', array(
        'width'       => 1920,
        'height'      => 480,
        'flex-width'  => true,
        'flex-height' => true,
        'header-text' => false,
    ));
    add_theme_support('custom-logo', array(
        'width'       => 200,
        'height'      => 200,
        'flex-width'  => true,
        'flex-height' => true,
    ));
    add_theme_support('html5', array(
        'search-form', 'comment-form', 'comment-list', 'gallery', 'caption',
    ));

    register_nav_menus(array(
        'primary' => 'Primary Menu',
    ));
}
add_action('after_setup_theme', 'onionpress_setup');

/**
 * Enqueue theme styles
 */
function onionpress_scripts() {
    wp_enqueue_style('onionpress-style', get_stylesheet_uri(), array(), '1.0.0');
}
add_action('wp_enqueue_scripts', 'onionpress_scripts');

/**
 * Register sidebar for blogroll / follows
 */
function onionpress_widgets_init() {
    register_sidebar(array(
        'name'          => 'Sidebar',
        'id'            => 'sidebar-1',
        'before_widget' => '<div class="sidebar-section">',
        'after_widget'  => '</div>',
        'before_title'  => '<h3>',
        'after_title'   => '</h3>',
    ));
}
add_action('widgets_init', 'onionpress_widgets_init');

/**
 * Render hit counter in footer automatically
 */
function onionpress_footer_counter() {
    if (shortcode_exists('hit_counter')) {
        echo '<div class="footer-counter">';
        echo do_shortcode('[hit_counter]');
        echo '</div>';
    }
}
add_action('onionpress_footer', 'onionpress_footer_counter');

/**
 * Get the user's avatar URL (Gravatar or custom logo)
 */
function onionpress_get_avatar_url() {
    $custom_logo_id = get_theme_mod('custom_logo');
    if ($custom_logo_id) {
        $logo_url = wp_get_attachment_image_url($custom_logo_id, 'thumbnail');
        if ($logo_url) {
            return $logo_url;
        }
    }

    // Fall back to admin user's Gravatar
    $admin = get_user_by('email', get_option('admin_email'));
    if ($admin) {
        return get_avatar_url($admin->ID, array('size' => 200));
    }

    return false;
}

/**
 * Get first letter of site title for avatar placeholder
 */
function onionpress_get_avatar_letter() {
    $title = get_bloginfo('name');
    return mb_strtoupper(mb_substr($title, 0, 1));
}

/**
 * Create Follow page on theme activation if it doesn't exist
 */
function onionpress_create_follow_page() {
    $page = get_page_by_path('follow');
    if ($page) {
        return;
    }
    wp_insert_post(array(
        'post_title'    => 'Follow',
        'post_name'     => 'follow',
        'post_status'   => 'publish',
        'post_type'     => 'page',
        'post_content'  => '',
        'page_template' => 'page-follow.php',
    ));
}
add_action('after_switch_theme', 'onionpress_create_follow_page');
