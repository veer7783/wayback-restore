"""Idempotent WordPress importer for restored listings."""

from __future__ import annotations

from typing import Any

from extractor.listing_extractor import ExtractedListing
from wordpress.client import WordPressClient, WordPressError
from wordpress.listingpro_mapping import build_listingpro_payload
from wordpress.media_importer import attach_featured, upload_media
from wordpress.taxonomy import ensure_term, location_term_name


def build_payload(
    listing: ExtractedListing,
    snapshot_url: str,
    timestamp: str,
    schema: dict[str, Any],
    local_site_url: str,
    parsed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = build_listingpro_payload(
        listing,
        snapshot_url,
        timestamp,
        schema,
        local_site_url,
        parsed,
    )
    # Backward-compatible keys used by existing tests and REST callers.
    payload["title"] = payload["post_title"]
    payload["slug"] = payload["post_name"]
    payload["status"] = payload["post_status"]
    payload["content"] = payload["post_content"]
    return payload


def import_listing(
    client: WordPressClient,
    listing: ExtractedListing,
    snapshot_url: str,
    timestamp: str,
    schema: dict[str, Any],
    local_site_url: str,
    parsed: dict[str, Any] | None = None,
    media: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = build_payload(listing, snapshot_url, timestamp, schema, local_site_url, parsed)
    if media:
        payload["media"] = {
            "featured": media.get("featured"),
            "gallery": media.get("gallery") or [],
        }

    existing = client.find_posts(listing.source_url, payload["post_type"])
    try:
        created_post = client.post_json("/wp-json/pkh-restorer/v1/import", payload)
        post_id = int(created_post["id"])
        wp_url = created_post.get("link")
        created = bool(created_post.get("created"))
        if existing and not created:
            created = False
        action = created_post.get("action") or ("updated" if existing else "created")
        return {
            "post_id": post_id,
            "wp_url": wp_url,
            "created": created,
            "action": action,
            "featured_media": created_post.get("featured_media"),
            "gallery_ids": created_post.get("gallery_ids") or [],
            "media_ids": created_post.get("media_ids") or [],
            "payload": payload,
        }
    except WordPressError:
        pass

    if existing:
        post_id = int(existing[0]["id"])
        wp_url = existing[0].get("link")
        created = False
        action = "updated"
    else:
        created_post = client.post_json(
            "/wp-json/wp/v2/posts",
            {
                "title": payload["title"],
                "content": payload["content"],
                "status": "publish",
                "slug": payload["slug"],
                "meta": payload["meta"],
            },
        )
        post_id = int(created_post["id"])
        wp_url = created_post.get("link") or f"{local_site_url.rstrip('/')}/{listing.slug}/"
        created = True
        action = "created"

    if listing.category:
        taxonomy = "listing-category" if "listing-category" in (schema.get("taxonomies") or []) else "category"
        ensure_term(client, taxonomy, listing.category)
    location_name = location_term_name(listing.location.model_dump())
    if location_name:
        taxonomy = "location" if "location" in (schema.get("taxonomies") or []) else "category"
        ensure_term(client, taxonomy, location_name)

    featured_id = None
    media_ids: list[int] = []
    for image in listing.images:
        if not image.get("downloaded") or not image.get("local_path"):
            continue
        media_id = upload_media(
            client,
            image["local_path"],
            listing.title or listing.slug,
            listing.source_url,
        )
        if media_id:
            media_ids.append(media_id)
            if featured_id is None:
                featured_id = media_id
    if featured_id:
        attach_featured(client, post_id, featured_id)

    return {
        "post_id": post_id,
        "wp_url": wp_url,
        "created": created,
        "action": action,
        "featured_media": featured_id,
        "gallery_ids": [],
        "media_ids": media_ids,
        "payload": payload,
    }
