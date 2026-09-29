<?php
/**
 * WP-CLI import entry: php wp-cli.phar eval-file import-cli.php
 * Reads JSON path from PKH_IMPORT_JSON.
 */

if (!defined('ABSPATH')) {
    exit;
}

$path = getenv('PKH_IMPORT_JSON');
if (!$path) {
    fwrite(STDERR, "PKH_IMPORT_JSON is not set\n");
    return;
}

$data = pkh_restorer_json_from_file($path);
if (is_wp_error($data)) {
    fwrite(STDERR, $data->get_error_message() . "\n");
    return;
}

$result = pkh_restorer_apply_payload($data);
if (is_wp_error($result)) {
    fwrite(STDERR, $result->get_error_message() . "\n");
    return;
}

echo wp_json_encode($result);
