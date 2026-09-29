"""Deterministic extraction of publication dates, videos, and site chrome."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from scraper.urls import rewrite_wayback_url


def _absolute(url: str, base_url: str) -> str:
    clean = rewrite_wayback_url((url or "").strip())
    return clean if clean.startswith(("http://", "https://")) else urljoin(base_url, clean)


def _iso_datetime(value: str) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.isoformat()


def _json_nodes(value: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(value, dict):
        nodes.append(value)
        for nested in value.values():
            nodes.extend(_json_nodes(nested))
    elif isinstance(value, list):
        for nested in value:
            nodes.extend(_json_nodes(nested))
    return nodes


def extract_publication_date(soup: BeautifulSoup) -> dict[str, str | None]:
    """Return only a deterministic WordPress/article publication date."""
    for key in ("article:published_time", "og:published_time"):
        tag = soup.find("meta", attrs={"property": key})
        if isinstance(tag, Tag) and tag.get("content"):
            value = _iso_datetime(str(tag["content"]))
            if value:
                return {"value": value, "source": key}

    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _json_nodes(payload):
            value = _iso_datetime(str(node.get("datePublished") or ""))
            if value:
                return {"value": value, "source": "json_ld.datePublished"}

    for selector in ("time[datetime]", "[itemprop='datePublished'][datetime]"):
        tag = soup.select_one(selector)
        if isinstance(tag, Tag) and tag.get("datetime"):
            value = _iso_datetime(str(tag["datetime"]))
            if value:
                return {"value": value, "source": "html.time.datetime"}
    return {"value": None, "source": "fallback"}


def extract_videos(soup: BeautifulSoup, base_url: str) -> list[dict[str, Any]]:
    videos: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(kind: str, raw_url: str, *, poster: str = "", embed_html: str = "") -> None:
        url = _absolute(raw_url, base_url)
        if not url or url in seen:
            return
        seen.add(url)
        host = url.lower()
        first_party = "projecthindukush.com" in host
        videos.append(
            {
                "video_type": kind,
                "original_video_url": url,
                "poster_url": _absolute(poster, base_url) if poster else "",
                "embed_html": embed_html,
                "first_party": first_party,
            }
        )

    for video in soup.find_all("video"):
        if not isinstance(video, Tag):
            continue
        poster = str(video.get("poster") or "")
        if video.get("src"):
            add("html5", str(video["src"]), poster=poster, embed_html=str(video))
        for source in video.find_all("source", src=True):
            add("html5", str(source["src"]), poster=poster, embed_html=str(video))

    for iframe in soup.find_all("iframe", src=True):
        url = str(iframe["src"])
        lowered = url.lower()
        kind = "iframe"
        if "youtube.com" in lowered or "youtu.be" in lowered:
            kind = "youtube"
        elif "vimeo.com" in lowered:
            kind = "vimeo"
        add(kind, url, embed_html=str(iframe))

    for anchor in soup.select(
        ".post-detail-content a[href], .entry-content a[href], "
        ".lp-listing-description a[href], a[href*='youtube.com'], "
        "a[href*='youtu.be'], a[href*='vimeo.com']"
    ):
        url = str(anchor.get("href") or "")
        lowered = url.lower()
        if lowered.endswith((".mp4", ".webm", ".ogv", ".mov")):
            add("video_link", url)
        elif "youtube.com" in lowered or "youtu.be" in lowered:
            add("youtube", url)
        elif "vimeo.com" in lowered:
            add("vimeo", url)
    return videos


def extract_site_chrome(soup: BeautifulSoup, base_url: str) -> dict[str, Any]:
    header = soup.find("header")
    footer = soup.find("footer")
    page = soup.select_one("[data-sitelogo]")
    logo = str(page.get("data-sitelogo") or "") if isinstance(page, Tag) else ""
    if not logo and isinstance(header, Tag):
        image = header.select_one("img[src]")
        if isinstance(image, Tag):
            logo = str(image.get("src") or "")

    navigation: list[dict[str, str]] = []
    if isinstance(header, Tag):
        seen: set[tuple[str, str]] = set()
        for anchor in header.select("nav a[href], .menu a[href], ul.menu a[href]"):
            text = " ".join(anchor.get_text(" ", strip=True).split())
            url = _absolute(str(anchor.get("href") or ""), base_url)
            key = (text, url)
            if text and url and key not in seen:
                seen.add(key)
                navigation.append({"label": text, "url": url})

    footer_links: list[dict[str, str]] = []
    if isinstance(footer, Tag):
        for anchor in footer.select("a[href]"):
            text = " ".join(anchor.get_text(" ", strip=True).split())
            url = _absolute(str(anchor.get("href") or ""), base_url)
            if text and url:
                footer_links.append({"label": text, "url": url})

    return {
        "logo_url": _absolute(logo, base_url) if logo else "",
        "header_html": str(header) if isinstance(header, Tag) else "",
        "navigation": navigation,
        "footer_html": str(footer) if isinstance(footer, Tag) else "",
        "footer_text": " ".join(footer.get_text(" ", strip=True).split()) if isinstance(footer, Tag) else "",
        "footer_links": footer_links,
    }
