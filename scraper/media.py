"""Resolve and download archived media while skipping trackers and ads."""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel

from scraper.archive_client import ArchiveClient, ArchiveClientError
from scraper.eligibility import DEFAULT_SNAPSHOT_CUTOFF, is_eligible_timestamp
from scraper.parser import looks_like_html
from scraper.urls import (
    build_wayback_url,
    normalize_url,
    rewrite_wayback_url,
    short_url_hash,
    slug_from_url,
    split_wayback_url,
)
from utils.hashing import sha256_bytes

LOGGER = logging.getLogger("pkh.media")

SKIP_SUBSTRINGS = (
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
    "pixel",
    "1x1.gif",
    "scorecardresearch",
    "content-loader.gif",
    "data:image",
    "/themes/listingpro/assets/",
    "hummingbird-assets",
    "favicon",
    "ph-new-logo",
    "logo-inner",
    "support-mohh",
)

PRIORITY_HINTS = (
    ("featured", 100),
    ("listing", 90),
    ("gallery", 80),
    ("wp-content/uploads", 70),
    ("attachment", 60),
)


class MediaAsset(BaseModel):
    original_url: str
    archive_url: str
    filename: str
    local_path: str | None = None
    mime_type: str | None = None
    sha256: str | None = None
    source_page: str
    priority: int = 0
    skipped_reason: str | None = None
    downloaded: bool = False
    http_status: int | None = None
    snapshot_timestamp: str | None = None
    eligibility: str = "eligible"


def should_skip(url: str, extra_substrings: list[str] | None = None) -> str | None:
    lowered = url.lower()
    for token in list(SKIP_SUBSTRINGS) + list(extra_substrings or []):
        if token.lower() in lowered:
            return token
    path = urlsplit(url).path.lower()
    if path.endswith((".js", ".css", ".map")):
        return "non-media-asset"
    return None


def media_priority(url: str, alt: str = "", css_class: str = "") -> int:
    haystack = " ".join([url, alt, css_class]).lower()
    score = 10
    for token, value in PRIORITY_HINTS:
        if token in haystack:
            score = max(score, value)
    return score


def archive_url_for(original_url: str, timestamp: str) -> str:
    return build_wayback_url(rewrite_wayback_url(original_url), timestamp, raw=True)


def filename_for(url: str) -> str:
    path = urlsplit(url).path
    name = path.rsplit("/", 1)[-1] or "asset"
    if "." not in name:
        name += ".bin"
    return f"{slug_from_url(url)}_{short_url_hash(url)}_{name}"


def collect_media(
    images: list[dict[str, str]],
    source_page: str,
    timestamp: str,
    skip_substrings: list[str] | None = None,
) -> list[MediaAsset]:
    assets: list[MediaAsset] = []
    seen: set[str] = set()
    for image in images:
        original = rewrite_wayback_url(image.get("src", ""))
        if not original:
            continue
        try:
            original = normalize_url(original)
        except ValueError:
            continue
        if original in seen:
            continue
        seen.add(original)
        skip = should_skip(original, skip_substrings)
        assets.append(
            MediaAsset(
                original_url=original,
                archive_url=archive_url_for(original, timestamp),
                filename=filename_for(original),
                source_page=source_page,
                priority=media_priority(original, image.get("alt", ""), image.get("class", "")),
                skipped_reason=skip,
            )
        )
    assets.sort(key=lambda item: item.priority, reverse=True)
    return assets


IMAGE_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
)


def validate_media_payload(
    payload: bytes,
    content_type: str = "",
    filename: str = "",
) -> tuple[bool, str]:
    """Return (ok, reason). Reject HTML error pages saved as images."""
    if not payload or len(payload) < 32:
        return False, "empty-or-tiny"
    if looks_like_html(content_type, payload):
        return False, "html-error-page"
    lowered_type = (content_type or "").lower()
    if lowered_type and "text/html" in lowered_type:
        return False, "html-content-type"
    magic_ok = False
    detected = ""
    for prefix, mime in IMAGE_MAGIC:
        if payload.startswith(prefix):
            if prefix == b"RIFF" and b"WEBP" not in payload[:16]:
                continue
            magic_ok = True
            detected = mime
            break
    if not magic_ok:
        if lowered_type.startswith("image/"):
            return False, "invalid-image-bytes"
        return False, "not-image"
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    _ = ext  # extension is advisory; magic bytes decide validity
    return True, "ok"


def capture_timestamp_from_response(response: object) -> str:
    url = str(getattr(response, "url", "") or "")
    timestamp, _original = split_wayback_url(url)
    if timestamp:
        return timestamp
    headers = getattr(response, "headers", None) or {}
    memento = headers.get("memento-datetime") or headers.get("Memento-Datetime") or ""
    if not memento:
        return ""
    try:
        from email.utils import parsedate_to_datetime

        parsed = parsedate_to_datetime(memento)
        return parsed.strftime("%Y%m%d%H%M%S")
    except (TypeError, ValueError, OverflowError):
        return ""


