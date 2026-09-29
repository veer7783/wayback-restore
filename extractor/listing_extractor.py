"""Normalize an archived incident page into a ListingPro-oriented JSON record."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from extractor.content_extractor import content_confidence, extract_content
from extractor.media_extractor import extract_media
from extractor.metadata_extractor import map_labeled_fields, required_field_status
from scraper.urls import slug_from_url


class Location(BaseModel):
    raw: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    lat: str | None = None
    lng: str | None = None


class ExtractedListing(BaseModel):
    source_url: str
    title: str | None = None
    post_type: str = "listing"
    category: str | None = None
    location: Location = Field(default_factory=Location)
    fields: dict[str, str | None] = Field(default_factory=dict)
    content: str = ""
    images: list[dict[str, Any]] = Field(default_factory=list)
    links: list[dict[str, Any]] = Field(default_factory=list)
    labeled_fields_raw: dict[str, str] = Field(default_factory=dict)
    field_status: dict[str, str] = Field(default_factory=dict)
    confidence: float = 0.0
    listingpro_detected: bool = False
    slug: str = ""
    extracted_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    ai_used: bool = False
    missing: list[str] = Field(default_factory=list)


def parse_location(raw: str | None) -> Location:
    if not raw:
        return Location()
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    location = Location(raw=raw)
    if len(parts) == 1:
        location.city = parts[0]
    elif len(parts) == 2:
        location.city, location.country = parts
    elif len(parts) >= 3:
        location.city = parts[0]
        location.state = parts[1]
        location.country = ", ".join(parts[2:])
    return location


def _links_as_list(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    grouped = parsed.get("links") or {}
    items: list[dict[str, Any]] = []
    for kind, urls in grouped.items():
        for url in urls:
            items.append({"type": kind, "url": url})
    return items


def extract_listing(
    parsed: dict[str, Any],
    field_mapping: dict[str, Any],
    required_fields: list[str],
    timestamp: str,
    skip_substrings: list[str] | None = None,
) -> ExtractedListing:
    labeled = parsed.get("labeled_fields") or {}
    mapped = map_labeled_fields(labeled, field_mapping)
    location_value = mapped.get("location") or parsed.get("location")
    if isinstance(location_value, str) and "get directions" in location_value.lower():
        location_value = location_value.replace("Get Directions", "").strip()
    content = extract_content(parsed)
    status = required_field_status(mapped, required_fields)
    missing = [key for key, value in status.items() if value != "present"]
    confidence = content_confidence(content, labeled)
    if parsed.get("title"):
        confidence = min(1.0, confidence + 0.1)
    if not missing:
        confidence = min(1.0, confidence + 0.2)

    signals = parsed.get("listingpro_signals") or {}
    listing = ExtractedListing(
        source_url=str(parsed.get("source_url") or ""),
        title=parsed.get("title") or None,
        category=parsed.get("category") or None,
        location=parse_location(location_value),
        fields=mapped,
        content=content,
        images=extract_media(parsed, timestamp, skip_substrings),
        links=_links_as_list(parsed),
        labeled_fields_raw=labeled,
        field_status=status,
        confidence=round(confidence, 3),
        listingpro_detected=bool(signals.get("is_listingpro")),
        slug=slug_from_url(str(parsed.get("canonical_url") or parsed.get("source_url") or "")),
        missing=missing,
    )
    coords = parsed.get("coordinates") or {}
    if coords.get("lat"):
        listing.location.lat = str(coords["lat"])
    if coords.get("lng"):
        listing.location.lng = str(coords["lng"])
    if listing.location.raw and "location" not in listing.fields:
        listing.fields["location"] = listing.location.raw
    return listing


def write_extracted(listing: ExtractedListing, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(listing.model_dump(), indent=2), encoding="utf-8")
