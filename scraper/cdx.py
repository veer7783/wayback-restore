"""Wayback CDX discovery for projecthindukush.com incident URLs."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from scraper.archive_client import ArchiveClient, ArchiveClientError
from scraper.eligibility import DEFAULT_SNAPSHOT_CUTOFF, is_eligible_timestamp
from scraper.urls import (
    build_wayback_url,
    host_variants,
    is_incident_url,
    normalize_url,
    rewrite_wayback_url,
    strip_www,
    url_hash,
)

LOGGER = logging.getLogger("pkh.cdx")

CDX_FIELDS = ("urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length")


class CDXRecord(BaseModel):
    urlkey: str = ""
    timestamp: str
    original: str
    mimetype: str = ""
    statuscode: str = ""
    digest: str = ""
    length: int = 0
    archive_url: str = ""
    discovered_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def status_code(self) -> int | None:
        if not self.statuscode:
            return None
        try:
            return int(self.statuscode)
        except ValueError:
            return None

    @property
    def normalized_url(self) -> str:
        return normalize_url(rewrite_wayback_url(self.original))


def parse_cdx_json(payload: Any) -> list[CDXRecord]:
    """Parse Wayback `output=json` rows. First row is the header."""
    if not isinstance(payload, list) or not payload:
        return []

    rows = payload
    header: list[str] | None = None
    if rows and isinstance(rows[0], list) and rows[0] and isinstance(rows[0][0], str):
        if rows[0][0] in {"urlkey", "timestamp", "original"}:
            header = [str(item) for item in rows[0]]
            rows = rows[1:]

    records: list[CDXRecord] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        mapping = _row_to_mapping(row, header)
        if not mapping.get("original") or not mapping.get("timestamp"):
            continue
        records.append(CDXRecord.model_validate(mapping))
    return records


def _row_to_mapping(row: list[Any], header: list[str] | None) -> dict[str, Any]:
    names = header or list(CDX_FIELDS)
    mapping: dict[str, Any] = {}
    for index, name in enumerate(names):
        if index >= len(row):
            break
        mapping[name] = row[index]
    try:
        mapping["length"] = int(mapping.get("length") or 0)
    except (TypeError, ValueError):
        mapping["length"] = 0
    original = str(mapping.get("original") or "")
    timestamp = str(mapping.get("timestamp") or "")
    if original and timestamp:
        mapping["archive_url"] = build_wayback_url(original, timestamp)
    return mapping


def dedupe_records(records: Iterable[CDXRecord]) -> list[CDXRecord]:
    """Keep one record per original URL + timestamp + digest."""
    seen: set[tuple[str, str, str]] = set()
    unique: list[CDXRecord] = []
    for record in records:
        key = (record.normalized_url, record.timestamp, record.digest)
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def unique_original_urls(records: Iterable[CDXRecord]) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for record in records:
        url = record.normalized_url
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def write_cdx_jsonl(records: Iterable[CDXRecord], path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(record.model_dump_json() + "\n")
            count += 1
    return count


def read_cdx_jsonl(path: Path) -> list[CDXRecord]:
    if not path.exists():
        return []
    records: list[CDXRecord] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            records.append(CDXRecord.model_validate(json.loads(line)))
    return records


def seed_known_record(url: str, timestamp: str) -> CDXRecord:
    """Documented first-test snapshot. Used when live CDX is unavailable."""
    normalized = normalize_url(url)
    return CDXRecord(
        timestamp=timestamp,
        original=normalized,
        mimetype="text/html",
        statuscode="200",
        archive_url=build_wayback_url(normalized, timestamp),
    )


def availability_record(client: ArchiveClient, endpoint: str, url: str) -> CDXRecord | None:
    """Fallback to the documented Wayback Availability API for a single URL."""
    response = client.request("GET", endpoint, params={"url": url}, retries=2)
    if response.status_code >= 400:
        return None
    payload = response.json()
    closest = (payload.get("archived_snapshots") or {}).get("closest") or {}
    if not closest.get("available"):
        return None
    timestamp = str(closest.get("timestamp") or "")
    original = str(closest.get("url") or url)
    status = str(closest.get("status") or "200")
    return CDXRecord(
        timestamp=timestamp,
        original=original,
        mimetype="text/html",
        statuscode=status,
        archive_url=build_wayback_url(rewrite_wayback_url(original), timestamp) if timestamp else "",
    )


class CDXClient:
    def __init__(self, client: ArchiveClient, endpoint: str) -> None:
        self.client = client
        self.endpoint = endpoint

    def search(
        self,
        url: str,
        *,
        match_type: str = "exact",
        limit: int | None = None,
        collapse: str | None = None,
        filters: list[str] | None = None,
        resume_key: str | None = None,
        from_ts: str | None = None,
        to_ts: str | None = None,
    ) -> tuple[list[CDXRecord], str | None]:
        params: dict[str, Any] = {
            "url": url,
            "output": "json",
            "showResumeKey": "true",
        }
        if match_type:
            params["matchType"] = match_type
        if limit is not None:
            params["limit"] = str(limit)
        if collapse:
            params["collapse"] = collapse
        if from_ts:
            params["from"] = from_ts
        if to_ts:
            params["to"] = to_ts
        if resume_key:
            params["resumeKey"] = resume_key

        query_params: list[tuple[str, str]] = [(key, str(value)) for key, value in params.items()]
        for item in filters or []:
            query_params.append(("filter", item))

        response = self.client.request("GET", self.endpoint, params=query_params, retries=2)
        if response.status_code >= 400:
            raise ArchiveClientError(f"CDX HTTP {response.status_code} for {url}")

        payload = response.json()
        next_key = None
        rows = payload
        if isinstance(payload, list) and payload:
            # showResumeKey appends [[], ["resumeKey"]] after the data rows.
            if (
                len(payload) >= 2
                and payload[-2] == []
                and isinstance(payload[-1], list)
                and payload[-1]
            ):
                next_key = str(payload[-1][0])
                rows = payload[:-2]
        records = parse_cdx_json(rows)
        LOGGER.info("CDX returned %s rows for %s", len(records), url)
        return records, next_key

    def iter_search(self, url: str, **kwargs: Any) -> list[CDXRecord]:
        collected: list[CDXRecord] = []
        resume = kwargs.pop("resume_key", None)
        while True:
            batch, resume = self.search(url, resume_key=resume, **kwargs)
            collected.extend(batch)
            if not resume:
                break
        return dedupe_records(collected)


def _snapshots_for_url(
    cdx_client: CDXClient,
    url: str,
    availability_endpoint: str,
    cutoff: str = DEFAULT_SNAPSHOT_CUTOFF,
) -> list[CDXRecord]:
    records: list[CDXRecord] = []
    for variant in host_variants(url):
        try:
            snapshots, _resume = cdx_client.search(
                variant,
                match_type="exact",
                filters=["mimetype:text/html"],
                limit=20,
                to_ts=cutoff,
            )
            records.extend(snapshots)
        except ArchiveClientError:
            LOGGER.warning("Exact CDX lookup failed for %s", variant)
        if records:
            break
    if not records:
        try:
            fallback = availability_record(cdx_client.client, availability_endpoint, url)
            if fallback and is_eligible_timestamp(fallback.timestamp, cutoff):
                records.append(fallback)
            elif fallback:
                LOGGER.info("Availability snapshot %s for %s is after cutoff; ignored", fallback.timestamp, url)
        except ArchiveClientError:
            LOGGER.warning("Availability API failed for %s; continuing with known/CDX data", url)
    records = [record for record in records if is_eligible_timestamp(record.timestamp, cutoff)]
    if not records:
        LOGGER.warning("No eligible snapshots for %s", url)
    return dedupe_records(records)


def discover_incident_records(
    cdx_client: CDXClient,
    domain: str,
    path_prefix: str,
    limit: int,
    page_size: int,
    known_test_url: str | None = None,
    known_test_timestamp: str | None = None,
    availability_endpoint: str = "https://archive.org/wayback/available",
    cutoff: str = DEFAULT_SNAPSHOT_CUTOFF,
) -> list[CDXRecord]:
    """Discover unique incident URLs, then collect their CDX rows.

    The known test URL is resolved first so a --limit 1 run does not depend
    on a slow prefix query against the full /incident/ tree.
    """
    discovered: list[CDXRecord] = []
    unique_urls: list[str] = []

    if known_test_url:
        unique_urls.append(strip_www(known_test_url))
        if known_test_timestamp and is_eligible_timestamp(known_test_timestamp, cutoff):
            discovered.append(seed_known_record(known_test_url, known_test_timestamp))
        if limit > 1 or not discovered:
            discovered.extend(
                _snapshots_for_url(cdx_client, unique_urls[0], availability_endpoint, cutoff)
            )
        if limit <= 1:
            return dedupe_records(discovered)

    prefix_url = f"{domain}{path_prefix}"
    try:
        # One CDX page is enough for a small unique-URL sample. Do not follow
        # resume keys through the full /incident/ tree.
        collapsed, resume = cdx_client.search(
            prefix_url,
            match_type="prefix",
            collapse="urlkey",
            limit=max(limit * 20, 200),
            filters=["statuscode:200", "mimetype:text/html"],
            to_ts=cutoff,
        )
        for record in collapsed:
            url = strip_www(record.normalized_url)
            if not is_incident_url(url, path_prefix):
                continue
            if url in unique_urls:
                continue
            unique_urls.append(url)
            if len(unique_urls) >= limit:
                break
        LOGGER.info("Prefix CDX discovered %s unique incident URLs", len(unique_urls))
    except ArchiveClientError:
        LOGGER.exception("Prefix CDX discovery failed; using already resolved URLs")

    unique_urls = unique_urls[:limit]
    for url in unique_urls:
        if any(strip_www(record.normalized_url) == url for record in discovered):
            continue
        discovered.extend(_snapshots_for_url(cdx_client, url, availability_endpoint, cutoff))

    return dedupe_records(discovered)


def discovery_summary(records: list[CDXRecord], path_prefix: str) -> dict[str, int]:
    unique = [url for url in unique_original_urls(records) if is_incident_url(url, path_prefix)]
    missing = sum(1 for url in unique if not any(r.normalized_url == url for r in records if r.timestamp))
    return {
        "discovered_urls": len(unique),
        "unique_incident_urls": len(unique),
        "snapshots_available": len(records),
        "missing_snapshots": missing,
    }


def record_identity(record: CDXRecord) -> str:
    return url_hash(f"{record.normalized_url}|{record.timestamp}|{record.digest}")
