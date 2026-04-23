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
            $can_set_avatar = current_user_can('upload_files') && !get_user_meta(get_current_user_id(), 'onionpress_avatar_id', true);
            $profile_url = $can_set_avatar ? admin_url('profile.php#onionpress-avatar') : '';
            if ($avatar_url) : ?>
                <img class="header-avatar" src="<?php echo esc_url($avatar_url); ?>" alt="">
            <?php elseif ($profile_url) : ?>
                <a href="<?php echo esc_url($profile_url); ?>" class="header-avatar-placeholder header-avatar-add" title="Add profile photo">
                    <span>+</span>
                </a>
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
                <?php
                $onionname = function_exists('onionpress_get_onionname')
                    ? onionpress_get_onionname() : '';
                $onion_addr = function_exists('onionpress_follow_get_own_address')
                    ? onionpress_follow_get_own_address() : null;
                if ($onionname) : ?>
                    <p class="site-onionname"><?php echo esc_html($onionname); ?><?php if ($onion_addr) : ?><span class="site-onion-host">@<?php echo esc_html($onion_addr); ?></span><?php endif; ?></p>
                <?php endif; ?>
                <?php $description = get_bloginfo('description', 'display');
                if ($description) : ?>
                    <p class="site-description"><?php echo esc_html($description); ?></p>
                <?php endif; ?>
                <?php
                if ($onion_addr) :
                    $onionhome = defined('ONIONHOME_ADDRESS') ? ONIONHOME_ADDRESS
                        : 'op2homeiwjb4fdqnfkj5kbokvcee45zpk2pwgvpz5rrkanp5qqwxzbyd.onion';
                    // Prefer the onionname-based follow URL so visitors get a
                    // short, human-readable link; OnionHome resolves it.
                    if ($onionname) {
                        $follow_url = 'http://' . $onionhome . '/follow?name=' . urlencode($onionname);
                    } else {
                        $follow_url = 'http://' . $onionhome . '/follow?address=' . urlencode($onion_addr);
                    }
                    ?>
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

        // Social Archive tabs — one link per source that has imported
        // posts (e.g. "My Tweets" → /category/twitter/). Labels come
        // from the source definitions in onionpress-social-archive.php
        // (mu-plugin; guaranteed loaded by this point).
        if (function_exists('onionpress_social_sources')) {
            foreach (onionpress_social_sources() as $slug => $info) {
                $term = get_term_by('slug', $info['cat_slug'], 'category');
                if ($term && !is_wp_error($term) && $term->count > 0) {
                    $cat_url = get_term_link($term);
                    if (!is_wp_error($cat_url)) {
                        $icon_html = function_exists('onionpress_social_nav_icon_html')
                            ? onionpress_social_nav_icon_html($slug)
                            : '';
                        echo '<li><a class="op-social-nav op-social-nav--' . esc_attr($slug) . '" '
                            . 'href="' . esc_url($cat_url) . '" '
                            . 'style="--op-accent:' . esc_attr($info['color']) . ';" '
                            . 'aria-label="' . esc_attr($info['nav_label']) . '">'
                            . $icon_html
                            . '<span class="op-social-nav-label">' . esc_html($info['nav_label']) . '</span>'
                            . '</a></li>';
                    }
                }
            }
        }

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