def classify_media_status(asset: MediaAsset, imported: bool | None = None, attachment_id: int | None = None) -> str:
    """Map download/import outcomes onto the batch-10 status vocabulary."""
    if imported is False and asset.downloaded:
        return "import_failed"
    if attachment_id:
        if asset.skipped_reason == "duplicate-sha256":
            return "already_exists"
        return "already_exists" if imported is False else "restored"
    reason = asset.skipped_reason or ""
    if reason in {"archive-miss", "missing_from_archive", "no_eligible_snapshot"}:
        return "missing_from_archive"
    if reason in {"invalid_archive_response", "html-error-page", "html-content-type", "invalid-image-bytes", "empty-or-tiny", "not-image"}:
        return "invalid_archive_response"
    if asset.downloaded:
        return "restored"
    if reason:
        return "download_failed"
    return "download_failed"


def lookup_eligible_media_capture(
    records: list,
    cutoff: str = DEFAULT_SNAPSHOT_CUTOFF,
) -> tuple[object | None, str]:
    """Pick the latest eligible CDX media capture. Never fall back past the cutoff."""
    eligible = [record for record in records if is_eligible_timestamp(getattr(record, "timestamp", ""), cutoff)]
    if not eligible:
        return None, "no_eligible_snapshot"
    images = [record for record in eligible if "image" in (getattr(record, "mimetype", "") or "").lower()]
    pool = images or eligible
    best = max(pool, key=lambda record: getattr(record, "timestamp", ""))
    return best, "eligible"


def download_media_asset(
    client: ArchiveClient,
    asset: MediaAsset,
    media_dir: Path,
    known_hashes: dict[str, str],
) -> MediaAsset:
    if asset.skipped_reason:
        return asset
    media_dir.mkdir(parents=True, exist_ok=True)
    destination = media_dir / asset.filename
    if destination.exists() and destination.stat().st_size > 0:
        existing = destination.read_bytes()
        ok, reason = validate_media_payload(existing, asset.mime_type or "", asset.filename)
        if ok:
            asset.local_path = str(destination)
            asset.downloaded = True
            asset.sha256 = sha256_bytes(existing)
            return asset
        destination.unlink(missing_ok=True)
    urls_to_try = [asset.archive_url]
    if "id_/" in asset.archive_url:
        urls_to_try.append(asset.archive_url.replace("id_/", "/"))

    response = None
    payload = b""
    last_error = None
    for candidate in urls_to_try:
        try:
            response, payload = client.get_bytes(candidate)
        except ArchiveClientError as exc:
            last_error = exc
            continue
        if response.status_code == 200 and payload:
            asset.archive_url = candidate
            asset.http_status = response.status_code
            captured = capture_timestamp_from_response(response)
            if captured:
                asset.snapshot_timestamp = captured
                if not is_eligible_timestamp(captured):
                    LOGGER.warning("Rejecting post-cutoff media capture %s for %s", captured, asset.original_url)
                    asset.skipped_reason = "no_eligible_snapshot"
                    asset.eligibility = "no_eligible_snapshot"
                    return asset
            break
        asset.http_status = response.status_code
        last_error = f"HTTP {response.status_code}"
        response = None
    if response is None or response.status_code != 200 or not payload:
        LOGGER.warning("Media miss %s (%s)", asset.original_url, last_error)
        asset.skipped_reason = "archive-miss"
        if asset.http_status in {404, 403, None} and not payload:
            asset.skipped_reason = "archive-miss"
        return asset

    content_type = response.headers.get("content-type", "")
    ok, reason = validate_media_payload(payload, content_type, asset.filename)
    if not ok:
        LOGGER.warning("Invalid archive media %s (%s)", asset.original_url, reason)
        asset.skipped_reason = "invalid_archive_response" if "html" in reason else reason
        asset.mime_type = content_type
        return asset

    digest = sha256_bytes(payload)
    if digest in known_hashes:
        asset.sha256 = digest
        asset.local_path = known_hashes[digest]
        asset.downloaded = True
        asset.skipped_reason = "duplicate-sha256"
        asset.mime_type = content_type or mimetypes.guess_type(asset.filename)[0]
        return asset

    destination.write_bytes(payload)
    asset.local_path = str(destination)
    asset.sha256 = digest
    asset.mime_type = content_type or mimetypes.guess_type(asset.filename)[0]
    asset.downloaded = True
    known_hashes[digest] = str(destination)
    return asset
