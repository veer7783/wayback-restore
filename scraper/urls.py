"""URL normalization, Wayback construction, and archive rewriting."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

WAYBACK_HOSTS = {"web.archive.org", "wayback.archive.org"}
WAYBACK_PATH_RE = re.compile(
    r"^/web/(?P<timestamp>\d{1,14})(?P<flags>[a-zA-Z]{0,4}_)?/(?P<original>.+)$"
)
WAYBACK_EMBEDDED_RE = re.compile(
    r"https?://web\.archive\.org/web/\d{1,14}(?:[a-zA-Z]{0,4}_)?/(https?://\S+)",
    re.IGNORECASE,
)
TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
}


def normalize_url(url: str, keep_fragment: bool = False) -> str:
    """Canonicalize a URL for deduplication and storage."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("URL is empty")

    if raw.startswith("//"):
        raw = "https:" + raw

    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    host = parts.hostname.lower() if parts.hostname else ""
    if parts.port and not (
        (scheme == "http" and parts.port == 80) or (scheme == "https" and parts.port == 443)
    ):
        netloc = f"{host}:{parts.port}"
    else:
        netloc = host

    path = parts.path or "/"
    if path != "/":
        path = re.sub(r"/{2,}", "/", path)
        if path.endswith("/") and path != "/":
            path = path.rstrip("/")

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
    ]
    query = urlencode(sorted(query_pairs), doseq=True)
    fragment = parts.fragment if keep_fragment else ""
    return urlunsplit((scheme, netloc, path, query, fragment))


def is_wayback_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in WAYBACK_HOSTS


def split_wayback_url(url: str) -> tuple[str | None, str]:
    """Return (timestamp, original_url) for a Wayback URL, else (None, url)."""
    parts = urlsplit(url)
    if (parts.hostname or "").lower() not in WAYBACK_HOSTS:
        return None, url
    match = WAYBACK_PATH_RE.match(parts.path)
    if not match:
        return None, url
    original = match.group("original")
    if original.startswith("http:/") and not original.startswith("http://"):
        original = original.replace("http:/", "http://", 1)
    if original.startswith("https:/") and not original.startswith("https://"):
        original = original.replace("https:/", "https://", 1)
    return match.group("timestamp"), original


def rewrite_wayback_url(url: str) -> str:
    """Strip Wayback wrappers so restored content never links to archive.org."""
    current = url
    for _ in range(5):
        timestamp, original = split_wayback_url(current)
        if timestamp is None:
            break
        current = original
    return current


def rewrite_wayback_text(text: str) -> str:
    """Replace embedded Wayback URLs inside HTML or plain text."""

    def _replace(match: re.Match[str]) -> str:
        return rewrite_wayback_url(match.group(0))

    return WAYBACK_EMBEDDED_RE.sub(_replace, text)


def build_wayback_url(original_url: str, timestamp: str, raw: bool = False) -> str:
    stamp = "".join(ch for ch in timestamp if ch.isdigit())
    if not stamp:
        raise ValueError("timestamp is required")
    flags = "id_" if raw else ""
    normalized = normalize_url(rewrite_wayback_url(original_url))
    return f"https://web.archive.org/web/{stamp}{flags}/{normalized}"


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def short_url_hash(url: str, length: int = 16) -> str:
    return url_hash(url)[:length]


def slug_from_url(url: str) -> str:
    path = urlsplit(normalize_url(url)).path.rstrip("/")
    slug = path.rsplit("/", 1)[-1] if path else "page"
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", slug).strip("-").lower()
    return slug or "page"


def html_basename(url: str) -> str:
    return f"{slug_from_url(url)}_{short_url_hash(url)}.html"


def strip_www(url: str) -> str:
    """Collapse www and apex hosts so the same incident is not counted twice."""
    normalized = normalize_url(rewrite_wayback_url(url))
    parts = urlsplit(normalized)
    host = parts.hostname or ""
    if host.startswith("www."):
        host = host[4:]
        return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return normalized


def host_variants(url: str) -> list[str]:
    apex = strip_www(url)
    parts = urlsplit(apex)
    host = parts.hostname or ""
    if not host:
        return [apex]
    www = urlunsplit((parts.scheme, "www." + host, parts.path, parts.query, parts.fragment))
    if www == apex:
        return [apex]
    return [apex, www]


def is_incident_url(url: str, prefix: str = "/incident/") -> bool:
    original = rewrite_wayback_url(url)
    path = urlsplit(normalize_url(original)).path.lower()
    if prefix not in path:
        return False
    remainder = path.split(prefix, 1)[-1]
    if not remainder or remainder.startswith("page/"):
        return False
    return True


def same_registrable_host(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    domain = domain.lower().lstrip(".")
    return host == domain or host.endswith("." + domain)


def is_internal_url(url: str, domain: str) -> bool:
    original = rewrite_wayback_url(url)
    if original.startswith("/") and not original.startswith("//"):
        return True
    return same_registrable_host(original, domain)
