"""Download unmodified archived HTML with retries and resume support."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, Field

from scraper.archive_client import ArchiveClient, ArchiveClientError
from scraper.snapshots import SelectedSnapshot
from scraper.urls import html_basename

LOGGER = logging.getLogger("pkh.download")


class DownloadMeta(BaseModel):
    original_url: str
    archive_url: str
    raw_archive_url: str
    timestamp: str
    http_status: int
    content_type: str = ""
    downloaded_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    html_path: str
    bytes: int = 0
    resumed: bool = False


def meta_path_for(html_path: Path) -> Path:
    return html_path.with_suffix(".meta.json")


def is_complete(html_path: Path) -> bool:
    meta_path = meta_path_for(html_path)
    if not html_path.exists() or html_path.stat().st_size == 0:
        return False
    if not meta_path.exists():
        return False
    try:
        meta = DownloadMeta.model_validate_json(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return meta.http_status == 200 and meta.bytes > 0


def download_snapshot(
    client: ArchiveClient,
    snapshot: SelectedSnapshot,
    html_dir: Path,
) -> DownloadMeta:
    html_dir.mkdir(parents=True, exist_ok=True)
    html_path = html_dir / html_basename(snapshot.original_url)
    sidecar = meta_path_for(html_path)

    if is_complete(html_path):
        LOGGER.info("Resume skip %s", snapshot.original_url)
        existing = DownloadMeta.model_validate_json(sidecar.read_text(encoding="utf-8"))
        existing.resumed = True
        return existing

    try:
        response, body = client.get_bytes(snapshot.raw_archive_url)
        if response.status_code != 200 or not body:
            raise ArchiveClientError(f"Raw snapshot HTTP {response.status_code}")
    except ArchiveClientError:
        LOGGER.warning("Raw snapshot failed, retrying rewritten capture %s", snapshot.archive_url)
        response, body = client.get_bytes(snapshot.archive_url)

    html_path.write_bytes(body)
    meta = DownloadMeta(
        original_url=snapshot.original_url,
        archive_url=snapshot.archive_url,
        raw_archive_url=snapshot.raw_archive_url,
        timestamp=snapshot.timestamp,
        http_status=response.status_code,
        content_type=response.headers.get("content-type", ""),
        html_path=str(html_path),
        bytes=len(body),
    )
    sidecar.write_text(meta.model_dump_json(indent=2), encoding="utf-8")
    LOGGER.info("Saved %s (%s bytes)", html_path.name, meta.bytes)
    return meta


def load_html(html_dir: Path, original_url: str) -> tuple[str | None, DownloadMeta | None]:
    html_path = html_dir / html_basename(original_url)
    if not is_complete(html_path):
        return None, None
    meta = DownloadMeta.model_validate_json(meta_path_for(html_path).read_text(encoding="utf-8"))
    return html_path.read_text(encoding="utf-8", errors="replace"), meta
