"""Choose the best main content block from a parsed page."""

from __future__ import annotations

import re
from typing import Any


JUNK_PATTERNS = (
    r"share this",
    r"leave a comment",
    r"related listings",
    r"you may also like",
)


def clean_content(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        if any(re.search(pattern, line, re.I) for pattern in JUNK_PATTERNS):
            continue
        lines.append(line)
    return "\n\n".join(lines)


def extract_content(parsed: dict[str, Any]) -> str:
    content = clean_content(str(parsed.get("content") or ""))
    if content:
        return content
    sections = parsed.get("sections") or []
    joined = "\n\n".join(
        f"{section.get('title', '')}\n{section.get('text', '')}".strip()
        for section in sections
        if section.get("text")
    )
    return clean_content(joined)


def content_confidence(content: str, labeled_fields: dict[str, str]) -> float:
    if not content:
        return 0.0
    score = 0.4
    if len(content) > 400:
        score += 0.3
    if labeled_fields:
        score += 0.2
    if "\n" in content:
        score += 0.1
    return min(1.0, score)
