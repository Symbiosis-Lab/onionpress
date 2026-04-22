<?php
/**
 * Plugin Name: OnionPress Social Archive
 * Description: Imports posts from social platforms (Twitter/X, Mastodon,
 *              Bluesky, …) into the regular WordPress blog as ordinary
 *              posts, tagged with a per-source category so each platform
 *              gets a standard `/category/<source>/` tab and everything
 *              mingles chronologically with your own writing. Per-source
 *              importers are separate mu-plugins that register a submenu
 *              under this one.
 * Version:     0.2
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

const ONIONPRESS_SOCIAL_ADMIN_SLUG = 'onionpress-social-archive';

/**
 * Canonical list of supported sources. Each entry carries:
 *   - label:    human-facing platform name (e.g. "Twitter / X")
 *   - cat_name: display name for the WP category
 *   - cat_slug: URL slug for the WP category, ends up in /category/<slug>/
 *   - color:    hex string used in the admin dashboard
 *
 * Importers can extend via the `onionpress_social_sources` filter.
 */
function onionpress_social_sources() {
    $sources = array(
        'twitter'  => array(
            'label'    => 'Twitter / X',
            'cat_name' => 'Twitter',
            'cat_slug' => 'twitter',
            'color'    => '#1da1f2',
        ),
        'mastodon' => array(
            'label'    => 'Mastodon',
            'cat_name' => 'Mastodon',
            'cat_slug' => 'mastodon',
            'color'    => '#6364ff',
        ),
        'bluesky'  => array(
            'label'    => 'Bluesky',
            'cat_name' => 'Bluesky',
            'cat_slug' => 'bluesky',
            'color'    => '#0085ff',
        ),
    );
    return apply_filters( 'onionpress_social_sources', $sources );
}

/**
 * Ensure a category term exists for *source_slug* and return its term ID.
 * Cached per-request so repeated lookups during a large import are cheap.
 * Creates the category on first use — no admin step required.
 */
function onionpress_social_ensure_category( $source_slug ) {
    static $cache = array();
    if ( isset( $cache[ $source_slug ] ) ) {
        return $cache[ $source_slug ];
    }
    $sources = onionpress_social_sources();
    if ( ! isset( $sources[ $source_slug ] ) ) {
        return 0;
    }
    $info = $sources[ $source_slug ];
    $term = get_term_by( 'slug', $info['cat_slug'], 'category' );
    if ( $term && ! is_wp_error( $term ) ) {
        return $cache[ $source_slug ] = (int) $term->term_id;
    }
    $result = wp_insert_term(
        $info['cat_name'],
        'category',
        array(
            'slug'        => $info['cat_slug'],
            'description' => sprintf( 'Posts imported from %s.', $info['label'] ),
        )
    );
    if ( is_wp_error( $result ) ) {
        return 0;
    }
    return $cache[ $source_slug ] = (int) $result['term_id'];
}

/**
 * Count posts currently tagged with the given source's category. Used
 * by the admin dashboard and by the one-shot migration from the
 * old social_post CPT shape (see below).
 */
function onionpress_social_count_for_source( $source_slug ) {
    $sources = onionpress_social_sources();
    if ( ! isset( $sources[ $source_slug ] ) ) {
        return 0;
    }
    $term = get_term_by( 'slug', $sources[ $source_slug ]['cat_slug'], 'category' );
    if ( ! $term || is_wp_error( $term ) ) {
        return 0;
    }
    return (int) $term->count;
}

/**
 * One-shot migration from the v0.1 shape (social_post CPT +
 * social_source taxonomy) to the v0.2 shape (regular posts + category).
 * Runs at wp_loaded, gated by a version option so it fires once per
 * site. If there were never any social_posts (a fresh v0.2 install),
 * it's effectively a no-op after the first query.
 */
add_action( 'wp_loaded', function () {
    if ( get_option( 'onionpress_social_archive_migration' ) === '0.2' ) {
        return;
    }
    global $wpdb;
    // Find any posts that still have the old social_post type.
    $old = $wpdb->get_col( $wpdb->prepare(
        "SELECT ID FROM {$wpdb->posts} WHERE post_type = %s LIMIT 5000",
        'social_post'
    ) );
    foreach ( $old as $post_id ) {
        // 1. Flip post_type to the regular `post`.
        $wpdb->update(
            $wpdb->posts,
            array( 'post_type' => 'post' ),
            array( 'ID' => (int) $post_id ),
            array( '%s' ),
            array( '%d' )
        );
        clean_post_cache( $post_id );
        // 2. Assign the matching source category.
        $source = get_post_meta( $post_id, '_source_id', true );
        if ( is_string( $source ) && strpos( $source, ':' ) !== false ) {
            list( $source_slug ) = explode( ':', $source, 2 );
            $cat_id = onionpress_social_ensure_category( $source_slug );
            if ( $cat_id ) {
                wp_set_post_categories( (int) $post_id, array( $cat_id ), true );
            }
        }
    }
    update_option( 'onionpress_social_archive_migration', '0.2' );
    // Flush so /category/<slug>/ URLs render cleanly and any stale
    // /social/... rules from v0.1 are dropped.
    flush_rewrite_rules( false );
}, 5 );  // before the rewrite-flush gate below

