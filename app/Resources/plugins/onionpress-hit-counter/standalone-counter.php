<?php
/**
 * Standalone Hit Counter API Endpoint
 *
 * Usage:
 *   GET  /wp-content/plugins/onionpress-hit-counter/standalone-counter.php?action=get
 *   POST /wp-content/plugins/onionpress-hit-counter/standalone-counter.php?action=increment
 *
 * Uses SHORTINIT so wp-settings.php returns after $wpdb is ready, before
 * plugins / mu-plugins / theme / hooks load. Counter state still lives in
 * wp_options for backup compatibility; we just talk to the table directly.
 */

define('SHORTINIT', true);

$wp_load = dirname(dirname(dirname(dirname(__FILE__)))) . '/wp-load.php';
if (!file_exists($wp_load)) {
    header('Content-Type: application/json');
    http_response_code(500);
    echo json_encode(['success' => false, 'error' => 'WordPress not found']);
    exit;
}
require_once $wp_load;

/** @var wpdb $wpdb */
global $wpdb;

$option_name = 'onionpress_hit_counter';
$action = isset($_GET['action']) ? $_GET['action'] : (isset($_POST['action']) ? $_POST['action'] : 'get');

header('Content-Type: application/json');

if ($action === 'increment') {
    // Atomic increment; handles first-run (row may not exist yet).
    $wpdb->query($wpdb->prepare(
        "INSERT INTO {$wpdb->options} (option_name, option_value, autoload)
         VALUES (%s, '1', 'no')
         ON DUPLICATE KEY UPDATE option_value = CAST(option_value AS UNSIGNED) + 1",
        $option_name
    ));
}

$count = (int) $wpdb->get_var($wpdb->prepare(
    "SELECT option_value FROM {$wpdb->options} WHERE option_name = %s",
    $option_name
));

echo json_encode([
    'success' => true,
    'count' => $count,
    'formatted' => str_pad((string) $count, 6, '0', STR_PAD_LEFT),
]);
