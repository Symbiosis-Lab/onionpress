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
 * When on a category archive page, narrow the built-in WP Archives
 * widget's month list + post counts to posts IN that category.
 *
 * Without these filters the "Archives" widget's counts are site-wide,
 * so on /category/twitter/ a month with 80 tweets + 1 original blog
 * post would read "(81)", which is misleading. Filtered:
 * "(80) tweets in Oct 2022" etc.
 *
 * Also flips on show_post_count for category archives so the user
 * doesn't have to reconfigure the widget manually — on category
 * pages the counts are now the point.
 *
 * Generic (not social-specific): applies whenever is_category() is
 * true, so a regular category also gets filtered monthly counts.
 */
add_filter( 'getarchives_join', function ( $join, $parsed_args ) {
    global $wpdb;
    if ( ! is_category() ) {
        return $join;
    }
    $term = get_queried_object();
    if ( ! ( $term instanceof WP_Term ) ) {
        return $join;
    }
    return $join
        . " INNER JOIN {$wpdb->term_relationships} tr ON {$wpdb->posts}.ID = tr.object_id"
        . " INNER JOIN {$wpdb->term_taxonomy} tt ON tr.term_taxonomy_id = tt.term_taxonomy_id";
}, 10, 2 );

add_filter( 'getarchives_where', function ( $where, $parsed_args ) {
    global $wpdb;
    if ( ! is_category() ) {
        return $where;
    }
    $term = get_queried_object();
    if ( ! ( $term instanceof WP_Term ) ) {
        return $where;
    }
    return $where . $wpdb->prepare(
        ' AND tt.taxonomy = %s AND tt.term_id = %d',
        'category',
        $term->term_id
    );
}, 10, 2 );

add_filter( 'widget_archives_args', function ( $args ) {
    if ( is_category() ) {
        $args['show_post_count'] = true;
    }
    return $args;
} );

add_filter( 'widget_archives_dropdown_args', function ( $args ) {
    if ( is_category() ) {
        $args['show_post_count'] = true;
    }
    return $args;
} );

/**
 * Uniform posts_per_page=200 on every front-end main-query listing
 * (home, category/tag archives, date/author archives, search results).
 * Singular views and admin queries keep WP defaults.
 *
 * Rationale: imported tweets are short and the card rendering is
 * lightweight; 200 per page is a reasonable browse unit, and uniform
 * pagination means one consistent rhythm instead of "10 on the home,
 * 200 on the twitter tab." Pairs with the infinite-scroll footer
 * script below so scrolling doesn't feel like a wall anyway.
 */
add_action( 'pre_get_posts', function ( $query ) {
    if ( is_admin() || ! $query->is_main_query() ) {
        return;
    }
    if ( $query->is_singular() ) {
        return;
    }
    $query->set( 'posts_per_page', 200 );
} );

/**
 * Render a Twitter-style profile header at the top of the main loop
 * when viewing a social-source category archive. Injected via
 * loop_start so the theme's index.php template can handle the post
 * listing itself — no custom category template needed.
 *
 * Gated to main query + front-end + is_category() + source-slug
 * match, so it won't fire on admin listings, widgets, or
 * non-source categories.
 */
add_action( 'loop_start', function ( $query ) {
    if ( ! $query instanceof WP_Query
        || ! $query->is_main_query()
        || is_admin()
        || is_feed()
        || ! is_category() ) {
        return;
    }
    $term = $query->get_queried_object();
    if ( ! ( $term instanceof WP_Term ) ) {
        return;
    }
    foreach ( onionpress_social_sources() as $slug => $info ) {
        if ( isset( $info['cat_slug'] ) && $info['cat_slug'] === $term->slug ) {
            echo onionpress_social_render_profile_header( $slug, $info, $term );
            return;
        }
    }
} );

