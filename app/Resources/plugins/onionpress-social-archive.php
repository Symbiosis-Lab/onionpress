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
            'label'     => 'Twitter / X',
            'cat_name'  => 'Twitter',
            'cat_slug'  => 'twitter',
            'color'     => '#1da1f2',
            'nav_label' => 'My Tweets',
        ),
        'mastodon' => array(
            'label'     => 'Mastodon',
            'cat_name'  => 'Mastodon',
            'cat_slug'  => 'mastodon',
            'color'     => '#6364ff',
            'nav_label' => 'My Toots',
        ),
        'bluesky'  => array(
            'label'     => 'Bluesky',
            'cat_name'  => 'Bluesky',
            'cat_slug'  => 'bluesky',
            'color'     => '#0085ff',
            'nav_label' => 'My Skeets',
        ),
    );
    return apply_filters( 'onionpress_social_sources', $sources );
}

/**
 * Inject per-source nav items into a custom primary nav menu via
 * the `wp_nav_menu_items` filter. Only applies when the user has
 * configured a primary menu AND has imported posts for that source;
 * sites without configured menus use the theme's fallback nav,
 * which has parallel logic in header.php.
 */
function onionpress_social_inject_nav_items( $items, $args ) {
    if ( ! is_object( $args ) || ( isset( $args->theme_location ) && $args->theme_location !== 'primary' ) ) {
        return $items;
    }
    $extra = '';
    foreach ( onionpress_social_sources() as $slug => $info ) {
        $term = get_term_by( 'slug', $info['cat_slug'], 'category' );
        if ( ! $term || is_wp_error( $term ) || $term->count < 1 ) {
            continue;
        }
        $url   = get_term_link( $term );
        if ( is_wp_error( $url ) ) {
            continue;
        }
        $extra .= sprintf(
            '<li class="menu-item"><a href="%s">%s</a></li>',
            esc_url( $url ),
            esc_html( $info['nav_label'] )
        );
    }
    return $items . $extra;
}
add_filter( 'wp_nav_menu_items', 'onionpress_social_inject_nav_items', 10, 2 );

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
 * One-shot migration to strip absolute hostnames from <img> / <video>
 * URLs in imported-post content. Earlier imports baked in whatever
 * WordPress's site URL was at import time (typically
 * http://localhost:8080/brewsterkahle/...), which renders fine on that
 * exact URL but breaks when the same post is viewed through the onion
 * (or via clearnet). Switching to root-relative URLs makes each image
 * load from whichever host the viewer is using. Gated by a version
 * option so it fires once per site.
 */
add_action( 'wp_loaded', function () {
    if ( get_option( 'onionpress_social_archive_url_scrub' ) === '1' ) {
        return;
    }
    global $wpdb;
    $rows = $wpdb->get_results( $wpdb->prepare(
        "SELECT p.ID, p.post_content
           FROM {$wpdb->posts} p
           JOIN {$wpdb->postmeta} pm ON p.ID = pm.post_id
          WHERE pm.meta_key = %s
            AND p.post_content LIKE %s
          LIMIT 5000",
        '_source_id',
        '%http%://%'
    ) );
    $fixed = 0;
    foreach ( $rows as $row ) {
        // Rewrite any absolute URL whose host *isn't* twitter.com /
        // mastodon / etc. to a root-relative path. The allow-list makes
        // sure we don't rewrite legit outbound links in tweet content
        // (archive.org, other-user references).
        $new = preg_replace_callback(
            '~\b(https?)://([^/\s"\'<>]+)(/[^\s"\'<>]*)?~',
            function ( $m ) {
                $host = $m[2];
                $path = $m[3] ?? '/';
                // Preserve outbound links to real sites on the public web.
                if ( preg_match( '~(^|\.)(twitter|x|mastodon|bsky|facebook|instagram|tiktok|youtube|archive|github|wikipedia)\.~i', $host ) ) {
                    return $m[0];
                }
                // Preserve other dotted domains too (generic web URLs).
                // Only rewrite what looks like a local reference: the
                // onion hostname, localhost, or 127.0.0.1 — these are
                // where WP's "absolute URL" baked in during import.
                if ( $host === 'localhost'
                     || strpos( $host, 'localhost:' ) === 0
                     || $host === '127.0.0.1'
                     || preg_match( '~\.onion(:\d+)?$~', $host ) ) {
                    return $path;
                }
                return $m[0];
            },
            $row->post_content
        );
        if ( $new !== $row->post_content ) {
            $wpdb->update(
                $wpdb->posts,
                array( 'post_content' => $new ),
                array( 'ID' => (int) $row->ID ),
                array( '%s' ),
                array( '%d' )
            );
            clean_post_cache( $row->ID );
            $fixed++;
        }
    }
    update_option( 'onionpress_social_archive_url_scrub', '1' );
}, 6 );

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

