<?php
/**
 * Plugin Name: OnionPress Social Archive
 * Description: Unified archive of posts imported from social platforms
 *              (Twitter, Mastodon, Bluesky, …). Registers the `social_post`
 *              custom post type and a `social_source` taxonomy so each
 *              platform has its own tab and the full archive has a
 *              cross-source timeline. Per-platform importers are separate
 *              mu-plugins that register a submenu under this one.
 * Version:     0.1
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

const ONIONPRESS_SOCIAL_POST_TYPE  = 'social_post';
const ONIONPRESS_SOCIAL_SOURCE_TAX = 'social_source';
const ONIONPRESS_SOCIAL_ADMIN_SLUG = 'onionpress-social-archive';

/**
 * Canonical list of supported source slugs. Importers can add to this via
 * the `onionpress_social_sources` filter, but the three launch sources are
 * declared here so the core plugin's CPT + taxonomy terms work even when
 * individual importer plugins haven't shipped yet.
 *
 * Each entry: [label, color]. The label is the human-facing platform name;
 * the color is a hex string used by templates / the admin listing.
 */
function onionpress_social_sources() {
    $sources = array(
        'twitter'  => array( 'label' => 'Twitter / X', 'color' => '#1da1f2' ),
        'mastodon' => array( 'label' => 'Mastodon',    'color' => '#6364ff' ),
        'bluesky'  => array( 'label' => 'Bluesky',     'color' => '#0085ff' ),
    );
    return apply_filters( 'onionpress_social_sources', $sources );
}

/**
 * Register the CPT + taxonomy. Both are public so the archive URLs
 * (`/social/` for everything, `/social/<slug>/` per source) work as
 * permalinks. `has_archive` gets the unified timeline for free via the
 * default archive template.
 */
add_action( 'init', function () {
    register_post_type( ONIONPRESS_SOCIAL_POST_TYPE, array(
        'label'        => 'Social Posts',
        'labels'       => array(
            'name'          => 'Social Posts',
            'singular_name' => 'Social Post',
            'menu_name'     => 'Social Archive',
            'all_items'     => 'All Social Posts',
        ),
        'public'       => true,
        'has_archive'  => 'social',
        'rewrite'      => array( 'slug' => 'social', 'with_front' => false ),
        'supports'     => array( 'title', 'editor', 'excerpt', 'custom-fields', 'thumbnail', 'comments' ),
        'show_in_rest' => true,
        'menu_icon'    => 'dashicons-format-chat',
        'taxonomies'   => array( ONIONPRESS_SOCIAL_SOURCE_TAX, 'post_tag' ),
        // Hide from the main admin menu — the Social Archive top-level menu
        // (registered below) owns the UI for imports, per-source tabs, etc.
        'show_in_menu' => false,
    ) );

    register_taxonomy( ONIONPRESS_SOCIAL_SOURCE_TAX, ONIONPRESS_SOCIAL_POST_TYPE, array(
        'label'             => 'Source',
        'hierarchical'      => false,
        'public'            => true,
        'show_admin_column' => true,
        'show_in_rest'      => true,
        'rewrite'           => array( 'slug' => 'social', 'with_front' => false ),
    ) );

    foreach ( onionpress_social_sources() as $slug => $info ) {
        if ( ! term_exists( $slug, ONIONPRESS_SOCIAL_SOURCE_TAX ) ) {
            wp_insert_term(
                $info['label'],
                ONIONPRESS_SOCIAL_SOURCE_TAX,
                array( 'slug' => $slug )
            );
        }
    }
} );

