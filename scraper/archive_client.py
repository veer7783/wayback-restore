"""HTTP client for documented Internet Archive endpoints.

Respects delay, retries with exponential backoff, and never attempts to
bypass Archive.org rate limits.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

import httpx
from tenacity import RetryError, retry, retry_if_exception, stop_after_attempt, wait_exponential

from scraper.urls import is_wayback_url, same_registrable_host

LOGGER = logging.getLogger("pkh.archive")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ArchiveClientError(RuntimeError):
    pass


class RateLimitedError(ArchiveClientError):
    pass


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, RateLimitedError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    return False


class ArchiveClient:
    def __init__(
        self,
        user_agent: str,
        delay_seconds: float = 2.0,
        timeout_seconds: float = 60.0,
        max_retries: int = 5,
        allowed_hosts: list[str] | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.delay_seconds = max(0.0, delay_seconds)
        self.timeout = httpx.Timeout(timeout_seconds)
        self.max_retries = max(1, max_retries)
        self.allowed_hosts = [host.lower() for host in (allowed_hosts or [])]
        self._last_request_at = 0.0
        self._client = httpx.Client(
            timeout=self.timeout,
            headers={"User-Agent": self.user_agent, "Accept": "*/*"},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ArchiveClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _assert_allowed(self, url: str) -> None:
        host = (urlsplit(url).hostname or "").lower()
        if not self.allowed_hosts:
            return
        if any(same_registrable_host(url, allowed) or host == allowed for allowed in self.allowed_hosts):
            return
        if is_wayback_url(url) and any(allowed.endswith("archive.org") for allowed in self.allowed_hosts):
            return
        raise ArchiveClientError(f"Refusing download from disallowed host: {host}")

    def _respect_delay(self) -> None:
        if self.delay_seconds <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.delay_seconds - elapsed
        if remaining > 0:
            LOGGER.debug("Waiting %.2fs before next Archive.org request", remaining)
            time.sleep(remaining)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
        retries: int | None = None,
    ) -> httpx.Response:
        self._assert_allowed(url)
        attempts = retries if retries is not None else self.max_retries

        @retry(
            retry=retry_if_exception(_is_retryable),
            wait=wait_exponential(multiplier=2, min=2, max=60),
            stop=stop_after_attempt(attempts),
            reraise=True,
        )
        def _do() -> httpx.Response:
            self._respect_delay()
            LOGGER.info("Archive request %s %s", method.upper(), url)
            response = self._client.request(method, url, params=params, headers=headers)
            self._last_request_at = time.monotonic()
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                LOGGER.warning("Archive.org rate limited; Retry-After=%s", retry_after)
                raise RateLimitedError("Archive.org returned HTTP 429")
            if response.status_code in RETRYABLE_STATUS:
                response.raise_for_status()
            return response

        try:
            return _do()
        except (RetryError, RateLimitedError, httpx.HTTPError) as exc:
            raise ArchiveClientError(f"Giving up on {url}: {exc}") from exc

    def get_text(self, url: str, params: dict[str, Any] | None = None) -> tuple[httpx.Response, str]:
        response = self.request("GET", url, params=params)
        response.raise_for_status()
        return response, response.text

    def get_bytes(self, url: str, params: dict[str, Any] | None = None) -> tuple[httpx.Response, bytes]:
        response = self.request("GET", url, params=params)
        return response, response.content
