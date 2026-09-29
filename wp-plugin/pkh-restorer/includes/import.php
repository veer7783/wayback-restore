<?php

if (!defined('ABSPATH')) {
    exit;
}

function pkh_restorer_json_from_file($path) {
    if (!$path || !is_readable($path)) {
        return new WP_Error('pkh_missing_json', 'Import JSON not readable: ' . $path);
    }
    $raw = file_get_contents($path);
    $data = json_decode($raw, true);
    if (!is_array($data)) {
        return new WP_Error('pkh_bad_json', 'Import JSON is invalid');
    }
    return $data;
}

function pkh_restorer_ensure_form_field($title, $slug, $type = 'text') {
    $title = sanitize_text_field($title);
    $slug = $slug ? sanitize_title($slug) : sanitize_title($title);
    $existing = get_page_by_path($slug, OBJECT, 'form-fields');
    if ($existing) {
        return (int) $existing->ID;
    }
    $post_id = wp_insert_post(array(
        'post_title' => $title,
        'post_name' => $slug,
        'post_status' => 'publish',
        'post_type' => 'form-fields',
    ), true);
    if (is_wp_error($post_id)) {
        return $post_id;
    }
    $options = array(
        'field-type' => $type ?: 'text',
        'radio-options' => '',
        'select-options' => '',
        'multicheck-options' => '',
        'exclusive_field' => '',
        'field-cat' => array(),
    );
    update_post_meta($post_id, 'lp_listingpro_options', $options);
    return (int) $post_id;
}

function pkh_restorer_find_listing($data) {
    $meta = isset($data['meta']) && is_array($data['meta']) ? $data['meta'] : array();
    $queries = array();
    if (!empty($meta['_pkh_source_id'])) {
        $queries[] = array(
            'post_type' => pkh_restorer_post_types(),
            'post_status' => 'any',
            'numberposts' => 1,
            'meta_key' => '_pkh_source_id',
            'meta_value' => $meta['_pkh_source_id'],
        );
    }
    if (!empty($meta['_archive_source_url'])) {
        $source = rtrim($meta['_archive_source_url'], '/');
        foreach (array($source, $source . '/') as $value) {
            $queries[] = array(
                'post_type' => pkh_restorer_post_types(),
                'post_status' => 'any',
                'numberposts' => 1,
                'meta_key' => '_archive_source_url',
                'meta_value' => $value,
            );
        }
    }
    foreach ($queries as $query) {
        $found = get_posts($query);
        if ($found) {
            return $found[0];
        }
    }
    $slug = isset($data['post_name']) ? sanitize_title($data['post_name']) : '';
    $post_type = !empty($data['post_type']) ? $data['post_type'] : 'listing';
    if ($slug && post_type_exists($post_type)) {
        $found = get_posts(array(
            'name' => $slug,
            'post_type' => $post_type,
            'post_status' => 'any',
            'numberposts' => 1,
        ));
        if ($found) {
            return $found[0];
        }
    }
    return null;
}

function pkh_restorer_import_attachment($local_path, $title, $original_url, $parent_id, $archive_url = '') {
    $original_url = esc_url_raw($original_url);
    $archive_url = esc_url_raw($archive_url);
    if ($original_url) {
        $existing = get_posts(array(
            'post_type' => 'attachment',
            'post_status' => 'inherit',
            'numberposts' => 1,
            'meta_key' => '_archive_original_url',
            'meta_value' => $original_url,
        ));
        if ($existing) {
            $existing_id = (int) $existing[0]->ID;
            if ($archive_url) {
                update_post_meta($existing_id, '_archive_snapshot_url', $archive_url);
            }
            return array(
                'id' => $existing_id,
                'created' => false,
                'url' => wp_get_attachment_url($existing_id),
            );
        }
    }
    if (!$local_path || !file_exists($local_path)) {
        return array('id' => 0, 'created' => false, 'url' => '');
    }

    require_once ABSPATH . 'wp-admin/includes/file.php';
    require_once ABSPATH . 'wp-admin/includes/media.php';
    require_once ABSPATH . 'wp-admin/includes/image.php';

    $filename = wp_unique_filename(wp_upload_dir()['path'], basename($local_path));
    $bits = wp_upload_bits($filename, null, file_get_contents($local_path));
    if (!empty($bits['error'])) {
        return array('id' => 0, 'created' => false, 'url' => '');
    }
    $filetype = wp_check_filetype($filename);
    $attachment_id = wp_insert_attachment(array(
        'post_mime_type' => $filetype['type'] ?: 'image/jpeg',
        'post_title' => sanitize_text_field($title ?: $filename),
        'post_content' => '',
        'post_status' => 'inherit',
        'guid' => $bits['url'],
    ), $bits['file'], $parent_id);
    if (is_wp_error($attachment_id) || !$attachment_id) {
        return array('id' => 0, 'created' => false, 'url' => '');
    }
    $metadata = wp_generate_attachment_metadata($attachment_id, $bits['file']);
    wp_update_attachment_metadata($attachment_id, $metadata);
    if ($original_url) {
        update_post_meta($attachment_id, '_archive_original_url', $original_url);
    }
    if ($archive_url) {
        update_post_meta($attachment_id, '_archive_snapshot_url', $archive_url);
    }
    return array(
        'id' => (int) $attachment_id,
        'created' => true,
        'url' => $bits['url'],
    );
}

