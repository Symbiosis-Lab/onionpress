<?php
/**
 * Plugin Name: OnionPress My Creations
 * Plugin URI: https://github.com/brewsterkahle/onionpress
 * Description: Displays files from the My Creations directory as a gallery/list page
 * Version: 1.0.0
 * Author: OnionPress
 * Author URI: https://github.com/brewsterkahle/onionpress
 * License: AGPL-3.0
 * Text Domain: onionpress-creations
 */

if (!defined('ABSPATH')) {
    exit;
}

class OnionPress_Creations {

    const CREATIONS_DIR = '/var/www/html/wp-content/creations';

    private static $instance = null;

    public static function get_instance() {
        if (null === self::$instance) {
            self::$instance = new self();
        }
        return self::$instance;
    }

    private function __construct() {
        add_action('template_redirect', array($this, 'serve_file'));
        add_filter('theme_page_templates', array($this, 'register_template'));
        add_filter('template_include', array($this, 'load_template'));
        register_activation_hook(__FILE__, array($this, 'activate'));
    }

    /**
     * Register "My Creations" page template
     */
    public function register_template($templates) {
        $templates['page-creations.php'] = 'My Creations';
        return $templates;
    }

    /**
     * Load the creations template from the plugin if the theme doesn't have one
     */
    public function load_template($template) {
        if (is_page()) {
            $page_template = get_page_template_slug();
            if ($page_template === 'page-creations.php') {
                // Prefer theme's template if it exists
                $theme_template = locate_template('page-creations.php');
                if ($theme_template) {
                    return $theme_template;
                }
                // Fall back to plugin's template
                $plugin_template = plugin_dir_path(__FILE__) . 'page-creations.php';
                if (file_exists($plugin_template)) {
                    return $plugin_template;
                }
            }
        }
        return $template;
    }

    /**
     * Serve files from My Creations directory
     */
    public function serve_file() {
        if (!isset($_GET['onionpress_creation'])) {
            return;
        }

        $requested = sanitize_file_name($_GET['onionpress_creation']);
        $creations_dir = self::CREATIONS_DIR;

        if (!is_dir($creations_dir)) {
            status_header(404);
            exit;
        }

        $filepath = realpath($creations_dir . '/' . $requested);

        // Security: ensure the resolved path is inside creations dir
        if (!$filepath || strpos($filepath, realpath($creations_dir)) !== 0) {
            status_header(404);
            exit;
        }

        if (!is_file($filepath) || !is_readable($filepath)) {
            status_header(404);
            exit;
        }

        $mime = wp_check_filetype($requested);
        $content_type = $mime['type'] ? $mime['type'] : 'application/octet-stream';

        header('Content-Type: ' . $content_type);
        header('Content-Length: ' . filesize($filepath));

        // Allow images/media to be displayed inline, force download for others
        $inline_types = array('image/', 'video/', 'audio/', 'text/html', 'text/plain', 'application/pdf');
        $is_inline = false;
        foreach ($inline_types as $type) {
            if (strpos($content_type, $type) === 0) {
                $is_inline = true;
                break;
            }
        }

        if (!$is_inline) {
            header('Content-Disposition: attachment; filename="' . $requested . '"');
        }

        readfile($filepath);
        exit;
    }

    /**
     * Create "My Creations" page on plugin activation
     */
    public function activate() {
        $page = get_page_by_path('my-creations');
        if ($page) {
            return;
        }

        wp_insert_post(array(
            'post_title'    => 'My Creations',
            'post_name'     => 'my-creations',
            'post_status'   => 'publish',
            'post_type'     => 'page',
            'post_content'  => '',
            'page_template' => 'page-creations.php',
        ));
    }

    /**
     * Helper: format file size for display
     */
    public static function format_size($bytes) {
        if ($bytes >= 1073741824) {
            return number_format($bytes / 1073741824, 1) . ' GB';
        } elseif ($bytes >= 1048576) {
            return number_format($bytes / 1048576, 1) . ' MB';
        } elseif ($bytes >= 1024) {
            return number_format($bytes / 1024, 0) . ' KB';
        }
        return $bytes . ' B';
    }

    /**
     * Helper: get file type icon
     */
    public static function file_icon($filename) {
        $ext = strtolower(pathinfo($filename, PATHINFO_EXTENSION));
        $icons = array(
            'jpg'  => '&#x1f5bc;',
            'jpeg' => '&#x1f5bc;',
            'png'  => '&#x1f5bc;',
            'gif'  => '&#x1f5bc;',
            'webp' => '&#x1f5bc;',
            'svg'  => '&#x1f5bc;',
            'pdf'  => '&#x1f4c4;',
            'html' => '&#x1f310;',
            'htm'  => '&#x1f310;',
            'mp3'  => '&#x1f3b5;',
            'wav'  => '&#x1f3b5;',
            'ogg'  => '&#x1f3b5;',
            'flac' => '&#x1f3b5;',
            'mp4'  => '&#x1f3ac;',
            'webm' => '&#x1f3ac;',
            'mov'  => '&#x1f3ac;',
            'avi'  => '&#x1f3ac;',
            'txt'  => '&#x1f4dd;',
            'md'   => '&#x1f4dd;',
            'zip'  => '&#x1f4e6;',
            'tar'  => '&#x1f4e6;',
            'gz'   => '&#x1f4e6;',
        );
        return isset($icons[$ext]) ? $icons[$ext] : '&#x1f4ce;';
    }

    /**
     * Helper: is this an image file?
     */
    public static function is_image($filename) {
        $ext = strtolower(pathinfo($filename, PATHINFO_EXTENSION));
        return in_array($ext, array('jpg', 'jpeg', 'png', 'gif', 'webp', 'svg'));
    }

    /**
     * Get all files in the creations directory
     */
    public static function get_files() {
        $creations_dir = self::CREATIONS_DIR;
        $files = array();

        if (!is_dir($creations_dir)) {
            return $files;
        }

        $entries = scandir($creations_dir);
        foreach ($entries as $entry) {
            if ($entry[0] === '.') {
                continue;
            }
            $path = $creations_dir . '/' . $entry;
            if (is_file($path)) {
                $files[] = array(
                    'name'     => $entry,
                    'path'     => $path,
                    'size'     => filesize($path),
                    'time'     => filemtime($path),
                    'is_image' => self::is_image($entry),
                    'icon'     => self::file_icon($entry),
                    'url'      => add_query_arg('onionpress_creation', urlencode($entry), home_url('/')),
                );
            }
        }

        // Sort by modification time, newest first
        usort($files, function($a, $b) {
            return $b['time'] - $a['time'];
        });

        return $files;
    }
}

OnionPress_Creations::get_instance();
