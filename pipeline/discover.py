"""Phase 1: CDX discovery."""

from __future__ import annotations

import logging

from pipeline.context import Runtime
from scraper.cdx import CDXClient, discover_incident_records, discovery_summary, write_cdx_jsonl
from scraper.eligibility import DEFAULT_SNAPSHOT_CUTOFF
from scraper.urls import url_hash
from utils.config import resolve_path

LOGGER = logging.getLogger("pkh.discover")


def run_discover(runtime: Runtime, limit: int) -> dict[str, int]:
    config = runtime.config
    wayback = config["wayback"]
    project = config["project"]
    cdx = CDXClient(runtime.client, wayback["cdx_endpoint"])
    records = discover_incident_records(
        cdx,
        domain=wayback["domain"],
        path_prefix=wayback["path_prefix"],
        limit=limit,
        page_size=int(wayback.get("page_size") or 100),
        known_test_url=project.get("known_test_url"),
        known_test_timestamp=project.get("known_test_timestamp"),
        availability_endpoint=wayback.get("availability_endpoint")
        or "https://archive.org/wayback/available",
        cutoff=str(wayback.get("snapshot_cutoff") or DEFAULT_SNAPSHOT_CUTOFF),
    )
    export_path = resolve_path(config, "cdx_jsonl")
    write_cdx_jsonl(records, export_path)

    for record in records:
        row = runtime.store.upsert_url(record.normalized_url, url_hash(record.normalized_url), record.urlkey)
        runtime.store.upsert_snapshot(
            row.id,
            {
                "timestamp": record.timestamp,
                "status_code": record.status_code,
                "mime_type": record.mimetype,
                "digest": record.digest,
                "length": record.length,
                "archive_url": record.archive_url,
            },
        )

    summary = discovery_summary(records, wayback["path_prefix"])
    LOGGER.info(
        "Discovered URLs: %s | Unique incident URLs: %s | Snapshots available: %s | Missing snapshots: %s",
        summary["discovered_urls"],
        summary["unique_incident_urls"],
        summary["snapshots_available"],
        summary["missing_snapshots"],
    )
    return summary
