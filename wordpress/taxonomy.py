"""Create or reuse WordPress/ListingPro taxonomies."""

from __future__ import annotations

from typing import Any

from wordpress.client import WordPressClient, WordPressError


def ensure_term(
    client: WordPressClient,
    taxonomy: str,
    name: str,
    slug: str | None = None,
) -> int | None:
    if not name:
        return None
    routes = {
        "category": "/wp-json/wp/v2/categories",
        "listing-category": "/wp-json/wp/v2/listing-category",
        "location": "/wp-json/wp/v2/location",
    }
    path = routes.get(taxonomy, f"/wp-json/wp/v2/{taxonomy}")
    try:
        existing = client.get_json(path, params={"search": name, "per_page": 20})
        if isinstance(existing, list):
            for term in existing:
                if str(term.get("name", "")).lower() == name.lower():
                    return int(term["id"])
    except WordPressError:
        existing = []

    try:
        created = client.post_json(path, {"name": name, "slug": slug or ""})
        return int(created["id"])
    except WordPressError:
        # Plugin fallback
        created = client.post_json(
            "/wp-json/pkh-restorer/v1/term",
            {"taxonomy": taxonomy, "name": name, "slug": slug or ""},
        )
        return int(created["id"]) if created.get("id") else None


def location_term_name(location: dict[str, Any]) -> str | None:
    if location.get("raw"):
        return str(location["raw"])
    parts = [location.get("city"), location.get("state"), location.get("country")]
    joined = ", ".join(part for part in parts if part)
    return joined or None