/**
 * Top-level admin menu "Social Archive". Importer mu-plugins register
 * submenu pages under this slug. The landing page is the dashboard —
 * per-source counts and links into each importer.
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
    // Rename the first submenu item (which WP auto-creates with the
    // parent's label) to "Dashboard" so importer submenus (Twitter,
    // Mastodon, …) read naturally.
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
    $sources      = onionpress_social_sources();
    $archive_url  = get_post_type_archive_link( ONIONPRESS_SOCIAL_POST_TYPE );
    $total_count  = intval( wp_count_posts( ONIONPRESS_SOCIAL_POST_TYPE )->publish );
    ?>
    <div class="wrap">
        <h1>Social Archive</h1>
        <p>Import your posts from social platforms into a unified, onion-hosted archive. Each source gets its own tab; a cross-source timeline combines everything in chronological order.</p>

        <h2>Totals</h2>
        <p>
            <strong><?php echo esc_html( $total_count ); ?></strong> imported post<?php echo $total_count === 1 ? '' : 's'; ?>
            <?php if ( $total_count > 0 && $archive_url ) : ?>
                &middot; <a href="<?php echo esc_url( $archive_url ); ?>">View unified timeline &rarr;</a>
            <?php endif; ?>
        </p>

        <h2>Per-source</h2>
        <table class="wp-list-table widefat striped" style="max-width:720px;">
            <thead>
                <tr>
                    <th>Source</th>
                    <th style="width:140px;">Posts</th>
                    <th style="width:220px;">Action</th>
                </tr>
            </thead>
            <tbody>
                <?php foreach ( $sources as $slug => $info ) :
                    $count      = onionpress_social_count_for_source( $slug );
                    $importer_slug = ONIONPRESS_SOCIAL_ADMIN_SLUG . '-' . $slug;
                    $import_url    = admin_url( 'admin.php?page=' . $importer_slug );
                    $has_importer  = onionpress_social_importer_registered( $slug );
                    $term          = get_term_by( 'slug', $slug, ONIONPRESS_SOCIAL_SOURCE_TAX );
                    $tab_url       = $term ? get_term_link( $term ) : '';
                    ?>
                    <tr>
                        <td>
                            <span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:<?php echo esc_attr( $info['color'] ); ?>;margin-right:6px;"></span>
                            <strong><?php echo esc_html( $info['label'] ); ?></strong>
                        </td>
                        <td>
                            <?php echo intval( $count ); ?>
                            <?php if ( $count > 0 && ! is_wp_error( $tab_url ) && $tab_url ) : ?>
                                &middot; <a href="<?php echo esc_url( $tab_url ); ?>">view</a>
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
            <li>Upload the ZIP on the matching Import page. OnionPress parses it, creates one post per entry, copies media into your uploads library, and preserves the original post dates.</li>
        </ol>
    </div>
    <?php
}

/**
 * Count published social_posts tagged with a given source slug.
 * Wraps WP_Query, which already caches the counts via the tax_query.
 */
function onionpress_social_count_for_source( $source_slug ) {
    $q = new WP_Query( array(
        'post_type'      => ONIONPRESS_SOCIAL_POST_TYPE,
        'posts_per_page' => 1,
        'fields'         => 'ids',
        'no_found_rows'  => false,
        'post_status'    => 'publish',
        'tax_query'      => array(
            array(
                'taxonomy' => ONIONPRESS_SOCIAL_SOURCE_TAX,
                'field'    => 'slug',
                'terms'    => $source_slug,
            ),
        ),
    ) );
    return intval( $q->found_posts );
}

/**
 * Has a sibling importer plugin for *source_slug* been loaded?
 *
 * Importers register themselves by calling
 * `onionpress_social_register_importer( 'twitter' )` at plugins_loaded
 * time. The dashboard uses this to decide whether to show an active
 * "Import" button or a greyed "not installed" notice.
 */
function onionpress_social_importer_registered( $slug ) {
    $registered = apply_filters( 'onionpress_social_importers', array() );
    return in_array( $slug, $registered, true );
}

/**
 * Helper for importer plugins to declare their source slug. A small
 * filter-based registry keeps the coupling loose — importer plugins
 * don't have to know about the core's internals beyond this one hook.
 */
function onionpress_social_register_importer( $slug ) {
    add_filter( 'onionpress_social_importers', function ( $list ) use ( $slug ) {
        $list[] = $slug;
        return array_values( array_unique( $list ) );
    } );
}
