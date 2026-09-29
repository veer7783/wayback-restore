"""Validate restored WordPress pages against archived extracts."""

from __future__ import annotations

import csv
import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any

import httpx

from extractor.listing_extractor import ExtractedListing
from pipeline.context import Runtime
from scraper.snapshots import read_selected_snapshots
from scraper.urls import slug_from_url, url_hash
from utils.config import resolve_path

LOGGER = logging.getLogger("pkh.validate")


def _similarity(left: str, right: str) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left.lower(), right.lower()).ratio()


def score_restoration(source: ExtractedListing, restored: dict[str, Any], weights: dict[str, float]) -> dict[str, Any]:
    title = _similarity(source.title or "", restored.get("title") or "")
    content = _similarity(source.content[:1500], restored.get("content") or "")
    field_hits = 0
    field_total = max(1, len(source.fields))
    for key, value in source.fields.items():
        if value and value.lower() in (restored.get("text") or "").lower():
            field_hits += 1
    fields = field_hits / field_total
    image_hits = 0
    for image in source.images:
        name = (image.get("filename") or "").split("_")[-1]
        if name and name in (restored.get("html") or ""):
            image_hits += 1
    images = 1.0 if not source.images else image_hits / len(source.images)
    url_score = 1.0 if source.slug and source.slug in (restored.get("url") or "") else 0.0
    category = 1.0 if not source.category or (source.category.lower() in (restored.get("text") or "").lower()) else 0.0
    location = 1.0 if not source.location.raw or (source.location.raw.lower() in (restored.get("text") or "").lower()) else 0.0

    total = (
        title * weights.get("title", 0.2)
        + content * weights.get("content", 0.3)
        + fields * weights.get("fields", 0.2)
        + images * weights.get("images", 0.15)
        + url_score * weights.get("url", 0.05)
        + category * weights.get("category", 0.05)
        + location * weights.get("location", 0.05)
    )
    missing = []
    if title < 0.5:
        missing.append("title")
    if content < 0.4:
        missing.append("content")
    if fields < 0.5:
        missing.append("fields")
    if images < 0.5:
        missing.append("images")
    return {
        "score": round(total * 100, 1),
        "parts": {
            "title": round(title, 3),
            "content": round(content, 3),
            "fields": round(fields, 3),
            "images": round(images, 3),
            "url": url_score,
            "category": category,
            "location": location,
        },
        "missing": missing,
    }


def run_validate(runtime: Runtime, limit: int) -> list[dict[str, Any]]:
    settings = runtime.config["settings"]
    selected = read_selected_snapshots(resolve_path(runtime.config, "selected_snapshots"))[:limit]
    parsed_dir = resolve_path(runtime.config, "parsed")
    rows: list[dict[str, Any]] = []
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for snapshot in selected:
            extract_path = parsed_dir / f"{slug_from_url(snapshot.original_url)}_{url_hash(snapshot.original_url)[:16]}.json"
            if not extract_path.exists():
                continue
            listing = ExtractedListing.model_validate_json(extract_path.read_text(encoding="utf-8"))
            wp_url = f"{settings.wp_url.rstrip('/')}/{listing.slug}/"
            status = 0
            html = ""
            title = ""
            try:
                response = client.get(wp_url)
                status = response.status_code
                html = response.text
                match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
                title = re.sub(r"\s+", " ", match.group(1)).strip() if match else ""
            except httpx.HTTPError as exc:
                LOGGER.warning("Validation fetch failed for %s: %s", wp_url, exc)

            wayback_left = bool(re.search(r"web\.archive\.org", html, re.I))
            broken = len(re.findall(r"href=\"#\"", html))
            restored = {
                "title": title,
                "content": re.sub(r"<[^>]+>", " ", html),
                "text": re.sub(r"<[^>]+>", " ", html),
                "html": html,
                "url": wp_url,
            }
            scored = score_restoration(listing, restored, runtime.config.get("match_weights") or {})
            rows.append(
                {
                    "URL": listing.source_url,
                    "WP_URL": wp_url,
                    "STATUS": status,
                    "TITLE": title,
                    "IMAGES": html.lower().count("<img"),
                    "BROKEN_LINKS": broken,
                    "FIELDS": len([value for value in listing.fields.values() if value]),
                    "MATCH_SCORE": scored["score"],
                    "WAYBACK_URLS": wayback_left,
                    "MISSING": "|".join(scored["missing"]),
                }
            )
            row = runtime.store.upsert_url(listing.source_url, url_hash(listing.source_url))
            runtime.store.record_import(row.id, {"source_url": listing.source_url, "match_score": scored["score"]})

    csv_path = resolve_path(runtime.config, "validation_csv")
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "URL",
                "WP_URL",
                "STATUS",
                "TITLE",
                "IMAGES",
                "BROKEN_LINKS",
                "FIELDS",
                "MATCH_SCORE",
                "WAYBACK_URLS",
                "MISSING",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)
    LOGGER.info("Wrote validation report for %s pages", len(rows))
    return rows
