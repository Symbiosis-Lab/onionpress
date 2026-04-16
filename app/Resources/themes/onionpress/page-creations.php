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

    <?php if (current_user_can('upload_files')) : ?>
        <div class="creations-upload">
            <form method="post" enctype="multipart/form-data" id="creations-upload-form">
                <?php wp_nonce_field('creations_upload', 'creations_upload_nonce'); ?>
                <label class="creations-upload-area" id="creations-drop-area">
                    <input type="file" name="creations_upload[]" multiple style="display:none" id="creations-file-input">
                    <span class="creations-upload-text">Drop files here or click to upload</span>
                </label>
            </form>
        </div>
        <script>
        (function() {
            var area = document.getElementById('creations-drop-area');
            var input = document.getElementById('creations-file-input');
            var form = document.getElementById('creations-upload-form');
            area.addEventListener('dragover', function(e) { e.preventDefault(); area.classList.add('dragover'); });
            area.addEventListener('dragleave', function() { area.classList.remove('dragover'); });
            area.addEventListener('drop', function(e) {
                e.preventDefault();
                area.classList.remove('dragover');
                input.files = e.dataTransfer.files;
                form.submit();
            });
            input.addEventListener('change', function() { if (input.files.length) form.submit(); });
        })();
        </script>
    <?php endif; ?>

    <?php if (empty($files)) : ?>
        <div class="creations-empty">
            <p>No creations yet.</p>
            <?php if (current_user_can('upload_files')) : ?>
                <p>Upload files above or drop them into <code>~/Documents/OnionPress/Creations/My Creations/</code></p>
            <?php endif; ?>
        </div>
    <?php else : ?>
        <div class="creations-view-toggle">
            <button class="active" data-view="grid">Grid</button>
            <button data-view="list">List</button>
        </div>

        <div class="creations-grid" id="creations-grid">
            <?php foreach ($files as $file) : ?>
                <a href="<?php echo esc_url($file['url']); ?>" class="creation-item">
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
                        <div class="creation-meta">
                            <?php echo OnionPress_Creations::format_size($file['size']); ?>
                            <?php if (!empty($file['rel_path']) && dirname($file['rel_path']) !== '.') : ?>
                                &middot; <?php echo esc_html(dirname($file['rel_path'])); ?>
                            <?php endif; ?>
                        </div>
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
                        <?php if (!empty($file['rel_path']) && dirname($file['rel_path']) !== '.') : ?>
                            <span class="creations-list-folder"><?php echo esc_html(dirname($file['rel_path'])); ?></span>
                        <?php endif; ?>
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
