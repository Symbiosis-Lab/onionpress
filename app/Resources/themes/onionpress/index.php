<?php get_header(); ?>

<?php if (have_posts()) : ?>

    <?php while (have_posts()) : the_post(); ?>
        <article <?php post_class('post'); ?>>
            <h2 class="post-title">
                <a href="<?php the_permalink(); ?>"><?php the_title(); ?></a>
            </h2>
            <div class="post-meta">
                <?php echo get_the_date(); ?>
                &middot;
                <?php the_author(); ?>
            </div>
            <div class="post-content">
                <?php the_excerpt(); ?>
            </div>
            <?php if (get_the_content() && str_word_count(get_the_content()) > str_word_count(get_the_excerpt())) : ?>
                <a class="read-more" href="<?php the_permalink(); ?>">Read more &rarr;</a>
            <?php endif; ?>
        </article>
    <?php endwhile; ?>

    <div class="pagination">
        <?php
        the_posts_pagination(array(
            'mid_size'  => 2,
            'prev_text' => '&laquo;',
            'next_text' => '&raquo;',
        ));
        ?>
    </div>

<?php else : ?>

    <article class="post">
        <h2 class="post-title">Welcome to your OnionPress site</h2>
        <div class="post-content">
            <p>Your onion service is up and running. Start writing your first post from the
               <a href="<?php echo esc_url(admin_url()); ?>">WordPress dashboard</a>.</p>
        </div>
    </article>

<?php endif; ?>

<?php
// Recent My Creations preview
if (class_exists('OnionPress_Creations')) :
    $recent_creations = array_slice(OnionPress_Creations::get_files(), 0, 4);
    if (!empty($recent_creations)) :
        $creations_page = get_page_by_path('my-creations');
        $creations_url = $creations_page ? get_permalink($creations_page) : '';
?>
    <section class="recent-creations">
        <h2 class="recent-creations-heading">
            <?php if ($creations_url) : ?>
                <a href="<?php echo esc_url($creations_url); ?>">My Creations</a>
            <?php else : ?>
                My Creations
            <?php endif; ?>
        </h2>
        <div class="recent-creations-grid">
            <?php foreach ($recent_creations as $file) : ?>
                <a href="<?php echo $creations_url ? esc_url($creations_url) : esc_url($file['url']); ?>" class="creation-item">
                    <?php if (!empty($file['thumb_url'])) : ?>
                        <img class="creation-thumbnail" src="<?php echo esc_url($file['thumb_url']); ?>" alt="<?php echo esc_attr($file['name']); ?>">
                    <?php elseif ($file['is_image']) : ?>
                        <img class="creation-thumbnail" src="<?php echo esc_url($file['url']); ?>" alt="<?php echo esc_attr($file['name']); ?>">
                    <?php else : ?>
                        <div class="creation-thumbnail">
                            <span class="creation-icon"><?php echo $file['icon']; ?></span>
                        </div>
                    <?php endif; ?>
                    <div class="creation-info">
                        <div class="creation-name"><?php echo esc_html($file['name']); ?></div>
                        <div class="creation-meta"><?php echo OnionPress_Creations::format_size($file['size']); ?></div>
                    </div>
                </a>
            <?php endforeach; ?>
        </div>
        <?php if ($creations_url) : ?>
            <a class="recent-creations-more" href="<?php echo esc_url($creations_url); ?>">View all &rarr;</a>
        <?php endif; ?>
    </section>
<?php
    endif;
endif;
?>

<?php get_footer(); ?>
