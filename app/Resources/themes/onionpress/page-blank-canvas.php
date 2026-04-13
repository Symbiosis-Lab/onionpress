<?php
/*
Template Name: Blank Canvas
Description: Renders a page with no theme header/footer chrome — just the
             page's own content, suitable for landing pages that ship
             their own HTML + CSS (e.g. the OnionPress product home).
*/
?><!DOCTYPE html>
<html <?php language_attributes(); ?>>
<head>
<meta charset="<?php bloginfo( 'charset' ); ?>">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title><?php echo esc_html( wp_get_document_title() ); ?></title>
<?php wp_head(); ?>
</head>
<body <?php body_class(); ?>>
<?php
while ( have_posts() ) :
    the_post();
    the_content();
endwhile;
wp_footer();
?>
</body>
</html>