/**
 * Bump posts_per_page to 200 on social-source category archives.
 *
 * The site-wide WordPress "Blog pages show at most" setting stays in
 * force for your own writing; this filter only kicks in on
 * /category/twitter/, /category/mastodon/, etc. Tweets are short
 * and the card rendering is lightweight, so 200 per page is a
 * reasonable browse/scroll unit for an archive of thousands.
 *
 * Gated on main query + front-end + category + source-slug match,
 * so it won't interfere with admin queries, widget queries, or
 * non-social categories.
 */
add_action( 'pre_get_posts', function ( $query ) {
    if ( is_admin() || ! $query->is_main_query() || ! is_category() ) {
        return;
    }
    $term = $query->get_queried_object();
    if ( ! ( $term instanceof WP_Term ) ) {
        return;
    }
    foreach ( onionpress_social_sources() as $info ) {
        if ( isset( $info['cat_slug'] ) && $info['cat_slug'] === $term->slug ) {
            $query->set( 'posts_per_page', 200 );
            return;
        }
    }
} );

/**
 * Wrap imported posts in a tweet-style card.
 *
 * Implemented as a `the_content` filter rather than baked into stored
 * post_content so:
 *   - the stored post stays clean (readable in wp-admin, easy to edit,
 *     friendly to future re-processing);
 *   - every existing import picks up the styling without a migration;
 *   - future importers for Mastodon, Bluesky, etc. inherit the same
 *     card for free just by populating _source_id / _source_url, with
 *     the card's accent color coming from the per-source definition.
 *
 * Source slug is read from the `source:` prefix in _source_id, so a
 * post without that meta (your own original writing) is untouched.
 */
function onionpress_social_wrap_as_card( $content ) {
    if ( ! in_the_loop() || is_admin() || is_feed() ) {
        return $content;
    }
    $post_id = get_the_ID();
    if ( ! $post_id ) {
        return $content;
    }
    $source_id  = get_post_meta( $post_id, '_source_id', true );
    $source_url = get_post_meta( $post_id, '_source_url', true );
    if ( empty( $source_id ) || strpos( $source_id, ':' ) === false ) {
        return $content;
    }
    list( $source_slug ) = explode( ':', $source_id, 2 );
    $sources = onionpress_social_sources();
    if ( ! isset( $sources[ $source_slug ] ) ) {
        return $content;
    }
    $info = $sources[ $source_slug ];

    $post   = get_post( $post_id );
    $author = $post ? get_userdata( $post->post_author ) : null;

    // Display name: fall back from user → "OnionPress archive" rather
    // than blank, since an imported post's author should always read
    // as a person or entity rather than an empty box.
    $display_name = $author && $author->display_name
        ? $author->display_name
        : get_bloginfo( 'name' );
    $avatar_url   = $author ? get_avatar_url( $author->ID, array( 'size' => 96 ) ) : '';

    // Per-source saved handle (e.g. onionpress_social_twitter_handle).
    // The Twitter importer sets this via its admin page; other
    // importers will follow the same convention.
    $handle_opt = 'onionpress_social_' . $source_slug . '_handle';
    $handle     = (string) get_option( $handle_opt, '' );

    // Twitter's "3:57 AM · Nov 22, 2022" format — same shape for
    // every source so the visual rhythm is consistent.
    $ts_display = get_the_time( 'g:i A \&middot; M j, Y', $post_id );

    $host       = $source_url ? parse_url( $source_url, PHP_URL_HOST ) : '';
    $host_label = $host ? esc_html( $host ) : esc_html( $info['label'] );

    $header  = '<div class="op-social-card__head">';
    if ( $avatar_url ) {
        $header .= sprintf(
            '<img class="op-social-card__avatar" src="%s" alt="" loading="lazy">',
            esc_url( $avatar_url )
        );
    }
    $header .= '<div class="op-social-card__identity">'
        . '<span class="op-social-card__name">' . esc_html( $display_name ) . '</span>';
    if ( $handle !== '' ) {
        $header .= '<span class="op-social-card__handle">@' . esc_html( $handle ) . '</span>';
    }
    $header .= '</div>';
    // Source icon pill — small visual cue of which platform this
    // imported from, colored with the source's brand color.
    $header .= sprintf(
        '<span class="op-social-card__badge" style="background:%s;" title="Imported from %s">%s</span>',
        esc_attr( $info['color'] ),
        esc_attr( $info['label'] ),
        esc_html( $info['label'] )
    );
    $header .= '</div>';

    $footer  = '<div class="op-social-card__foot">';
    $footer .= '<span class="op-social-card__ts">' . $ts_display . '</span>';
    if ( $source_url ) {
        $footer .= sprintf(
            ' <span class="op-social-card__sep">&middot;</span> '
                . '<a class="op-social-card__viewlink" href="%s" rel="nofollow noopener" target="_blank">View on %s &rarr;</a>',
            esc_url( $source_url ),
            $host_label
        );
    }
    $footer .= '</div>';

    return sprintf(
        '<div class="op-social-card op-social-card--%s" style="--op-accent:%s;">%s<div class="op-social-card__body">%s</div>%s</div>',
        esc_attr( $source_slug ),
        esc_attr( $info['color'] ),
        $header,
        $content,
        $footer
    );
}
add_filter( 'the_content', 'onionpress_social_wrap_as_card', 20 );

