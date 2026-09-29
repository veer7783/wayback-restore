"""Map extracted archive listings onto live ListingPro 2.9.12 fields.

Field names come from the live XAMPP install and ListingPro plugin code:

- post type ``listing``
- taxonomies ``listing-category``, ``location``, ``features``, ``list-tags``
- map/address bag ``lp_listingpro_options`` keys ``gAddress``, ``latitude``, ``longitude``
- extra form values ``lp_listingpro_options_fields`` keyed by form-field ``post_name``
- gallery ``gallery_image_ids`` (comma-separated attachment IDs)
- featured image ``_thumbnail_id``
- listing body is ``post_content``
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from extractor.listing_extractor import ExtractedListing
from scraper.urls import rewrite_wayback_text, url_hash
from wordpress.listingpro import choose_post_type
from wordpress.taxonomy import location_term_name

WORDPRESS_RESIZE_RE = re.compile(r"-\d+x\d+(?=\.[a-zA-Z0-9]+$)")

# Archive extra-field labels → extractor field keys. Titles stay verbatim.
EXTRA_FIELD_SPECS: tuple[dict[str, str], ...] = (
    {"extractor_key": "date", "archive_title": "DATE"},
    {"extractor_key": "murdered", "archive_title": "How many were Murdered?"},
    {"extractor_key": "perpetrators", "archive_title": "Perpetrators"},
    {"extractor_key": "were_you_there", "archive_title": "WERE YOU THERE?"},
    {
        "extractor_key": "source",
        "archive_title": (
            "Where did you come to know about this event from ? "
            "(Please provide source link)"
        ),
    },
)

SKIP_LISTING_MEDIA = (
    "google-analytics.com",
    "googletagmanager.com",
    "googleadservices.com",
    "doubleclick.net",
    "googlesyndication.com",
    "maps.googleapis.com",
    "maps.gstatic.com",
    "facebook.net",
    "facebook.com/tr",
    "hotjar.com",
    "wp-hummingbird",
    "hummingbird-cache",
    "pagead",
    "adsense",
    "/ads/",
    "analytics.js",
    "gtm.js",
    "content-loader.gif",
    "/themes/listingpro/assets/",
    "ph-new-logo",
    "logo-inner",
    "support-mohh",
    "favicon",
)


def wordpress_slug(title: str) -> str:
    """Approximate WordPress ``sanitize_title`` for preview/mapping."""
    text = (title or "").strip().lower()
    text = re.sub(r"[^a-z0-9\s-]+", "", text)
    text = re.sub(r"[\s-]+", "-", text).strip("-")
    return text or "field"


def content_to_html(text: str) -> str:
    cleaned = rewrite_wayback_text(text or "").strip()
    if not cleaned:
        return ""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", cleaned) if part.strip()]
    html_parts = []
    for paragraph in paragraphs:
        escaped = html.escape(paragraph)
        escaped = escaped.replace("\n", "<br />\n")
        html_parts.append(f"<p>{escaped}</p>")
    return "\n".join(html_parts)


def archived_content_html(
    listing: ExtractedListing,
    parsed: dict[str, Any] | None,
    local_site_url: str,
) -> str:
    raw = str((parsed or {}).get("content_html") or "")
    if not raw:
        return content_to_html(listing.content)
    cleaned = rewrite_wayback_text(raw)
    for source in (
        "https://www.projecthindukush.com",
        "http://www.projecthindukush.com",
        "https://projecthindukush.com",
        "http://projecthindukush.com",
    ):
        cleaned = cleaned.replace(source, local_site_url.rstrip("/"))
    return cleaned


def extra_form_fields(listing: ExtractedListing) -> list[dict[str, str]]:
    """Build ListingPro form-fields from archive labels. Never invent values."""
    labeled = listing.labeled_fields_raw or {}
    fields: list[dict[str, str]] = []
    for spec in EXTRA_FIELD_SPECS:
        title = spec["archive_title"]
        value = labeled.get(title) or listing.fields.get(spec["extractor_key"])
        if not value:
            continue
        fields.append(
            {
                "extractor_key": spec["extractor_key"],
                "title": title,
                "slug": wordpress_slug(title),
                "type": "text",
                "value": str(value),
            }
        )
    return fields


def listingpro_options(listing: ExtractedListing) -> dict[str, str]:
    address = location_term_name(listing.location.model_dump()) or ""
    options: dict[str, str] = {}
    if address:
        options["gAddress"] = address
    if listing.location.lat:
        options["latitude"] = str(listing.location.lat)
    if listing.location.lng:
        options["longitude"] = str(listing.location.lng)
    return options


def listingpro_options_fields(listing: ExtractedListing) -> dict[str, str]:
    return {item["slug"]: item["value"] for item in extra_form_fields(listing)}


def is_skipped_media_url(url: str) -> bool:
    lowered = url.lower()
    return any(token in lowered for token in SKIP_LISTING_MEDIA)


def prefer_original_upload(url: str) -> str:
    """Prefer full-size uploads over WordPress -550x420 (etc.) derivatives."""
    path = urlsplit(url).path
    if WORDPRESS_RESIZE_RE.search(path):
        return WORDPRESS_RESIZE_RE.sub("", url)
    return url


def first_party_upload_urls(listing: ExtractedListing, parsed: dict[str, Any] | None = None) -> list[str]:
    candidates: list[str] = []
    if parsed:
        for image in parsed.get("images") or []:
            src = image.get("src") if isinstance(image, dict) else None
            if src:
                candidates.append(src)
        metadata = parsed.get("metadata") or {}
        for key in ("og:image", "twitter:image"):
            if metadata.get(key):
                candidates.append(str(metadata[key]))
        links = parsed.get("links") or {}
        if isinstance(links, dict):
            for group in links.values():
                candidates.extend(group or [])
        elif isinstance(links, list):
            for item in links:
                if isinstance(item, dict) and item.get("url"):
                    candidates.append(item["url"])
                elif isinstance(item, str):
                    candidates.append(item)
    for image in listing.images:
        if image.get("original_url"):
            candidates.append(str(image["original_url"]))
    for link in listing.links:
        if isinstance(link, dict) and link.get("url"):
            candidates.append(str(link["url"]))

    seen: set[str] = set()
    selected: list[str] = []
    for raw in candidates:
        url = prefer_original_upload(rewrite_wayback_text(raw.split("?", 1)[0]))
        lowered = url.lower()
        if "/wp-content/uploads/" not in lowered:
            continue
        if is_skipped_media_url(url):
            continue
        if not re.search(r"\.(jpe?g|png|gif|webp)$", urlsplit(url).path, re.I):
            continue
        if url in seen:
            continue
        seen.add(url)
        selected.append(url)
    return selected


def classify_media_urls(listing: ExtractedListing, parsed: dict[str, Any] | None = None) -> dict[str, Any]:
    urls = first_party_upload_urls(listing, parsed)
    featured = None
    content_urls: list[str] = []
    logo_url = None
    if parsed:
        featured = (parsed.get("metadata") or {}).get("og:image")
        if featured:
            featured = prefer_original_upload(rewrite_wayback_text(str(featured)))
            if featured not in urls or is_skipped_media_url(featured):
                featured = None
        for image in parsed.get("images") or []:
            if not isinstance(image, dict):
                continue
            src = prefer_original_upload(rewrite_wayback_text(str(image.get("src") or "").split("?", 1)[0]))
            role = str(image.get("role") or "")
            css = f"{image.get('class') or ''} {image.get('context') or ''}".lower()
            if "/wp-content/uploads/" not in src.lower() or src not in urls:
                continue
            if "logo" in css and "business" in css:
                logo_url = src
            if role == "content" or any(token in css for token in ("post-detail-content", "entry-content", "lp-listing-description")):
                if src != featured and src not in content_urls:
                    content_urls.append(src)
    gallery = [url for url in urls if url != featured and url != logo_url and url not in content_urls]
    return {
        "featured_image_url": featured,
        "gallery_urls": gallery,
        "content_urls": content_urls,
        "all_urls": ([featured] if featured else []) + gallery + [url for url in content_urls if url != featured],
        "logo_url": logo_url,
        "logo_reason": (
            "listing business logo"
            if logo_url
            else "no listing business logo in archive; site header logo skipped"
        ),
    }


def build_listingpro_payload(
    listing: ExtractedListing,
    snapshot_url: str,
    timestamp: str,
    schema: dict[str, Any],
    local_site_url: str,
    parsed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    post_type = choose_post_type(schema, "listing", "post")
    content_html = archived_content_html(listing, parsed, local_site_url)
    location_name = location_term_name(listing.location.model_dump())
    form_fields = extra_form_fields(listing)
    media = classify_media_urls(listing, parsed)
    source_id = url_hash(listing.source_url)
    imported_at = datetime.now(timezone.utc).isoformat()
    options = listingpro_options(listing)
    options_fields = listingpro_options_fields(listing)
    publication = (parsed or {}).get("publication_date") or {}
    videos = list((parsed or {}).get("videos") or [])
    primary_video = next(
        (
            str(item.get("original_video_url") or "")
            for item in videos
            if item.get("original_video_url")
        ),
        "",
    )
    if primary_video:
        options["video"] = primary_video
    return {
        "post_type": post_type,
        "post_title": listing.title or listing.slug,
        "post_name": listing.slug or "listing",
        "post_status": "publish",
        "post_content": content_html,
        "post_date": publication.get("value"),
        "publication_date_source": publication.get("source") or "fallback",
        "taxonomies": {
            "listing-category": [listing.category] if listing.category else [],
            "location": [location_name] if location_name else [],
            "features": [],
            "list-tags": [],
        },
        "lp_listingpro_options": options,
        "lp_listingpro_options_fields": options_fields,
        "form_fields": form_fields,
        "gallery_image_ids": "",
        "featured_image": media["featured_image_url"],
        "gallery": media["gallery_urls"],
        "business_logo": None,
        "videos": videos,
        "location": listing.location.model_dump(),
        "coordinates": {
            "latitude": listing.location.lat,
            "longitude": listing.location.lng,
        },
        "archive_metadata": {
            "_archive_source_url": listing.source_url,
            "_archive_snapshot_url": snapshot_url,
            "_archive_timestamp": timestamp,
            "_archive_imported_at": imported_at,
            "_pkh_source_id": source_id,
            "_archive_publication_date_source": publication.get("source") or "fallback",
            "_archive_video_urls": [
                item.get("original_video_url")
                for item in videos
                if item.get("original_video_url")
            ],
            **({"video": primary_video} if primary_video else {}),
        },
        "meta": {
            "_archive_source_url": listing.source_url,
            "_archive_snapshot_url": snapshot_url,
            "_archive_timestamp": timestamp,
            "_archive_imported_at": imported_at,
            "_pkh_source_id": source_id,
            **{f"_pkh_{key}": value for key, value in listing.fields.items() if value},
        },
        "fields": listing.fields,
        "category": listing.category,
        "local_site_url": local_site_url.rstrip("/"),
        "collected_by": listing.fields.get("collected_by"),
        "collected_by_status": (listing.field_status or {}).get("collected_by") or "missing_from_archive",
        "media_plan": media,
    }


def source_to_listingpro_mapping(listing: ExtractedListing, parsed: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = build_listingpro_payload(
        listing,
        "",
        "",
        {"post_types": ["listing"], "taxonomies": ["listing-category", "location", "features", "list-tags"]},
        "http://localhost/projecthindukush",
        parsed,
    )
    media = payload["media_plan"]
    rows = [
        {
            "source": "title",
            "archive_value": listing.title,
            "listingpro_target": "post_title",
            "status": "mapped" if listing.title else "missing_from_archive",
        },
        {
            "source": "slug",
            "archive_value": listing.slug,
            "listingpro_target": "post_name",
            "status": "mapped" if listing.slug else "missing_from_archive",
        },
        {
            "source": "article",
            "archive_value": (listing.content or "")[:120],
            "listingpro_target": "post_content",
            "status": "mapped" if listing.content else "missing_from_archive",
        },
        {
            "source": "category",
            "archive_value": listing.category,
            "listingpro_target": "taxonomy:listing-category",
            "status": "mapped" if listing.category else "missing_from_archive",
        },
        {
            "source": "location",
            "archive_value": listing.location.raw,
            "listingpro_target": "taxonomy:location + lp_listingpro_options.gAddress",
            "status": "mapped" if listing.location.raw else "missing_from_archive",
        },
        {
            "source": "latitude",
            "archive_value": listing.location.lat,
            "listingpro_target": "lp_listingpro_options.latitude",
            "status": "mapped" if listing.location.lat else "missing_from_archive",
        },
        {
            "source": "longitude",
            "archive_value": listing.location.lng,
            "listingpro_target": "lp_listingpro_options.longitude",
            "status": "mapped" if listing.location.lng else "missing_from_archive",
        },
        {
            "source": "collected_by",
            "archive_value": listing.fields.get("collected_by"),
            "listingpro_target": "lp_listingpro_options_fields (written only when present)",
            "status": (listing.field_status or {}).get("collected_by")
            or ("present" if listing.fields.get("collected_by") else "missing_from_archive"),
        },
        {
            "source": "featured_image",
            "archive_value": media["featured_image_url"],
            "listingpro_target": "_thumbnail_id",
            "status": "mapped" if media["featured_image_url"] else "missing_from_archive",
        },
        {
            "source": "gallery",
            "archive_value": media["gallery_urls"],
            "listingpro_target": "gallery_image_ids",
            "status": "mapped" if media["gallery_urls"] else "missing_from_archive",
        },
        {
            "source": "business_logo",
            "archive_value": None,
            "listingpro_target": "lp_listingpro_options.business_logo",
            "status": "missing_from_archive",
        },
    ]
    for item in extra_form_fields(listing):
        rows.append(
            {
                "source": item["extractor_key"],
                "archive_value": item["value"],
                "listingpro_target": f"lp_listingpro_options_fields[{item['slug']}] / form-fields:{item['title']}",
                "status": "mapped",
            }
        )
    return {
        "source_url": listing.source_url,
        "slug": listing.slug,
        "post_type": "listing",
        "live_schema": {
            "post_type": "listing",
            "taxonomies": ["listing-category", "location", "features", "list-tags"],
            "options_meta": "lp_listingpro_options",
            "extra_fields_meta": "lp_listingpro_options_fields",
            "gallery_meta": "gallery_image_ids",
            "featured_meta": "_thumbnail_id",
        },
        "mappings": rows,
        "payload_keys": {
            "lp_listingpro_options": payload["lp_listingpro_options"],
            "lp_listingpro_options_fields": payload["lp_listingpro_options_fields"],
            "taxonomies": payload["taxonomies"],
        },
        "notes": [
            "COLLECTED BY is missing_from_archive and is not invented.",
            "Location is not a labeled extra field on this snapshot; it is stored as taxonomy + gAddress.",
            "features and list-tags are empty because the archive page has none.",
            "Site header logo and MOHH sidebar image are not listing media.",
        ],
    }
