"""Optional AI fallback used only when deterministic extraction is incomplete.

The model is never used to invent facts. Every returned value must appear in
the source HTML, otherwise it is discarded as missing_from_archive.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from extractor.listing_extractor import ExtractedListing
from extractor.metadata_extractor import normalize_label

LOGGER = logging.getLogger("pkh.ai")


def _value_in_html(value: str, html: str) -> bool:
    if not value:
        return False
    return value.lower() in html.lower()


def apply_ai_extraction(
    listing: ExtractedListing,
    html: str,
    required_fields: list[str],
    enabled: bool,
    api_key: str | None = None,
    model: str = "gpt-4o-mini",
) -> ExtractedListing:
    if not enabled:
        return listing
    if listing.confidence >= 0.75 and not listing.missing:
        return listing
    key = api_key or os.getenv("OPENAI_API_KEY")
    if not key:
        LOGGER.info("AI extraction enabled but no API key; leaving gaps as missing")
        return listing

    prompt = {
        "task": "Extract only fields explicitly present in the HTML.",
        "missing_fields": listing.missing,
        "required_fields": required_fields,
        "rules": [
            "Do not invent values",
            "If a field is not present, return null",
            "Return JSON only",
        ],
        "html_excerpt": html[:12000],
    }
    try:
        response = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": "Extract structured fields from archived HTML. Never invent facts.",
                    },
                    {"role": "user", "content": json.dumps(prompt)},
                ],
            },
            timeout=60,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        payload = json.loads(content)
    except Exception:
        LOGGER.exception("AI extraction failed; keeping deterministic result")
        return listing

    listing.ai_used = True
    fields = payload.get("fields") if isinstance(payload, dict) else None
    if not isinstance(fields, dict):
        return listing

    for key_name, value in fields.items():
        target = normalize_label(str(key_name)).replace(" ", "_")
        if value in (None, "", "missing_from_archive"):
            listing.fields.setdefault(target, None)
            continue
        text = str(value).strip()
        if _value_in_html(text, html):
            listing.fields[target] = text
        else:
            listing.fields[target] = None
            LOGGER.info("Discarded AI value for %s because it was not in HTML", target)

    listing.missing = [
        field for field in required_fields if not listing.fields.get(field)
    ]
    listing.field_status = {
        field: "present" if listing.fields.get(field) else "missing_from_archive"
        for field in required_fields
    }
    return listing
