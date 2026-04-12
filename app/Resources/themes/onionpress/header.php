<!DOCTYPE html>
<html <?php language_attributes(); ?>>
<head>
    <meta charset="<?php bloginfo('charset'); ?>">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <?php wp_head(); ?>
</head>
<body <?php body_class(); ?>>
<?php wp_body_open(); ?>

<header class="site-header">
    <?php if (get_header_image()) : ?>
        <img class="header-cover" src="<?php header_image(); ?>" alt="">
    <?php else : ?>
        <div class="header-cover-placeholder"></div>
    <?php endif; ?>

    <div class="header-inner">
        <div class="header-profile">
            <?php
            $avatar_url = onionpress_get_avatar_url();
            if ($avatar_url) : ?>
                <img class="header-avatar" src="<?php echo esc_url($avatar_url); ?>" alt="">
            <?php else : ?>
                <div class="header-avatar-placeholder">
                    <?php echo esc_html(onionpress_get_avatar_letter()); ?>
                </div>
            <?php endif; ?>

            <div class="header-info">
                <h1 class="site-title">
                    <a href="<?php echo esc_url(home_url('/')); ?>">
                        <?php bloginfo('name'); ?>
                    </a>
                </h1>
                <?php $description = get_bloginfo('description', 'display');
                if ($description) : ?>
                    <p class="site-description"><?php echo esc_html($description); ?></p>
                <?php endif; ?>
                <?php
                $onion_addr = function_exists('onionpress_follow_get_own_address')
                    ? onionpress_follow_get_own_address() : null;
                if ($onion_addr) :
                    $onionhome = defined('ONIONHOME_ADDRESS') ? ONIONHOME_ADDRESS
                        : 'op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion';
                    $follow_url = 'http://' . $onionhome . '/follow?address=' . urlencode($onion_addr); ?>
                    <a class="header-follow" href="<?php echo esc_url($follow_url); ?>">+ Follow</a>
                <?php endif; ?>
            </div>
        </div>
    </div>
</header>

<nav class="site-nav">
    <?php
    if (has_nav_menu('primary')) {
        wp_nav_menu(array(
            'theme_location' => 'primary',
            'container'      => false,
            'fallback_cb'    => false,
        ));
    } else {
        // Default nav when no menu is configured
        echo '<ul>';
        echo '<li><a href="' . esc_url(home_url('/')) . '">Home</a></li>';
        $creations_page = get_page_by_path('my-creations');
        if ($creations_page) {
            echo '<li><a href="' . esc_url(get_permalink($creations_page)) . '">My Creations</a></li>';
        }
        echo '</ul>';
    }
    ?>
</nav>

<div class="site-wrapper">
    <div class="content-area">
        <main class="main-content">
