<?php
/**
 * Template Name: My Creations
 *
 * Displays files from the My Creations directory as a gallery/list.
 * Requires the OnionPress Creations plugin.
 */

get_header();

$files = class_exists('OnionPress_Creations') ? OnionPress_Creations::get_files() : array();
?>

<article class="post">
    <h1 class="post-title"><?php the_title(); ?></h1>

    <?php if (empty($files)) : ?>
        <div class="creations-empty">
            <p>No creations yet.</p>
            <p>Drop files into <code>~/Documents/OnionPress/Creations/My Creations/</code> and they will appear here automatically.</p>
        </div>
    <?php else : ?>
        <div class="creations-view-toggle">
            <button class="active" data-view="grid">Grid</button>
            <button data-view="list">List</button>
        </div>

        <div class="creations-grid" id="creations-grid">
            <?php foreach ($files as $file) : ?>
                <a href="<?php echo esc_url($file['url']); ?>" class="creation-item">
                    <?php if ($file['is_image']) : ?>
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

        <div class="creations-list" id="creations-list" style="display: none;">
            <?php foreach ($files as $file) : ?>
                <div class="creations-list-item">
                    <span class="creations-list-icon"><?php echo $file['icon']; ?></span>
                    <span class="creations-list-name">
                        <a href="<?php echo esc_url($file['url']); ?>"><?php echo esc_html($file['name']); ?></a>
                    </span>
                    <span class="creations-list-size"><?php echo OnionPress_Creations::format_size($file['size']); ?></span>
                </div>
            <?php endforeach; ?>
        </div>

        <script>
        (function() {
            var buttons = document.querySelectorAll('.creations-view-toggle button');
            var grid = document.getElementById('creations-grid');
            var list = document.getElementById('creations-list');

            buttons.forEach(function(btn) {
                btn.addEventListener('click', function() {
                    buttons.forEach(function(b) { b.classList.remove('active'); });
                    btn.classList.add('active');

                    if (btn.dataset.view === 'grid') {
                        grid.style.display = '';
                        list.style.display = 'none';
                    } else {
                        grid.style.display = 'none';
                        list.style.display = '';
                    }
                });
            });
        })();
        </script>
    <?php endif; ?>
</article>

<?php get_footer(); ?>
