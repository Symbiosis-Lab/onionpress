<?php
/**
 * Template Name: Blogroll
 *
 * Aggregated RSS/Atom feed from all followed sites.
 */

get_header();

$items = function_exists( 'onionpress_get_aggregated_feed_items' )
    ? onionpress_get_aggregated_feed_items( 30 )
    : array();
$titles_map = get_option( 'onionpress_following_titles', array() );
if ( ! is_array( $titles_map ) ) { $titles_map = array(); }
?>

<article class="post">
    <h1 class="post-title"><?php the_title(); ?></h1>

    <?php if ( empty( $items ) ) : ?>
        <p>No feed items yet. Follow some sites from
           <a href="<?php echo esc_url( admin_url( 'admin.php?page=onionpress-settings' ) ); ?>">OnionPress Settings</a>
           to see their posts here.</p>
    <?php else : ?>
        <div class="blogroll-feed">
            <?php foreach ( $items as $item ) :
                $sp = function_exists( 'onionpress_follow_parse' )
                    ? onionpress_follow_parse( $item['source_addr'] )
                    : array( 'name' => '' );
                if ( isset( $titles_map[ $item['source_addr'] ] ) && $titles_map[ $item['source_addr'] ] ) {
                    $source_label = $titles_map[ $item['source_addr'] ];
                } elseif ( ! empty( $sp['name'] ) ) {
                    $source_label = '@' . $sp['name'];
                } else {
                    $source_label = $item['source'];
                }
                $date_display = $item['date'] ? date( 'M j, Y', $item['date'] ) : '';
            ?>
                <div class="blogroll-feed-item">
                    <div class="blogroll-feed-meta">
                        <span class="blogroll-feed-source"><?php echo esc_html( $source_label ); ?></span>
                        <?php if ( $date_display ) : ?>
                            <span class="blogroll-feed-date"><?php echo esc_html( $date_display ); ?></span>
                        <?php endif; ?>
                    </div>
                    <h2 class="blogroll-feed-title">
                        <a href="<?php echo esc_url( $item['permalink'] ); ?>">
                            <?php echo esc_html( $item['title'] ); ?>
                        </a>
                    </h2>
                    <?php if ( $item['excerpt'] ) : ?>
                        <p class="blogroll-feed-excerpt"><?php echo esc_html( $item['excerpt'] ); ?></p>
                    <?php endif; ?>
                </div>
            <?php endforeach; ?>
        </div>
    <?php endif; ?>
</article>

<?php get_sidebar(); ?>
<?php get_footer(); ?>
