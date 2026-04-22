<?php
/**
 * Category archive template.
 *
 * Handles two cases:
 *
 *  1. A Social Archive source category (Twitter, Mastodon, Bluesky, …).
 *     Renders a Twitter-style profile header at the top of the page
 *     (avatar + display name + @handle + tweet count + date range),
 *     then lists every imported post rendered as a full tweet card
 *     via `the_content()`. The tweet-card styling comes from the
 *     onionpress-social-archive mu-plugin's `the_content` filter, so
 *     this template just needs to drive content (not excerpts).
 *
 *  2. Any other category. Falls back to the standard title + excerpt
 *     listing shape used by index.php.
 *
 * Adding Mastodon / Bluesky imports later doesn't require any change
 * here: new source slugs register in onionpress_social_sources() and
 * the header + card rendering applies automatically.
 */

get_header();

$current_term = get_queried_object();
$is_social_source = false;
$source_info = null;

if ( $current_term instanceof WP_Term
    && function_exists( 'onionpress_social_sources' ) ) {
    foreach ( onionpress_social_sources() as $slug => $info ) {
        if ( isset( $info['cat_slug'] ) && $info['cat_slug'] === $current_term->slug ) {
            $is_social_source = true;
            $source_info      = $info;
            $source_slug      = $slug;
            break;
        }
    }
}

if ( $is_social_source ) :

    // Profile header — Twitter-ish layout above the feed.
    // Author: the site admin (post_author === 1 is typical).
    $admin_user = get_user_by( 'id', 1 );
    $avatar_url = $admin_user ? get_avatar_url( $admin_user->ID, array( 'size' => 128 ) ) : '';
    $name       = $admin_user ? $admin_user->display_name : get_bloginfo( 'name' );
    $handle_opt = 'onionpress_social_' . $source_slug . '_handle';
    $handle     = (string) get_option( $handle_opt, '' );

    // Archive stats — count and date range of imported posts.
    $post_count = intval( $current_term->count );
    $earliest   = get_posts( array(
        'category'       => $current_term->term_id,
        'posts_per_page' => 1,
        'orderby'        => 'date',
        'order'          => 'ASC',
    ) );
    $latest     = get_posts( array(
        'category'       => $current_term->term_id,
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
    ?>

    <section class="op-social-profile" style="--op-accent:<?php echo esc_attr( $source_info['color'] ); ?>;">
        <?php if ( $avatar_url ) : ?>
            <img class="op-social-profile__avatar" src="<?php echo esc_url( $avatar_url ); ?>" alt="">
        <?php endif; ?>
        <div class="op-social-profile__meta">
            <h1 class="op-social-profile__name"><?php echo esc_html( $name ); ?></h1>
            <?php if ( $handle !== '' ) : ?>
                <div class="op-social-profile__handle">@<?php echo esc_html( $handle ); ?></div>
            <?php endif; ?>
            <div class="op-social-profile__source">
                Imported from <strong><?php echo esc_html( $source_info['label'] ); ?></strong>
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

    <style>
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
        flex: 0 0 auto;
        width: 80px;
        height: 80px;
        border-radius: 50%;
        object-fit: cover;
        background: #eee;
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

    <?php if ( have_posts() ) : ?>

        <?php while ( have_posts() ) : the_post(); ?>
            <article <?php post_class( 'op-social-archive-item' ); ?>>
                <?php
                // Full content fires the `the_content` filter, which the
                // onionpress-social-archive mu-plugin uses to wrap each
                // imported post in a tweet-style card.
                the_content();
                ?>
            </article>
        <?php endwhile; ?>

        <div class="pagination">
            <?php
            the_posts_pagination( array(
                'mid_size'  => 2,
                'prev_text' => '&laquo;',
                'next_text' => '&raquo;',
            ) );
            ?>
        </div>

    <?php else : ?>

        <p>No posts in this category yet.</p>

    <?php endif; ?>

<?php else :

    // Non-social categories: render the standard title + excerpt
    // listing shape used by index.php. Duplicated inline rather than
    // load_template()'d so we don't double-call get_header/get_footer.
    if ( have_posts() ) : ?>
        <?php while ( have_posts() ) : the_post(); ?>
            <article <?php post_class( 'post' ); ?>>
                <h2 class="post-title">
                    <a href="<?php the_permalink(); ?>"><?php the_title(); ?></a>
                </h2>
                <div class="post-meta">
                    <?php echo get_the_date(); ?> &middot; <?php the_author(); ?>
                </div>
                <div class="post-content"><?php the_excerpt(); ?></div>
                <?php if ( get_the_content() && str_word_count( get_the_content() ) > str_word_count( get_the_excerpt() ) ) : ?>
                    <a class="read-more" href="<?php the_permalink(); ?>">Read more &rarr;</a>
                <?php endif; ?>
            </article>
        <?php endwhile; ?>
        <div class="pagination">
            <?php the_posts_pagination( array(
                'mid_size'  => 2,
                'prev_text' => '&laquo;',
                'next_text' => '&raquo;',
            ) ); ?>
        </div>
    <?php else : ?>
        <p>No posts in this category yet.</p>
    <?php endif; ?>

<?php endif;

get_footer();
