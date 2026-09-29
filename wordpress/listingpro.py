"""Inspect an installed ListingPro theme instead of assuming its schema."""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

REGISTER_POST_TYPE_RE = re.compile(r"register_post_type\(\s*['\"]([^'\"]+)['\"]")
REGISTER_TAXONOMY_RE = re.compile(r"register_taxonomy\(\s*['\"]([^'\"]+)['\"]")
REGISTER_META_RE = re.compile(r"register_post_meta\(\s*['\"][^'\"]+['\"]\s*,\s*['\"]([^'\"]+)['\"]")
META_KEY_RE = re.compile(r"['\"](lp_[a-zA-Z0-9_]+)['\"]")


def inspect_php_tree(root: Path) -> dict[str, list[str]]:
    post_types: set[str] = set()
    taxonomies: set[str] = set()
    meta_fields: set[str] = set()
    if not root.exists():
        return {"post_types": [], "taxonomies": [], "meta_fields": []}

    for php in root.rglob("*.php"):
        try:
            text = php.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        post_types.update(REGISTER_POST_TYPE_RE.findall(text))
        taxonomies.update(REGISTER_TAXONOMY_RE.findall(text))
        meta_fields.update(REGISTER_META_RE.findall(text))
        if "listingpro" in text.lower() or "lp_" in text:
            meta_fields.update(META_KEY_RE.findall(text))
    return {
        "post_types": sorted(post_types),
        "taxonomies": sorted(taxonomies),
        "meta_fields": sorted(meta_fields),
    }


def inspect_theme_zip(zip_path: Path) -> dict[str, list[str]]:
    if not zip_path.exists():
        return {"post_types": [], "taxonomies": [], "meta_fields": []}
    post_types: set[str] = set()
    taxonomies: set[str] = set()
    meta_fields: set[str] = set()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            if not info.filename.lower().endswith(".php"):
                continue
            try:
                text = archive.read(info).decode("utf-8", errors="ignore")
            except Exception:
                continue
            post_types.update(REGISTER_POST_TYPE_RE.findall(text))
            taxonomies.update(REGISTER_TAXONOMY_RE.findall(text))
            meta_fields.update(REGISTER_META_RE.findall(text))
            if "listingpro" in text.lower() or "lp_" in text:
                meta_fields.update(META_KEY_RE.findall(text))
    return {
        "post_types": sorted(post_types),
        "taxonomies": sorted(taxonomies),
        "meta_fields": sorted(meta_fields),
    }


def merge_schema(*parts: dict[str, list[str]]) -> dict[str, Any]:
    merged = {"post_types": set(), "taxonomies": set(), "meta_fields": set()}
    for part in parts:
        for key in merged:
            merged[key].update(part.get(key) or [])
    return {key: sorted(value) for key, value in merged.items()}


def write_schema(schema: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema, indent=2), encoding="utf-8")


def choose_post_type(schema: dict[str, Any], default: str, fallback: str) -> str:
    raw_types = schema.get("post_types") or []
    types: set[str] = set()
    for item in raw_types:
        if isinstance(item, str):
            types.add(item)
        elif isinstance(item, dict) and item.get("name"):
            types.add(str(item["name"]))
    if default in types:
        return default
    if "listing" in types:
        return "listing"
    return fallback
