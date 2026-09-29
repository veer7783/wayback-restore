"""Normalize discovered labels using the editable field mapping."""

from __future__ import annotations

import re
from typing import Any


def normalize_label(label: str) -> str:
    cleaned = label.lower().strip()
    cleaned = cleaned.replace("(please provide source link)", "")
    cleaned = re.sub(r"[^a-z0-9? ]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ?")
    return cleaned


def mapping_aliases(field_mapping: dict[str, Any]) -> dict[str, str]:
    """Map normalized labels to target field names."""
    aliases: dict[str, str] = {}
    for key, spec in field_mapping.items():
        if not isinstance(spec, dict):
            continue
        target = str(spec.get("target") or key)
        if spec.get("source_label"):
            aliases[normalize_label(str(spec["source_label"]))] = target
        for alias in spec.get("aliases") or []:
            aliases[normalize_label(str(alias))] = target
    return aliases


def map_labeled_fields(
    labeled_fields: dict[str, str],
    field_mapping: dict[str, Any],
) -> dict[str, str | None]:
    aliases = mapping_aliases(field_mapping)
    mapped: dict[str, str | None] = {}
    for raw_label, value in labeled_fields.items():
        target = aliases.get(normalize_label(raw_label))
        if not target:
            continue
        mapped[target] = value.strip() if value and value.strip() else None
    return mapped


def required_field_status(
    mapped: dict[str, str | None],
    required: list[str],
) -> dict[str, str]:
    status: dict[str, str] = {}
    for field in required:
        value = mapped.get(field)
        if value:
            status[field] = "present"
        else:
            status[field] = "missing_from_archive"
    return status
