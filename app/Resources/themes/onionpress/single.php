<?php get_header(); ?>

<?php while (have_posts()) : the_post(); ?>
    <article <?php post_class('post'); ?>>
        <h1 class="post-title"><?php the_title(); ?></h1>
        <div class="post-meta">
            <?php echo get_the_date(); ?>
            &middot;
            <?php the_author(); ?>
        </div>
        <div class="post-content">
            <?php the_content(); ?>
        </div>
    </article>

    <?php
    if (comments_open() || get_comments_number()) {
        comments_template();
    }
    ?>

    <div class="pagination">
        <?php
        the_post_navigation(array(
            'prev_text' => '&laquo; %title',
            'next_text' => '%title &raquo;',
        ));
        ?>
    </div>
<?php endwhile; ?>

<?php get_footer(); ?>
