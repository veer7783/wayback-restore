"""WordPress REST API client. Credentials are never logged."""

from __future__ import annotations

import logging
from typing import Any

import httpx

LOGGER = logging.getLogger("pkh.wp")


class WordPressError(RuntimeError):
    pass


class WordPressClient:
    def __init__(self, base_url: str, username: str, password: str, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            auth=(username, password),
            timeout=timeout,
            headers={"User-Agent": "PKH-Restorer/0.1"},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self._client.request(method, path, **kwargs)
        if response.status_code >= 400:
            LOGGER.error("WordPress HTTP %s for %s", response.status_code, path)
            raise WordPressError(f"WordPress HTTP {response.status_code} for {path}")
        return response

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params).json()

    def post_json(self, path: str, payload: dict[str, Any]) -> Any:
        return self.request("POST", path, json=payload).json()

    def verify_rest(self) -> dict[str, Any]:
        data = self.get_json("/wp-json/")
        return {
            "name": data.get("name"),
            "url": data.get("url"),
            "namespaces": data.get("namespaces", []),
        }

    def find_posts(self, source_url: str, post_type: str) -> list[dict[str, Any]]:
        routes = [
            f"/wp-json/wp/v2/{post_type}s",
            f"/wp-json/wp/v2/{post_type}",
            "/wp-json/pkh-restorer/v1/find",
        ]
        for route in routes:
            try:
                result = self.get_json(route, params={"_archive_source_url": source_url, "per_page": 20})
                if isinstance(result, list):
                    return result
                if isinstance(result, dict) and result.get("posts"):
                    return list(result["posts"])
            except WordPressError:
                continue
        return []
