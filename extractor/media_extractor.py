"""Select listing-relevant images from a parsed page."""

from __future__ import annotations

from typing import Any

from scraper.media import collect_media


def extract_media(
    parsed: dict[str, Any],
    timestamp: str,
    skip_substrings: list[str] | None = None,
) -> list[dict[str, Any]]:
    assets = collect_media(
        parsed.get("images") or [],
        source_page=str(parsed.get("source_url") or ""),
        timestamp=timestamp,
        skip_substrings=skip_substrings,
    )
    return [asset.model_dump() for asset in assets]
