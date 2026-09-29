<?php

if (!defined('ABSPATH')) {
    exit;
}

function pkh_restorer_register_routes() {
    register_rest_route('pkh-restorer/v1', '/health', array(
        'methods' => 'GET',
        'permission_callback' => '__return_true',
        'callback' => 'pkh_restorer_health',
    ));

    register_rest_route('pkh-restorer/v1', '/schema', array(
        'methods' => 'GET',
        'permission_callback' => 'pkh_restorer_can_import',
        'callback' => 'pkh_restorer_schema',
    ));

    register_rest_route('pkh-restorer/v1', '/find', array(
        'methods' => 'GET',
        'permission_callback' => 'pkh_restorer_can_import',
        'callback' => 'pkh_restorer_find',
    ));

    register_rest_route('pkh-restorer/v1', '/import', array(
        'methods' => 'POST',
        'permission_callback' => 'pkh_restorer_can_import',
        'callback' => 'pkh_restorer_import',
    ));

    register_rest_route('pkh-restorer/v1', '/term', array(
        'methods' => 'POST',
        'permission_callback' => 'pkh_restorer_can_import',
        'callback' => 'pkh_restorer_term',
    ));

    register_rest_route('pkh-restorer/v1', '/featured', array(
        'methods' => 'POST',
        'permission_callback' => 'pkh_restorer_can_import',
        'callback' => 'pkh_restorer_featured',
    ));
}

function pkh_restorer_can_import() {
    return current_user_can('edit_posts');
}

function pkh_restorer_health() {
    return array(
        'ok' => true,
        'site' => get_bloginfo('name'),
        'url' => home_url('/'),
    );
}

function pkh_restorer_schema() {
    $post_types = array();
    foreach (get_post_types(array(), 'objects') as $name => $object) {
        $post_types[] = array(
            'name' => $name,
            'rest_base' => $object->rest_base,
            'public' => (bool) $object->public,
        );
    }

    $taxonomies = array();
    foreach (get_taxonomies(array(), 'objects') as $name => $object) {
        $taxonomies[] = array(
            'name' => $name,
            'object_type' => $object->object_type,
            'rest_base' => $object->rest_base,
        );
    }

    global $wp_meta_keys;
    $meta_fields = array();
    if (is_array($wp_meta_keys)) {
        foreach ($wp_meta_keys as $object_type => $types) {
            if ($object_type !== 'post') {
                continue;
            }
            foreach ($types as $type_name => $keys) {
                foreach (array_keys((array) $keys) as $key) {
                    $meta_fields[] = $type_name . ':' . $key;
                }
            }
        }
    }

    return array(
        'post_types' => $post_types,
        'taxonomies' => $taxonomies,
        'meta_fields' => $meta_fields,
        'theme' => wp_get_theme()->get_stylesheet(),
        'active_plugins' => (array) get_option('active_plugins', array()),
    );
}

function pkh_restorer_find(WP_REST_Request $request) {
    $source = $request->get_param('_archive_source_url');
    if (!$source) {
        return array('posts' => array());
    }
    $query = new WP_Query(array(
        'post_type' => pkh_restorer_post_types(),
        'post_status' => 'any',
        'posts_per_page' => 20,
        'meta_key' => '_archive_source_url',
        'meta_value' => $source,
    ));
    $posts = array();
    foreach ($query->posts as $post) {
        $posts[] = array(
            'id' => $post->ID,
            'link' => get_permalink($post),
            'title' => get_the_title($post),
            'type' => $post->post_type,
        );
    }
    return array('posts' => $posts);
}

function pkh_restorer_import(WP_REST_Request $request) {
    $data = $request->get_json_params();
    if (!is_array($data) || !$data) {
        $data = $request->get_params();
    }
    if (empty($data['post_title']) && $request->get_param('title')) {
        $data['post_title'] = $request->get_param('title');
    }
    if (empty($data['post_content']) && $request->get_param('content')) {
        $data['post_content'] = $request->get_param('content');
    }
    if (empty($data['post_name']) && $request->get_param('slug')) {
        $data['post_name'] = $request->get_param('slug');
    }
    $result = pkh_restorer_apply_payload($data);
    if (is_wp_error($result)) {
        return new WP_Error('pkh_import_failed', $result->get_error_message(), array('status' => 500));
    }
    return $result;
}

function pkh_restorer_term(WP_REST_Request $request) {
    $taxonomy = sanitize_key($request->get_param('taxonomy'));
    $name = sanitize_text_field($request->get_param('name'));
    if (!taxonomy_exists($taxonomy)) {
        $taxonomy = 'category';
    }
    $term = term_exists($name, $taxonomy);
    if (!$term) {
        $term = wp_insert_term($name, $taxonomy);
    }
    if (is_wp_error($term)) {
        return new WP_Error('pkh_term_failed', $term->get_error_message(), array('status' => 500));
    }
    $term_id = is_array($term) ? (int) $term['term_id'] : (int) $term;
    return array('id' => $term_id, 'taxonomy' => $taxonomy);
}

function pkh_restorer_featured(WP_REST_Request $request) {
    $post_id = (int) $request->get_param('post_id');
    $media_id = (int) $request->get_param('media_id');
    if ($post_id && $media_id) {
        set_post_thumbnail($post_id, $media_id);
    }
    return array('post_id' => $post_id, 'media_id' => $media_id);
}
