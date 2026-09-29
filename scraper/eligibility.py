"""Snapshot timestamp eligibility. Incident/event dates are never used."""

from __future__ import annotations

DEFAULT_SNAPSHOT_CUTOFF = "20260228235959"


def normalize_timestamp(timestamp: str | None) -> str:
    digits = "".join(ch for ch in str(timestamp or "") if ch.isdigit())
    if not digits:
        return ""
    return digits.ljust(14, "0")[:14]


def is_eligible_timestamp(timestamp: str | None, cutoff: str = DEFAULT_SNAPSHOT_CUTOFF) -> bool:
    """True only when the Wayback snapshot timestamp is at or before the cutoff."""
    stamp = normalize_timestamp(timestamp)
    limit = normalize_timestamp(cutoff)
    if not stamp or not limit:
        return False
    return stamp <= limit


def partition_by_cutoff(timestamps: list[str], cutoff: str = DEFAULT_SNAPSHOT_CUTOFF) -> dict[str, list[str]]:
    eligible: list[str] = []
    rejected: list[str] = []
    for stamp in timestamps:
        if is_eligible_timestamp(stamp, cutoff):
            eligible.append(normalize_timestamp(stamp))
        else:
            rejected.append(normalize_timestamp(stamp))
    return {"eligible": eligible, "rejected": rejected}