function onionpress_social_render_profile_header( $source_slug, $info, $term ) {
    $admin_user = get_user_by( 'id', 1 );
    $avatar_url = $admin_user ? get_avatar_url( $admin_user->ID, array( 'size' => 128 ) ) : '';
    $name       = $admin_user ? $admin_user->display_name : get_bloginfo( 'name' );
    $handle_opt = 'onionpress_social_' . $source_slug . '_handle';
    $handle     = (string) get_option( $handle_opt, '' );

    $post_count = intval( $term->count );
    $earliest   = get_posts( array(
        'category'       => $term->term_id,
        'posts_per_page' => 1,
        'orderby'        => 'date',
        'order'          => 'ASC',
    ) );
    $latest = get_posts( array(
        'category'       => $term->term_id,
        'posts_per_page' => 1,
        'orderby'        => 'date',
        'order'          => 'DESC',
    ) );
    $date_range = '';
    if ( $earliest && $latest ) {
        $from = mysql2date( 'M Y', $earliest[0]->post_date );
        $to   = mysql2date( 'M Y', $latest[0]->post_date );
        $date_range = ( $from === $to ) ? $from : "$from &ndash; $to";
    }

    ob_start();
    ?>
    <section class="op-social-profile" style="--op-accent:<?php echo esc_attr( $info['color'] ); ?>;">
        <?php if ( $avatar_url ) : ?>
            <img class="op-social-profile__avatar" src="<?php echo esc_url( $avatar_url ); ?>" alt="">
        <?php endif; ?>
        <div class="op-social-profile__meta">
            <h1 class="op-social-profile__name"><?php echo esc_html( $name ); ?></h1>
            <?php if ( $handle !== '' ) : ?>
                <div class="op-social-profile__handle">@<?php echo esc_html( $handle ); ?></div>
            <?php endif; ?>
            <div class="op-social-profile__source">
                Imported from <strong><?php echo esc_html( $info['label'] ); ?></strong>
            </div>
            <div class="op-social-profile__stats">
                <strong><?php echo number_format_i18n( $post_count ); ?></strong>
                post<?php echo $post_count === 1 ? '' : 's'; ?>
                <?php if ( $date_range ) : ?>
                    &middot; <?php echo $date_range; ?>
                <?php endif; ?>
            </div>
        </div>
    </section>
    <?php
    return (string) ob_get_clean();
}

/**
 * Universal infinite-scroll on any listing page that renders a
 * `.pagination` block. IntersectionObserver watches pagination; when
 * it scrolls into view the script fetches the next page URL from its
 * <a class="next"> link, extracts all <article> elements from the
 * fetched document, and appends them above the (newly-replaced)
 * pagination block. Continues until no next link exists.
 *
 * Layered on top of server-rendered pagination, not a replacement —
 * with JS off or on fetch failure, the normal next-page link is
 * clickable. Crawlers (SPN, Googlebot) see the same server-side
 * <a class="next"> and can follow it without JS.
 */
