"""Wayback recovery and validation for first-party video files."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel

from scraper.archive_client import ArchiveClient, ArchiveClientError
from scraper.eligibility import is_eligible_timestamp
from scraper.media import capture_timestamp_from_response
from scraper.urls import build_wayback_url, rewrite_wayback_url, short_url_hash
from utils.hashing import sha256_bytes

LOGGER = logging.getLogger("pkh.video")


class VideoAsset(BaseModel):
    source_page_url: str
    video_type: str
    original_video_url: str
    archive_url: str = ""
    snapshot_timestamp: str = ""
    status: str = "pending"
    local_path: str = ""
    local_url: str = ""
    wordpress_attachment_id: int | None = None
    media_hash: str = ""
    missing_reason: str = ""
    mime_type: str = ""
    http_status: int | None = None


def normalize_video_url(url: str) -> str:
    clean = rewrite_wayback_url(url).split("?", 1)[0]
    return clean


def validate_video_payload(payload: bytes, content_type: str = "", filename: str = "") -> tuple[bool, str]:
    if not payload or len(payload) < 64:
        return False, "empty-or-tiny"
    start = payload.lstrip()[:80].lower()
    if start.startswith((b"<!doctype html", b"<html")) or "html" in content_type.lower():
        return False, "html-error-page"
    lower = filename.lower()
    if lower.endswith(".mp4") and b"ftyp" not in payload[:64]:
        return False, "invalid-mp4"
    if lower.endswith(".webm") and not payload.startswith(b"\x1a\x45\xdf\xa3"):
        return False, "invalid-webm"
    reasonable = content_type.lower().startswith("video/") or lower.endswith(
        (".mp4", ".webm", ".ogv", ".mov")
    )
    return (True, "ok") if reasonable else (False, "unexpected-content-type")


def download_first_party_video(
    client: ArchiveClient,
    source_page_url: str,
    video: dict,
    page_timestamp: str,
    media_dir: Path,
) -> VideoAsset:
    original = normalize_video_url(str(video.get("original_video_url") or ""))
    asset = VideoAsset(
        source_page_url=source_page_url,
        video_type=str(video.get("video_type") or "video"),
        original_video_url=original,
        snapshot_timestamp=page_timestamp,
    )
    if not video.get("first_party"):
        asset.status = "embed_preserved"
        return asset
    if not original or not is_eligible_timestamp(page_timestamp):
        asset.status = "video_missing_from_archive"
        asset.missing_reason = "no_eligible_snapshot"
        return asset

    asset.archive_url = build_wayback_url(original, page_timestamp, raw=True)
    media_dir.mkdir(parents=True, exist_ok=True)
    name = Path(urlsplit(original).path).name or "video.mp4"
    destination = media_dir / f"{short_url_hash(original)}_{name}"
    if destination.exists() and destination.stat().st_size:
        payload = destination.read_bytes()
        ok, reason = validate_video_payload(payload, "", destination.name)
        if ok:
            asset.local_path = str(destination)
            asset.media_hash = sha256_bytes(payload)
            asset.status = "restored"
            return asset
        destination.unlink(missing_ok=True)

    response = None
    payload = b""
    for candidate in (asset.archive_url, asset.archive_url.replace("id_/", "/")):
        try:
            response, payload = client.get_bytes(candidate)
        except ArchiveClientError:
            continue
        asset.http_status = response.status_code
        if response.status_code == 200 and payload:
            captured = capture_timestamp_from_response(response)
            if captured and not is_eligible_timestamp(captured):
                asset.status = "video_missing_from_archive"
                asset.missing_reason = "post_cutoff_capture"
                return asset
            asset.archive_url = candidate
            asset.snapshot_timestamp = captured or page_timestamp
            break
    if response is None or response.status_code != 200 or not payload:
        asset.status = "video_missing_from_archive"
        asset.missing_reason = "archive_miss"
        return asset

    asset.mime_type = response.headers.get("content-type", "")
    ok, reason = validate_video_payload(payload, asset.mime_type, destination.name)
    if not ok:
        asset.status = "video_missing_from_archive"
        asset.missing_reason = reason
        return asset
    destination.write_bytes(payload)
    asset.local_path = str(destination)
    asset.media_hash = sha256_bytes(payload)
    asset.status = "restored"
    return asset
