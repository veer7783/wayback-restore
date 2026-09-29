"""Download first-party listing media for a restored page."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from extractor.listing_extractor import ExtractedListing
from scraper.archive_client import ArchiveClient, ArchiveClientError
from scraper.cdx import CDXClient
from scraper.eligibility import DEFAULT_SNAPSHOT_CUTOFF, is_eligible_timestamp
from scraper.media import (
    MediaAsset,
    archive_url_for,
    classify_media_status,
    download_media_asset,
    filename_for,
    lookup_eligible_media_capture,
)
from scraper.urls import rewrite_wayback_text
from wordpress.listingpro_mapping import classify_media_urls

LOGGER = logging.getLogger("pkh.restore_media")


def _fallback_snapshot_derivative(original_url: str, parsed: dict[str, Any], timestamp: str) -> MediaAsset | None:
    """If the full-size upload 404s, use a resized copy that was actually in the snapshot HTML."""
    stem = original_url.rsplit("/", 1)[-1]
    stem_base = stem.rsplit(".", 1)[0]
    for image in parsed.get("images") or []:
        src = rewrite_wayback_text(str(image.get("src") or ""))
        if stem_base and stem_base in src and src != original_url:
            return MediaAsset(
                original_url=src,
                archive_url=archive_url_for(src, timestamp),
                filename=filename_for(src),
                source_page=str(parsed.get("source_url") or ""),
                priority=70,
                snapshot_timestamp=timestamp,
            )
    return None


def _role_for_url(url: str, plan: dict[str, Any]) -> str:
    if url == plan.get("featured_image_url"):
        return "featured"
    if url == plan.get("logo_url"):
        return "logo"
    if url in (plan.get("content_urls") or []):
        return "content"
    if url in (plan.get("gallery_urls") or []):
        return "gallery"
    return "content"


def resolve_media_archive_url(
    original_url: str,
    page_timestamp: str,
    cdx: CDXClient | None = None,
    cutoff: str = DEFAULT_SNAPSHOT_CUTOFF,
) -> tuple[str, str, str]:
    """Return (archive_url, timestamp, eligibility). Never use a March 2026+ capture."""
    if is_eligible_timestamp(page_timestamp, cutoff):
        return archive_url_for(original_url, page_timestamp), page_timestamp, "eligible"
    if cdx is not None:
        try:
            records, _resume = cdx.search(
                original_url,
                match_type="exact",
                to_ts=cutoff,
                limit=20,
                filters=["statuscode:200"],
            )
            best, status = lookup_eligible_media_capture(records, cutoff)
            if best is not None:
                stamp = str(getattr(best, "timestamp", "") or "")
                return archive_url_for(original_url, stamp), stamp, status
            if records:
                return "", "", "no_eligible_snapshot"
        except ArchiveClientError as exc:
            LOGGER.warning("Media CDX lookup failed for %s: %s", original_url, exc)
    return "", "", "no_eligible_snapshot"


def _pack_local(asset: MediaAsset) -> dict[str, Any]:
    return {
        "local_path": asset.local_path,
        "original_url": asset.original_url,
        "filename": asset.filename,
        "archive_url": asset.archive_url,
        "sha256": asset.sha256,
        "mime_type": asset.mime_type,
        "http_status": asset.http_status,
        "snapshot_timestamp": asset.snapshot_timestamp,
    }


def download_listing_media(
    client: ArchiveClient,
    listing: ExtractedListing,
    parsed: dict[str, Any] | None,
    timestamp: str,
    media_dir: Path,
    cdx: CDXClient | None = None,
    cutoff: str = DEFAULT_SNAPSHOT_CUTOFF,
    known_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    plan = classify_media_urls(listing, parsed)
    known_hashes = known_hashes if known_hashes is not None else {}
    downloaded: dict[str, MediaAsset] = {}
    misses: list[dict[str, str]] = []
    rows: list[dict[str, Any]] = []

    for url in plan["all_urls"]:
        role = _role_for_url(url, plan)
        archive_url, media_ts, eligibility = resolve_media_archive_url(url, timestamp, cdx, cutoff)
        asset = MediaAsset(
            original_url=url,
            archive_url=archive_url,
            filename=filename_for(url),
            source_page=listing.source_url,
            priority=100 if role == "featured" else 80,
            snapshot_timestamp=media_ts or timestamp,
            eligibility=eligibility,
        )
        if eligibility != "eligible" or not asset.archive_url:
            asset.skipped_reason = "no_eligible_snapshot"
            asset.eligibility = "no_eligible_snapshot"
        else:
            asset = download_media_asset(client, asset, media_dir, known_hashes)
            if (not asset.downloaded or not asset.local_path) and parsed and asset.skipped_reason == "archive-miss":
                fallback = _fallback_snapshot_derivative(url, parsed, media_ts or timestamp)
                if fallback:
                    fallback.eligibility = eligibility
                    fallback.snapshot_timestamp = media_ts or timestamp
                    asset = download_media_asset(client, fallback, media_dir, known_hashes)
        status = classify_media_status(asset)
        row = {
            "source_page_url": listing.source_url,
            "original_media_url": asset.original_url,
            "archive_media_url": asset.archive_url,
            "media_type": asset.mime_type or "",
            "role": role,
            "snapshot_timestamp": asset.snapshot_timestamp or media_ts or timestamp,
            "eligibility": asset.eligibility,
            "http_status": asset.http_status,
            "download_status": "ok" if asset.downloaded else (asset.skipped_reason or "failed"),
            "validation_status": "ok" if asset.downloaded else (asset.skipped_reason or "failed"),
            "wordpress_attachment_id": None,
            "local_url": "",
            "media_hash": asset.sha256,
            "local_path": asset.local_path,
            "final_status": status,
        }
        rows.append(row)
        if asset.downloaded and asset.local_path:
            downloaded[url] = asset
            downloaded[asset.original_url] = asset
        else:
            misses.append({"url": url, "reason": asset.skipped_reason or "download-failed", "role": role})

    featured = None
    if plan["featured_image_url"] and plan["featured_image_url"] in downloaded:
        featured = _pack_local(downloaded[plan["featured_image_url"]])

    gallery = []
    for url in plan["gallery_urls"]:
        asset = downloaded.get(url)
        if not asset:
            continue
        gallery.append(_pack_local(asset))

    content = []
    for url in plan.get("content_urls") or []:
        asset = downloaded.get(url)
        if not asset:
            continue
        content.append(_pack_local(asset))

    logo = None
    if plan.get("logo_url") and plan["logo_url"] in downloaded:
        logo = _pack_local(downloaded[plan["logo_url"]])

    return {
        "featured": featured,
        "gallery": gallery,
        "content": content,
        "logo": logo,
        "misses": misses,
        "plan": plan,
        "rows": rows,
        "downloaded": downloaded,
    }
