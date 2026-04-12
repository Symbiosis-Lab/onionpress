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

<?php get_footer(); ?>
