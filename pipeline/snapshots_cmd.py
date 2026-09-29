"""Phase 2: snapshot selection."""

from __future__ import annotations

import logging

from pipeline.context import Runtime
from scraper.cdx import read_cdx_jsonl
from scraper.eligibility import DEFAULT_SNAPSHOT_CUTOFF
from scraper.snapshots import (
    ensure_known_snapshot,
    select_snapshots_for_records,
    write_selected_snapshots,
)
from scraper.urls import url_hash
from utils.config import resolve_path

LOGGER = logging.getLogger("pkh.snapshots")


def run_snapshots(runtime: Runtime, limit: int) -> int:
    config = runtime.config
    records = read_cdx_jsonl(resolve_path(config, "cdx_jsonl"))
    if not records:
        LOGGER.warning("No CDX records found; run discover first")
        records = []

    selected = select_snapshots_for_records(
        records,
        weights=config.get("scoring"),
        cutoff=str(config.get("wayback", {}).get("snapshot_cutoff") or DEFAULT_SNAPSHOT_CUTOFF),
    )
    selected = selected[:limit]
    selected = ensure_known_snapshot(
        selected,
        config["project"]["known_test_url"],
        config["project"]["known_test_timestamp"],
    )
    selected = selected[: max(limit, 1)]
    write_selected_snapshots(selected, resolve_path(config, "selected_snapshots"))

    for item in selected:
        row = runtime.store.upsert_url(item.original_url, url_hash(item.original_url))
        runtime.store.upsert_snapshot(
            row.id,
            {
                "timestamp": item.timestamp,
                "status_code": item.status_code,
                "mime_type": item.mime_type,
                "digest": item.digest,
                "length": item.length,
                "archive_url": item.archive_url,
                "score": item.score.total,
            },
            selected=True,
        )
        runtime.store.mark_selected(row.id, item.timestamp)

    LOGGER.info("Selected %s snapshots", len(selected))
    return len(selected)
