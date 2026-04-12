<aside class="sidebar">
    <?php if (is_active_sidebar('sidebar-1')) : ?>
        <?php dynamic_sidebar('sidebar-1'); ?>
    <?php endif; ?>

    <?php
    // Follows — reads from the onionpress_following option (managed in OnionPress Settings)
    $following = get_option('onionpress_following', array());
    if (!is_array($following)) {
        $following = array();
    }
    if (!empty($following)) : ?>
        <div class="sidebar-section">
            <h3>Follows</h3>
            <ul>
                <?php foreach ($following as $addr) :
                    // Display a short label: first 12 chars of the .onion address
                    $label = strlen($addr) > 20 ? substr($addr, 0, 12) . '...' : $addr;
                ?>
                    <li class="blogroll-item">
                        <a href="http://<?php echo esc_attr($addr); ?>/">
                            <?php echo esc_html($label); ?>
                        </a>
                    </li>
                <?php endforeach; ?>
            </ul>
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