/**
 * One-shot rewrite-rule flush on first activation. mu-plugins have no
 * activation hook, so version-gate the flush via a WP option: flush
 * once, record, never again (until the version tag bumps). Per-site on
 * multisite because each site stores its own rewrite_rules option.
 */
add_action( 'wp_loaded', function () {
    if ( get_option( 'onionpress_social_archive_rewrite_version' ) !== '0.2' ) {
        flush_rewrite_rules( false );
        update_option( 'onionpress_social_archive_rewrite_version', '0.2' );
    }
}, 10 );

/**
 * Top-level admin menu "Social Archive". Importer sibling plugins
 * register submenu pages under this slug. The landing page is a
 * dashboard — per-source counts and per-source "Import" buttons.
 */
add_action( 'admin_menu', function () {
    add_menu_page(
        'Social Archive',
        'Social Archive',
        'manage_options',
        ONIONPRESS_SOCIAL_ADMIN_SLUG,
        'onionpress_social_archive_dashboard',
        'dashicons-format-chat',
        25
    );
    add_submenu_page(
        ONIONPRESS_SOCIAL_ADMIN_SLUG,
        'Social Archive Dashboard',
        'Dashboard',
        'manage_options',
        ONIONPRESS_SOCIAL_ADMIN_SLUG,
        'onionpress_social_archive_dashboard'
    );
}, 10 );

function onionpress_social_archive_dashboard() {
    $sources = onionpress_social_sources();
    ?>
    <div class="wrap">
        <h1>Social Archive</h1>
        <p>Import your posts from social platforms into this blog as regular posts. Each platform gets its own category tab (e.g. <code>/category/twitter/</code>); everything interleaves chronologically on your main blog and in the category archives.</p>

        <h2>Per-source</h2>
        <table class="wp-list-table widefat striped" style="max-width:720px;">
            <thead>
                <tr>
                    <th>Source</th>
                    <th style="width:140px;">Posts</th>
                    <th style="width:240px;">Action</th>
                </tr>
            </thead>
            <tbody>
                <?php foreach ( $sources as $slug => $info ) :
                    $count         = onionpress_social_count_for_source( $slug );
                    $importer_slug = ONIONPRESS_SOCIAL_ADMIN_SLUG . '-' . $slug;
                    $import_url    = admin_url( 'admin.php?page=' . $importer_slug );
                    $has_importer  = onionpress_social_importer_registered( $slug );
                    $cat_term      = get_term_by( 'slug', $info['cat_slug'], 'category' );
                    $cat_url       = $cat_term ? get_term_link( $cat_term ) : '';
                    ?>
                    <tr>
                        <td>
                            <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:<?php echo esc_attr( $info['color'] ); ?>;margin-right:6px;"></span>
                            <strong><?php echo esc_html( $info['label'] ); ?></strong>
                        </td>
                        <td>
                            <?php echo intval( $count ); ?>
                            <?php if ( $count > 0 && $cat_url && ! is_wp_error( $cat_url ) ) : ?>
                                &middot; <a href="<?php echo esc_url( $cat_url ); ?>">view</a>
                            <?php endif; ?>
                        </td>
                        <td>
                            <?php if ( $has_importer ) : ?>
                                <a href="<?php echo esc_url( $import_url ); ?>" class="button button-primary">Import</a>
                            <?php else : ?>
                                <em>Importer not installed</em>
                            <?php endif; ?>
                        </td>
                    </tr>
                <?php endforeach; ?>
            </tbody>
        </table>

        <h2>How this works</h2>
        <ol>
            <li>Request a data export from the social platform (typically Settings &rarr; Your Data &rarr; Download archive).</li>
            <li>Wait for the platform to email the archive (hours to days).</li>
            <li>Upload the ZIP on the matching Import page. OnionPress reads the archive, copies media into your blog's uploads, and creates one blog post per entry, tagged with the right category and dated to the original post time.</li>
        </ol>
    </div>
    <?php
}

/**
 * Has a sibling importer plugin for *source_slug* been loaded?
 *
 * Importers register themselves via
 * `onionpress_social_register_importer( 'twitter' )` at plugins_loaded
 * time. The dashboard uses this to show an active "Import" button vs.
 * a greyed "not installed" notice.
 */
function onionpress_social_importer_registered( $slug ) {
    $registered = apply_filters( 'onionpress_social_importers', array() );
    return in_array( $slug, $registered, true );
}

function onionpress_social_register_importer( $slug ) {
    add_filter( 'onionpress_social_importers', function ( $list ) use ( $slug ) {
        $list[] = $slug;
        return array_values( array_unique( $list ) );
    } );
}