/**
 * Inline stylesheet for the tweet-style card. Emitted once per page
 * via wp_head so we don't pollute post_content with styles that can't
 * be overridden in the theme. Uses CSS custom property --op-accent so
 * each source's brand color lights up the border without us needing
 * one CSS block per source.
 */
function onionpress_social_card_styles() {
    // Only emit on front-end views (skip admin / feeds).
    if ( is_admin() || is_feed() ) {
        return;
    }
    ?>
    <style id="onionpress-social-card-styles">
    .op-social-card {
        max-width: 600px;
        margin: 1.25em 0;
        padding: 1em 1.25em;
        border: 1px solid #e1e8ed;
        border-left: 4px solid var(--op-accent, #1da1f2);
        border-radius: 14px;
        background: #ffffff;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
        line-height: 1.45;
        color: #0f1419;
    }
    .op-social-card__head {
        display: flex;
        align-items: center;
        gap: 0.75em;
        margin-bottom: 0.75em;
    }
    .op-social-card__avatar {
        flex: 0 0 auto;
        width: 44px;
        height: 44px;
        border-radius: 50%;
        object-fit: cover;
        background: #eee;
    }
    .op-social-card__identity {
        display: flex;
        flex-direction: column;
        min-width: 0;
        flex: 1;
    }
    .op-social-card__name {
        font-weight: 700;
        color: #0f1419;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    .op-social-card__handle {
        color: #536471;
        font-size: 0.9em;
    }
    .op-social-card__badge {
        flex: 0 0 auto;
        padding: 0.15em 0.55em;
        border-radius: 999px;
        font-size: 0.72em;
        font-weight: 600;
        color: #fff;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        opacity: 0.85;
    }
    .op-social-card__body {
        font-size: 1.02em;
        color: #0f1419;
        word-wrap: break-word;
    }
    .op-social-card__body p:first-child { margin-top: 0; }
    .op-social-card__body p:last-child  { margin-bottom: 0; }
    .op-social-card__body img,
    .op-social-card__body video {
        max-width: 100%;
        height: auto;
        border-radius: 12px;
        margin: 0.5em 0;
        display: block;
    }
    .op-social-card__body a {
        color: #1d9bf0;
        text-decoration: none;
    }
    .op-social-card__body a:hover { text-decoration: underline; }
    .op-social-card__foot {
        margin-top: 0.9em;
        padding-top: 0.65em;
        border-top: 1px solid #eff3f4;
        color: #536471;
        font-size: 0.85em;
    }
    .op-social-card__foot a { color: #1d9bf0; text-decoration: none; }
    .op-social-card__foot a:hover { text-decoration: underline; }
    .op-social-card__sep { margin: 0 0.25em; }
    @media (prefers-color-scheme: dark) {
        .op-social-card {
            background: #15202b;
            border-color: #38444d;
            color: #e7e9ea;
        }
        .op-social-card__name  { color: #e7e9ea; }
        .op-social-card__body  { color: #e7e9ea; }
        .op-social-card__foot  { border-top-color: #22303c; color: #8b98a5; }
        .op-social-card__handle, .op-social-card__foot { color: #8b98a5; }
    }
    </style>
    <?php
}
add_action( 'wp_head', 'onionpress_social_card_styles' );
