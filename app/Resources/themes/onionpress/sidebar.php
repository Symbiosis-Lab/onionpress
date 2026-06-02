<aside class="sidebar">
    <?php if (is_active_sidebar('sidebar-1')) : ?>
        <?php dynamic_sidebar('sidebar-1'); ?>
    <?php endif; ?>

    <?php
    // Follows — reads from the onionpress_following option (managed in OnionPress Settings).
    // Keys are canonical follow keys: a URL, or <addr>.onion[/<onionname>].
    $following = get_option('onionpress_following', array());
    $following_titles = get_option('onionpress_following_titles', array());
    if (!is_array($following)) { $following = array(); }
    if (!is_array($following_titles)) { $following_titles = array(); }
    if (!empty($following)) : ?>
        <div class="sidebar-section">
            <h3>Follows</h3>
            <ul>
                <?php foreach ($following as $key) :
                    $p = function_exists('onionpress_follow_parse')
                        ? onionpress_follow_parse($key)
                        : array('type' => preg_match('#^https?://#i', $key) ? 'url' : 'onion', 'addr' => $key, 'name' => '', 'url' => $key);
                    if (isset($following_titles[$key]) && $following_titles[$key]) {
                        $label = $following_titles[$key];
                    } elseif ('' !== $p['name']) {
                        $label = '@' . $p['name'];
                    } elseif ('url' === $p['type']) {
                        $label = $p['url'];
                    } else {
                        $label = substr($p['addr'], 0, 12) . '...';
                    }
                    $href = function_exists('onionpress_follow_site_url')
                        ? onionpress_follow_site_url($key)
                        : (preg_match('#^https?://#i', $key) ? $key : 'http://' . $key . '/');
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
                    $sp = function_exists('onionpress_follow_parse')
                        ? onionpress_follow_parse($item['source_addr'])
                        : array('name' => '');
                    if (isset($following_titles[$item['source_addr']]) && $following_titles[$item['source_addr']]) {
                        $source = $following_titles[$item['source_addr']];
                    } elseif (!empty($sp['name'])) {
                        $source = '@' . $sp['name'];
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
