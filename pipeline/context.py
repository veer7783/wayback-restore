"""Shared runtime context for CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scraper.archive_client import ArchiveClient
from state.db import StateStore
from utils.config import load_config, load_field_mapping, resolve_path
from utils.logging import setup_logging


@dataclass
class Runtime:
    config: dict[str, Any]
    mapping: dict[str, Any]
    store: StateStore
    client: ArchiveClient
    root: Path


def build_runtime() -> Runtime:
    config = load_config()
    mapping = load_field_mapping()
    root: Path = config["root"]
    setup_logging(root / "data" / "logs")
    store = StateStore(resolve_path(config, "sqlite"))
    wayback = config["wayback"]
    client = ArchiveClient(
        user_agent=wayback["user_agent"],
        delay_seconds=float(wayback["delay_seconds"]),
        timeout_seconds=float(wayback["timeout_seconds"]),
        max_retries=int(wayback["max_retries"]),
        allowed_hosts=list(wayback.get("allowed_download_hosts") or []),
    )
    return Runtime(config=config, mapping=mapping, store=store, client=client, root=root)
