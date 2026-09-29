<?php
/**
 * Plugin Name: PKH Restorer
 * Description: Idempotent restoration helpers for Project Hindu Kush Wayback imports.
 * Version: 0.1.0
 * Author: Project Hindu Kush
 */

if (!defined('ABSPATH')) {
    exit;
}

require_once __DIR__ . '/includes/meta.php';
require_once __DIR__ . '/includes/import.php';
require_once __DIR__ . '/includes/rest.php';

add_action('init', 'pkh_restorer_register_meta');
add_action('rest_api_init', 'pkh_restorer_register_routes');
add_filter('register_post_type_args', 'pkh_restorer_listing_rewrite', 10, 2);

function pkh_restorer_listing_rewrite($args, $post_type) {
    if ($post_type !== 'listing') {
        return $args;
    }
    $opts = get_option('listingpro_options');
    $slug = 'incident';
    if (is_array($opts) && !empty($opts['listing_slug'])) {
        $slug = $opts['listing_slug'];
    }
    $args['rewrite'] = array('slug' => $slug);
    return $args;
}
