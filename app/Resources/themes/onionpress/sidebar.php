<aside class="sidebar">
    <?php if (is_active_sidebar('sidebar-1')) : ?>
        <?php dynamic_sidebar('sidebar-1'); ?>
    <?php endif; ?>

    <?php
    // Follows — reads from the onionpress_following option (managed in OnionPress Settings)
    $following = get_option('onionpress_following', array());
    $following_names = get_option('onionpress_following_names', array());
    $following_titles = get_option('onionpress_following_titles', array());
    if (!is_array($following)) { $following = array(); }
    if (!is_array($following_names)) { $following_names = array(); }
    if (!is_array($following_titles)) { $following_titles = array(); }
    // Well-known names
    $following_names += array(
        'op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion' => 'onionhome',
        'oheavenfhbohpdjijmxo3xgvvuo6eleyhhorbompoycle6x5eajlp7qd.onion' => 'onionheaven',
    );
    if (!empty($following)) : ?>
        <div class="sidebar-section">
            <h3>Follows</h3>
            <ul>
                <?php foreach ($following as $addr) :
                    if (isset($following_titles[$addr]) && $following_titles[$addr]) {
                        $label = $following_titles[$addr];
                    } elseif (isset($following_names[$addr])) {
                        $label = '@' . $following_names[$addr];
                    } elseif (strlen($addr) > 20) {
                        $label = substr($addr, 0, 12) . '...';
                    } else {
                        $label = $addr;
                    }
                    $href = preg_match('#^https?://#', $addr) ? $addr : 'http://' . $addr . '/';
                ?>
                    <li class="blogroll-item">
                        <a href="<?php echo esc_url($href); ?>">
                            <?php echo esc_html($label); ?>
                        </a>
                    </li>
                <?php endforeach; ?>
            </ul>
        </div>
    <?php endif; ?>

    <?php
    // Recent feed items from followed sites
    $feed_items = function_exists('onionpress_get_aggregated_feed_items')
        ? onionpress_get_aggregated_feed_items(5)
        : array();
    if (!empty($feed_items)) : ?>
        <div class="sidebar-section">
            <h3>Feed</h3>
            <ul>
                <?php foreach ($feed_items as $item) :
                    if (isset($following_titles[$item['source_addr']]) && $following_titles[$item['source_addr']]) {
                        $source = $following_titles[$item['source_addr']];
                    } elseif (isset($following_names[$item['source_addr']])) {
                        $source = '@' . $following_names[$item['source_addr']];
                    } else {
                        $source = $item['source'];
                    }
                ?>
                    <li>
                        <a href="<?php echo esc_url($item['permalink']); ?>">
                            <?php echo esc_html(wp_trim_words($item['title'], 8)); ?>
                        </a>
                        <small class="sidebar-feed-source"><?php echo esc_html($source); ?></small>
                    </li>
                <?php endforeach; ?>
            </ul>
            <?php
            $blogroll_page = get_page_by_path('blogroll');
            if ($blogroll_page) : ?>
                <a class="sidebar-more-link" href="<?php echo esc_url(get_permalink($blogroll_page)); ?>">View all &rarr;</a>
            <?php endif; ?>
        </div>
    <?php endif; ?>

    <?php
    // Recent posts
    $recent = new WP_Query(array(
        'posts_per_page' => 5,
        'post_status'    => 'publish',
    ));
    if ($recent->have_posts()) : ?>
        <div class="sidebar-section">
            <h3>Recent Posts</h3>
            <ul>
                <?php while ($recent->have_posts()) : $recent->the_post(); ?>
                    <li><a href="<?php the_permalink(); ?>"><?php the_title(); ?></a></li>
                <?php endwhile; wp_reset_postdata(); ?>
            </ul>
        </div>
    <?php endif; ?>
</aside>
