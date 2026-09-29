"""Upload restored media through the WordPress REST API."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from wordpress.client import WordPressClient, WordPressError


def upload_media(client: WordPressClient, local_path: str, title: str, source_url: str) -> int | None:
    path = Path(local_path)
    if not path.exists():
        return None
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    files = {"file": (path.name, path.read_bytes(), mime)}
    data = {"title": title, "caption": source_url}
    try:
        response = client.request(
            "POST",
            "/wp-json/wp/v2/media",
            files=files,
            data=data,
            headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
        )
        payload = response.json()
        return int(payload["id"])
    except WordPressError:
        response = client.request(
            "POST",
            "/wp-json/pkh-restorer/v1/media",
            files=files,
            data={"title": title, "source_url": source_url},
        )
        payload = response.json()
        return int(payload["id"]) if payload.get("id") else None


def attach_featured(client: WordPressClient, post_id: int, media_id: int) -> None:
    try:
        client.request("POST", f"/wp-json/wp/v2/posts/{post_id}", json={"featured_media": media_id})
    except WordPressError:
        client.post_json(
            "/wp-json/pkh-restorer/v1/featured",
            {"post_id": post_id, "media_id": media_id},
        )
