"""Select the best Wayback snapshot per URL using a documented scoring model.

Latest is not automatically best. Metadata is scored first; HTML signals are
added when a snapshot body has already been downloaded.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from scraper.cdx import CDXRecord
from scraper.eligibility import DEFAULT_SNAPSHOT_CUTOFF, is_eligible_timestamp, normalize_timestamp
from scraper.urls import build_wayback_url, normalize_url, strip_www

LOGGER = logging.getLogger("pkh.snapshots")

LISTINGPRO_MARKERS = (
    "listingpro",
    "lp-listing",
    "listing-second-view",
    "lp-listing-title",
    "listing-details",
    "lp_listingpro",
    "listing-cat",
    "listing-location",
    "additional-details",
    "features-listing",
    "extra-fields",
    "post-detail-content",
    "single_listing",
    "class=\"listing",
    "class='listing",
)

CONTENT_MARKERS = (
    "entry-content",
    "post-content",
    "listing-content",
    "lp-listing-description",
    "article-content",
    "itemprop=\"description\"",
)

FIELD_MARKERS = (
    "how many were murdered",
    "perpetrators",
    "were you there",
    "collected by",
    "where did you come to know",
    "additional-details",
    "list-style-none",
)


class SnapshotScore(BaseModel):
    total: float
    status_200: float = 0
    mime_html: float = 0
    listingpro_markers: float = 0
    main_content: float = 0
    images: float = 0
    expected_fields: float = 0
    length_bonus: float = 0
    recency_bonus: float = 0
    html_scored: bool = False


class SelectedSnapshot(BaseModel):
    original_url: str
    timestamp: str
    status_code: int | None
    mime_type: str
    digest: str
    length: int
    archive_url: str
    raw_archive_url: str
    score: SnapshotScore
    selected_reason: str
    selected_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    candidates: int = 1
    eligibility_status: str = "eligible"
    rejected_after_cutoff: int = 0
    eligible_candidates: int = 1


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(needle in lowered for needle in needles)


def score_snapshot(
    record: CDXRecord,
    html: str | None = None,
    weights: dict[str, Any] | None = None,
    latest_timestamp: str | None = None,
) -> SnapshotScore:
    weights = weights or {}
    status_pts = float(weights.get("status_200", 50))
    mime_pts = float(weights.get("mime_html", 30))
    listing_pts = float(weights.get("listingpro_markers", 20))
    content_pts = float(weights.get("main_content", 20))
    image_pts = float(weights.get("images", 10))
    field_pts = float(weights.get("expected_fields", 10))
    length_max = float(weights.get("length_bonus_max", 8))
    recency_max = float(weights.get("recency_bonus_max", 5))

    score = SnapshotScore(total=0)
    if record.status_code == 200:
        score.status_200 = status_pts
    if "html" in (record.mimetype or "").lower():
        score.mime_html = mime_pts

    if record.length:
        score.length_bonus = min(length_max, record.length / 8000.0)

    if latest_timestamp and record.timestamp:
        try:
            latest = int(latest_timestamp)
            current = int(record.timestamp)
            if latest > 0:
                closeness = max(0.0, 1.0 - ((latest - current) / max(latest, 1)))
                score.recency_bonus = recency_max * closeness
        except ValueError:
            score.recency_bonus = 0

    if html:
        score.html_scored = True
        if _contains_any(html, LISTINGPRO_MARKERS):
            score.listingpro_markers = listing_pts
        if _contains_any(html, CONTENT_MARKERS) or len(html) > 4000:
            score.main_content = content_pts
        if "<img" in html.lower() or "wp-content/uploads" in html.lower():
            score.images = image_pts
        if _contains_any(html, FIELD_MARKERS):
            score.expected_fields = field_pts
    else:
        # Metadata-only: treat larger HTML captures as more likely to contain
        # listing structure, without assuming newest is best.
        if record.length >= 20000:
            score.main_content = content_pts * 0.4

    score.total = (
        score.status_200
        + score.mime_html
        + score.listingpro_markers
        + score.main_content
        + score.images
        + score.expected_fields
        + score.length_bonus
        + score.recency_bonus
    )
    return score


def select_best_snapshot(
    records: list[CDXRecord],
    html_by_timestamp: dict[str, str] | None = None,
    weights: dict[str, Any] | None = None,
    cutoff: str = DEFAULT_SNAPSHOT_CUTOFF,
) -> SelectedSnapshot | None:
    if not records:
        return None

    rejected = [record for record in records if not is_eligible_timestamp(record.timestamp, cutoff)]
    eligible = [record for record in records if is_eligible_timestamp(record.timestamp, cutoff)]
    if not eligible:
        sample = records[0]
        return SelectedSnapshot(
            original_url=strip_www(sample.normalized_url),
            timestamp="",
            status_code=None,
            mime_type="",
            digest="",
            length=0,
            archive_url="",
            raw_archive_url="",
            score=SnapshotScore(total=0),
            selected_reason="no_eligible_snapshot",
            candidates=len(records),
            eligibility_status="no_eligible_snapshot",
            rejected_after_cutoff=len(rejected),
            eligible_candidates=0,
        )

    latest = max(
        (normalize_timestamp(record.timestamp) for record in eligible if record.timestamp),
        default=None,
    )
    scored: list[tuple[CDXRecord, SnapshotScore]] = []
    for record in eligible:
        html = None
        if html_by_timestamp:
            html = html_by_timestamp.get(record.timestamp)
        scored.append((record, score_snapshot(record, html=html, weights=weights, latest_timestamp=latest)))

    scored.sort(key=lambda item: (item[1].total, item[0].length, item[0].timestamp), reverse=True)
    best_record, best_score = scored[0]
    reasons = []
    if best_record.status_code == 200:
        reasons.append("http-200")
    if "html" in best_record.mimetype.lower():
        reasons.append("text-html")
    if best_score.html_scored:
        reasons.append("html-signals")
    else:
        reasons.append("cdx-metadata")
    if latest and normalize_timestamp(best_record.timestamp) == latest:
        reasons.append("latest-eligible")
    elif latest:
        reasons.append("not-latest-by-choice")
    if rejected:
        reasons.append("cutoff-applied")

    return SelectedSnapshot(
            original_url=strip_www(best_record.normalized_url),
        timestamp=best_record.timestamp,
        status_code=best_record.status_code,
        mime_type=best_record.mimetype,
        digest=best_record.digest,
        length=best_record.length,
        archive_url=best_record.archive_url or build_wayback_url(best_record.original, best_record.timestamp),
        raw_archive_url=build_wayback_url(best_record.original, best_record.timestamp, raw=True),
        score=best_score,
        selected_reason=",".join(reasons),
        candidates=len(records),
        eligibility_status="eligible",
        rejected_after_cutoff=len(rejected),
        eligible_candidates=len(eligible),
    )


def select_snapshots_for_records(
    records: list[CDXRecord],
    weights: dict[str, Any] | None = None,
    html_lookup: dict[tuple[str, str], str] | None = None,
    cutoff: str = DEFAULT_SNAPSHOT_CUTOFF,
) -> list[SelectedSnapshot]:
    grouped: dict[str, list[CDXRecord]] = defaultdict(list)
    for record in records:
        grouped[strip_www(record.normalized_url)].append(record)

    selected: list[SelectedSnapshot] = []
    for url, group in grouped.items():
        html_by_ts = None
        if html_lookup:
            html_by_ts = {
                timestamp: html
                for (page_url, timestamp), html in html_lookup.items()
                if page_url == url
            }
        choice = select_best_snapshot(group, html_by_timestamp=html_by_ts, weights=weights, cutoff=cutoff)
        if choice:
            selected.append(choice)
    selected.sort(key=lambda item: item.original_url)
    return selected


def write_selected_snapshots(selected: list[SelectedSnapshot], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(selected),
        "snapshots": [item.model_dump() for item in selected],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    LOGGER.info("Wrote %s selected snapshots to %s", len(selected), path)


def read_selected_snapshots(path: Path) -> list[SelectedSnapshot]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [SelectedSnapshot.model_validate(item) for item in payload.get("snapshots", [])]


def ensure_known_snapshot(
    selected: list[SelectedSnapshot],
    known_url: str,
    known_timestamp: str,
) -> list[SelectedSnapshot]:
    """Guarantee the documented Varanasi test snapshot is present."""
    if known_timestamp and not is_eligible_timestamp(known_timestamp):
        return selected
    normalized = normalize_url(known_url)
    for item in selected:
        if item.original_url == normalized:
            return selected
    selected.append(
        SelectedSnapshot(
            original_url=normalized,
            timestamp=known_timestamp,
            status_code=200,
            mime_type="text/html",
            digest="",
            length=0,
            archive_url=build_wayback_url(normalized, known_timestamp),
            raw_archive_url=build_wayback_url(normalized, known_timestamp, raw=True),
            score=SnapshotScore(total=80, status_200=50, mime_html=30),
            selected_reason="known-test-snapshot",
            candidates=1,
        )
    )
    return selected
