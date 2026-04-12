<?php get_header(); ?>

<?php while (have_posts()) : the_post(); ?>
    <article <?php post_class('post'); ?>>
        <h1 class="post-title"><?php the_title(); ?></h1>
        <div class="post-content">
            <?php the_content(); ?>
        </div>
    </article>
<?php endwhile; ?>

<?php get_footer(); ?>