add_action( 'wp_footer', function () {
    if ( is_admin() || is_feed() || is_singular() ) {
        return;
    }
    ?>
    <script id="op-social-infinite-scroll">
    (function () {
        var seen = new Set();
        var loading = false;
        function currentNext() {
            var p = document.querySelector('.pagination .next');
            return p && p.href ? p.href : null;
        }
        function sig(el) {
            return el.outerHTML.length + ':' + (el.textContent || '').slice(0, 60);
        }
        function loadMore(url) {
            if (loading) return;
            loading = true;
            var pag = document.querySelector('.pagination');
            if (!pag) { loading = false; return; }
            pag.classList.add('is-loading');
            fetch(url, { credentials: 'same-origin' })
                .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
                .then(function (html) {
                    var doc = new DOMParser().parseFromString(html, 'text/html');
                    // Grab the main content area's articles — avoid
                    // sidebar widgets with post-like markup.
                    var newArts = doc.querySelectorAll('.main-content article');
                    newArts.forEach(function (art) {
                        var s = sig(art);
                        if (seen.has(s)) return;
                        seen.add(s);
                        pag.parentNode.insertBefore(art, pag);
                    });
                    var newPag = doc.querySelector('.pagination');
                    if (newPag) {
                        pag.replaceWith(newPag);
                        observe();
                    } else {
                        pag.remove();
                    }
                })
                .catch(function () { /* leave pagination click-through intact */ })
                .finally(function () { loading = false; });
        }
        var io = new IntersectionObserver(function (entries) {
            entries.forEach(function (e) {
                if (!e.isIntersecting) return;
                var n = currentNext();
                if (n) loadMore(n);
            });
        }, { rootMargin: '600px 0px' });
        function observe() {
            var pag = document.querySelector('.pagination');
            if (pag) io.observe(pag);
        }
        // Seed 'seen' with articles already on the page
        document.querySelectorAll('.main-content article').forEach(function (art) {
            seen.add(sig(art));
        });
        observe();
    })();
    </script>
    <style id="op-social-profile-styles">
    .op-social-profile {
        display: flex;
        align-items: center;
        gap: 1.25em;
        max-width: 640px;
        margin: 1em 0 2em;
        padding: 1.25em 1.5em;
        border: 1px solid #e1e8ed;
        border-top: 6px solid var(--op-accent, #1da1f2);
        border-radius: 14px;
        background: #fff;
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
    }
    .op-social-profile__avatar {
        flex: 0 0 auto; width: 80px; height: 80px;
        border-radius: 50%; object-fit: cover; background: #eee;
    }
    .op-social-profile__meta { min-width: 0; flex: 1; }
    .op-social-profile__name  { margin: 0 0 0.15em; font-size: 1.6em; line-height: 1.2; color: #0f1419; }
    .op-social-profile__handle { color: #536471; font-size: 1em; margin-bottom: 0.5em; }
    .op-social-profile__source,
    .op-social-profile__stats  { color: #536471; font-size: 0.9em; line-height: 1.5; }
    .op-social-profile__stats strong { color: #0f1419; }
    @media (prefers-color-scheme: dark) {
        .op-social-profile { background: #15202b; border-color: #38444d; }
        .op-social-profile__name { color: #e7e9ea; }
        .op-social-profile__handle, .op-social-profile__source, .op-social-profile__stats { color: #8b98a5; }
        .op-social-profile__stats strong { color: #e7e9ea; }
    }
    </style>
    <?php
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
 * Twin of the_content filter for excerpt contexts (homepage, search
 * results, date/author archives — anywhere the theme calls
 * the_excerpt() on a listing). Without this, imported posts show as
 * title + plain-text excerpt when scrolling the main blog, which
 * looks wrong next to cards on the category pages and single-tweet
 * pages. Returns the full card HTML instead of the excerpt so a
 * tweet looks like a tweet wherever it appears.
 */
function onionpress_social_wrap_excerpt_as_card( $excerpt ) {
    if ( is_admin() || is_feed() ) {
        return $excerpt;
    }
    $post_id = get_the_ID();
    if ( ! $post_id ) {
        return $excerpt;
    }
    $source_id = get_post_meta( $post_id, '_source_id', true );
    if ( empty( $source_id ) || strpos( $source_id, ':' ) === false ) {
        return $excerpt;
    }
    $post = get_post( $post_id );
    if ( ! $post ) {
        return $excerpt;
    }
    // Render the card using the same code path as the_content so the
    // visual treatment is identical everywhere. The_content filter
    // expects raw post_content and adds the card chrome around it.
    return onionpress_social_wrap_as_card( wpautop( $post->post_content ) );
}
add_filter( 'the_excerpt', 'onionpress_social_wrap_excerpt_as_card', 20 );
add_filter( 'get_the_excerpt', 'onionpress_social_wrap_excerpt_as_card', 20 );

/**
 * Tag the <body> and <article> elements with op-is-social when we're
 * rendering an imported post. The theme-level title + date/author
 * chrome looks redundant above our tweet-style card (the card already
 * shows the author and time); CSS in the card stylesheet below uses
 * these classes to hide the redundant chrome on single-post views,
 * leaving just the card.
 */
add_filter( 'body_class', function ( $classes ) {
    if ( is_singular() ) {
        $post_id = get_queried_object_id();
        if ( $post_id && get_post_meta( $post_id, '_source_id', true ) ) {
            $classes[] = 'op-is-social';
        }
    }
    return $classes;
} );
add_filter( 'post_class', function ( $classes, $class, $post_id ) {
    if ( $post_id && get_post_meta( $post_id, '_source_id', true ) ) {
        $classes[] = 'op-is-social';
    }
    return $classes;
}, 10, 3 );

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
    /* On single-post views of imported content, hide the theme's
       own title and date/author meta — the card already shows those,
       and doubling them up makes an imported tweet look like a blog
       post that happens to contain a tweet. Targets .op-is-social
       on body (single pages) or on article (archive pages) so the
       rule applies either way without cross-theme fragility. */
    body.op-is-social .post-title,
    body.op-is-social .post-meta,
    .op-is-social > .post-title,
    .op-is-social > .post-meta,
    .op-is-social > .read-more { display: none; }
    /* The theme wraps listing excerpts in <div class="post-content">.
       Our card is a block element that should fill the article — no
       extra padding from that wrapper. */
    .op-is-social > .post-content { padding: 0; }
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
