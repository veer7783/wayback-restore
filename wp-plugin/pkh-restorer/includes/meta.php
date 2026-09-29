<?php

if (!defined('ABSPATH')) {
    exit;
}

function pkh_restorer_meta_keys() {
    return array(
        '_archive_source_url',
        '_archive_snapshot_url',
        '_archive_timestamp',
        '_archive_imported_at',
        '_pkh_date',
        '_pkh_murdered',
        '_pkh_perpetrators',
        '_pkh_were_you_there',
        '_pkh_collected_by',
        '_pkh_source',
        '_pkh_location',
        '_pkh_source_id',
    );
}

function pkh_restorer_post_types() {
    $types = array('post', 'page');
    if (post_type_exists('listing')) {
        $types[] = 'listing';
    }
    return $types;
}

function pkh_restorer_register_meta() {
    foreach (pkh_restorer_post_types() as $post_type) {
        foreach (pkh_restorer_meta_keys() as $key) {
            register_post_meta($post_type, $key, array(
                'show_in_rest' => true,
                'single' => true,
                'type' => 'string',
                'auth_callback' => function () {
                    return current_user_can('edit_posts');
                },
            ));
        }
    }
}