function pkh_restorer_attachment_id($result) {
    if (is_array($result)) {
        return (int) ($result['id'] ?? 0);
    }
    return (int) $result;
}

function pkh_restorer_rewrite_content_media($content, array $url_map) {
    foreach ($url_map as $original => $local) {
        if (!$original || !$local) {
            continue;
        }
        $content = str_replace($original, $local, $content);
        $content = str_replace(set_url_scheme($original, 'http'), $local, $content);
        $content = str_replace(set_url_scheme($original, 'https'), $local, $content);
    }
    return $content;
}

function pkh_restorer_apply_payload(array $data) {
    $post_type = isset($data['post_type']) ? $data['post_type'] : 'listing';
    if (!in_array($post_type, pkh_restorer_post_types(), true)) {
        $post_type = post_type_exists('listing') ? 'listing' : 'post';
    }

    foreach ((array) ($data['form_fields'] ?? array()) as $field) {
        if (empty($field['title'])) {
            continue;
        }
        pkh_restorer_ensure_form_field(
            $field['title'],
            isset($field['slug']) ? $field['slug'] : '',
            isset($field['type']) ? $field['type'] : 'text'
        );
    }

    $existing = pkh_restorer_find_listing($data);
    $action = $existing ? 'updated' : 'created';
    $postarr = array(
        'post_title' => sanitize_text_field($data['post_title'] ?? $data['title'] ?? ''),
        'post_content' => wp_kses_post($data['post_content'] ?? $data['content'] ?? ''),
        'post_status' => sanitize_key($data['post_status'] ?? 'publish'),
        'post_name' => sanitize_title($data['post_name'] ?? $data['slug'] ?? ''),
        'post_type' => $post_type,
    );
    if (!empty($data['post_date'])) {
        $timestamp = strtotime((string) $data['post_date']);
        if ($timestamp !== false) {
            $postarr['post_date'] = wp_date('Y-m-d H:i:s', $timestamp);
            $postarr['post_date_gmt'] = gmdate('Y-m-d H:i:s', $timestamp);
        }
    }
    if ($existing) {
        $postarr['ID'] = $existing->ID;
        $post_id = wp_update_post($postarr, true);
    } else {
        $post_id = wp_insert_post($postarr, true);
    }
    if (is_wp_error($post_id)) {
        return $post_id;
    }

    $meta = (array) ($data['meta'] ?? array());
    foreach ($meta as $key => $value) {
        if (!is_string($key) || $key === '') {
            continue;
        }
        if (is_array($value) || is_object($value)) {
            update_post_meta($post_id, sanitize_key($key), $value);
        } else {
            update_post_meta($post_id, sanitize_key($key), is_scalar($value) ? $value : wp_json_encode($value));
        }
    }

    $options = (array) ($data['lp_listingpro_options'] ?? array());
    if ($options) {
        update_post_meta($post_id, 'lp_listingpro_options', $options);
    }

    $option_fields = (array) ($data['lp_listingpro_options_fields'] ?? array());
    if ($option_fields) {
        update_post_meta($post_id, 'lp_listingpro_options_fields', $option_fields);
    }

    $taxonomies = (array) ($data['taxonomies'] ?? array());
    if (empty($taxonomies['listing-category']) && !empty($data['category'])) {
        $taxonomies['listing-category'] = array($data['category']);
    }
    if (empty($taxonomies['location']) && !empty($data['location']['raw'])) {
        $taxonomies['location'] = array($data['location']['raw']);
    }
    foreach ($taxonomies as $taxonomy => $terms) {
        if (!taxonomy_exists($taxonomy)) {
            continue;
        }
        $names = array_values(array_filter(array_map('strval', (array) $terms)));
        wp_set_object_terms($post_id, $names, $taxonomy, false);
    }

    $media_ids = array();
    $gallery_ids = array();
    $featured_id = 0;
    $created_attachments = 0;
    $reused_attachments = 0;
    $url_map = array();
    $ids_by_url = array();
    $media = (array) ($data['media'] ?? array());
    $title = $postarr['post_title'];

    $apply_attachment = function ($item, $fallback_title) use ($post_id, &$created_attachments, &$reused_attachments, &$url_map, &$ids_by_url) {
        if (!is_array($item) || empty($item['local_path'])) {
            return 0;
        }
        $result = pkh_restorer_import_attachment(
            $item['local_path'],
            $item['filename'] ?? $fallback_title,
            $item['original_url'] ?? '',
            $post_id,
            $item['archive_url'] ?? ''
        );
        $aid = pkh_restorer_attachment_id($result);
        if (!$aid) {
            return 0;
        }
        if (!empty($result['created'])) {
            $created_attachments++;
        } else {
            $reused_attachments++;
        }
        $local = !empty($result['url']) ? $result['url'] : wp_get_attachment_url($aid);
        if (!empty($item['original_url']) && $local) {
            $url_map[$item['original_url']] = $local;
            $ids_by_url[$item['original_url']] = $aid;
        }
        return $aid;
    };

    if (!empty($media['featured']['local_path'])) {
        $featured_id = $apply_attachment($media['featured'], $title);
        if ($featured_id) {
            set_post_thumbnail($post_id, $featured_id);
            $media_ids[] = $featured_id;
        }
    }

    foreach ((array) ($media['gallery'] ?? array()) as $item) {
        $aid = $apply_attachment($item, $title);
        if ($aid) {
            $gallery_ids[] = $aid;
            $media_ids[] = $aid;
        }
    }

    foreach ((array) ($media['content'] ?? array()) as $item) {
        $aid = $apply_attachment($item, $title);
        if ($aid) {
            $media_ids[] = $aid;
        }
    }

    foreach ((array) ($media['videos'] ?? array()) as $item) {
        $aid = $apply_attachment($item, $title . ' video');
        if ($aid) {
            $media_ids[] = $aid;
        }
    }

    if (!empty($media['site_logo']['local_path'])) {
        $site_logo_id = $apply_attachment($media['site_logo'], 'Project Hindu Kush header logo');
        if ($site_logo_id) {
            $media_ids[] = $site_logo_id;
        }
    }

    if (!empty($media['logo']['local_path'])) {
        $logo_id = $apply_attachment($media['logo'], $title . ' logo');
        if ($logo_id) {
            $media_ids[] = $logo_id;
            $logo_url = wp_get_attachment_url($logo_id);
            if ($logo_url) {
                $options['business_logo'] = $logo_url;
            }
        }
    }

    if ($gallery_ids) {
        $unique = array();
        foreach ($gallery_ids as $gid) {
            if (!in_array($gid, $unique, true)) {
                $unique[] = $gid;
            }
        }
        update_post_meta($post_id, 'gallery_image_ids', implode(',', $unique));
        $gallery_ids = $unique;
    }

    if ($url_map) {
        $rewritten = pkh_restorer_rewrite_content_media($postarr['post_content'], $url_map);
        if ($rewritten !== $postarr['post_content']) {
            wp_update_post(array(
                'ID' => $post_id,
                'post_content' => $rewritten,
            ));
        }
    }

    if ($options) {
        update_post_meta($post_id, 'lp_listingpro_options', $options);
    }

    return array(
        'id' => (int) $post_id,
        'link' => get_permalink($post_id),
        'action' => $action,
        'updated' => $action === 'updated',
        'created' => $action === 'created',
        'featured_media' => $featured_id ?: null,
        'gallery_ids' => $gallery_ids,
        'media_ids' => array_values(array_unique($media_ids)),
        'attachments_created' => $created_attachments,
        'attachments_reused' => $reused_attachments,
        'media_map' => $url_map,
        'media_ids_by_url' => $ids_by_url,
    );
}
