"""Parse archived ListingPro HTML without mutating the original file."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from scraper.page_extras import extract_publication_date, extract_site_chrome, extract_videos
from scraper.urls import is_internal_url, rewrite_wayback_url

LABEL_VALUE_RE = re.compile(r"^\s*([^:]{2,80}):\s*(.+?)\s*$")


def parse_html(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def visible_text(node: Tag | None) -> str:
    if node is None:
        return ""
    return " ".join(node.get_text(" ", strip=True).split())


def extract_title(soup: BeautifulSoup) -> str:
    for selector in (
        "h1.lp-listing-name",
        "h1.listing-title",
        ".lp-listing-title h1",
        "h1.entry-title",
        "h1",
    ):
        node = soup.select_one(selector)
        if node and visible_text(node):
            return visible_text(node)
    if soup.title and soup.title.string:
        title = soup.title.string.split("–")[0].split("-")[0].strip()
        return title
    return ""


def extract_canonical(soup: BeautifulSoup, fallback: str) -> str:
    link = soup.find("link", rel=lambda value: value and "canonical" in value)
    if isinstance(link, Tag) and link.get("href"):
        return rewrite_wayback_url(str(link["href"]))
    og = soup.find("meta", property="og:url")
    if isinstance(og, Tag) and og.get("content"):
        return rewrite_wayback_url(str(og["content"]))
    return rewrite_wayback_url(fallback)


def extract_breadcrumbs(soup: BeautifulSoup) -> list[str]:
    crumbs: list[str] = []
    for selector in (".breadcrumb", ".breadcrumbs", "nav.rank-math-breadcrumb", ".lp-breadcrumb"):
        node = soup.select_one(selector)
        if not node:
            continue
        for item in node.find_all(["a", "span", "li"]):
            text = visible_text(item)
            if text and text.lower() not in {"/", "»", ">"}:
                crumbs.append(text)
        if crumbs:
            break
    return crumbs


def extract_category(soup: BeautifulSoup, breadcrumbs: list[str], title: str = "") -> str | None:
    for anchor in soup.select("a[href*='incident-category'], a[href*='listing-category']"):
        text = visible_text(anchor)
        if text and text.lower() not in {"home"}:
            return text
    for selector in (
        ".listing-cat a",
        ".lp-listing-cats a",
        "a[rel='category']",
        ".entry-category a",
    ):
        node = soup.select_one(selector)
        if node and visible_text(node):
            return visible_text(node)
    crumbs = [item for item in breadcrumbs if item.lower() not in {"home", "/", "»", ">"}]
    if title:
        crumbs = [item for item in crumbs if item.lower() != title.lower()]
    if crumbs:
        return crumbs[0]
    return None


def extract_location_text(soup: BeautifulSoup) -> str | None:
    address = soup.select_one(".lp-details-address span:last-child")
    if address and visible_text(address):
        return visible_text(address)
    for selector in (
        ".lp-details-address",
        ".listing-detail-infos .list-st-img",
        ".listing-location",
        ".lp-listing-location",
        ".lp-location",
        "[itemprop='address']",
        ".map-address",
    ):
        node = soup.select_one(selector)
        text = visible_text(node)
        if text:
            return text
    return None


def extract_coordinates(soup: BeautifulSoup) -> dict[str, str] | None:
    trigger = soup.select_one("[data-lat][data-lan], [data-lat][data-lng]")
    if not isinstance(trigger, Tag):
        return None
    lat = trigger.get("data-lat")
    lng = trigger.get("data-lan") or trigger.get("data-lng")
    if not lat or not lng:
        return None
    return {"lat": str(lat), "lng": str(lng)}


def extract_main_content(soup: BeautifulSoup) -> str:
    for selector in (
        ".post-detail-content",
        ".lp-listing-description",
        ".listing-content",
        ".entry-content",
        "article .post-content",
        ".post-content",
        "article",
    ):
        node = soup.select_one(selector)
        if not node:
            continue
        clone = BeautifulSoup(str(node), "lxml")
        for junk in clone.select("script, style, nav, form, .social-networks"):
            junk.decompose()
        text = clone.get_text("\n", strip=True)
        if len(text) > 80:
            return text
    return ""


def extract_main_content_html(soup: BeautifulSoup) -> str:
    """Preserve article links and embeds while removing scripts and unsafe wrappers."""
    for selector in (
        ".post-detail-content",
        ".lp-listing-description",
        ".listing-content",
        ".entry-content",
        "article .post-content",
        ".post-content",
        "article",
    ):
        node = soup.select_one(selector)
        if not node:
            continue
        clone = BeautifulSoup(str(node), "lxml")
        for junk in clone.select("script, style, nav, form, .social-networks"):
            junk.decompose()
        container = clone.select_one(selector) or clone.body
        if isinstance(container, Tag):
            return "".join(str(child) for child in container.contents).strip()
    return ""


def extract_page_sections(soup: BeautifulSoup) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    for heading in soup.find_all(["h2", "h3", "h4"]):
        title = visible_text(heading)
        if not title:
            continue
        bits: list[str] = []
        for sibling in heading.find_next_siblings():
            if sibling.name in {"h2", "h3", "h4"}:
                break
            text = visible_text(sibling) if isinstance(sibling, Tag) else ""
            if text:
                bits.append(text)
        if bits:
            sections.append({"title": title, "text": "\n".join(bits)})
    return sections


def extract_labeled_pairs(soup: BeautifulSoup) -> dict[str, str]:
    """Discover label/value pairs from ListingPro and generic HTML structures."""
    pairs: dict[str, str] = {}

    for dt in soup.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        label = visible_text(dt)
        value = visible_text(dd) if isinstance(dd, Tag) else ""
        if label and value:
            pairs.setdefault(label, value)

    for row in soup.select("table tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            label = visible_text(cells[0])
            value = visible_text(cells[1])
            if label and value:
                pairs.setdefault(label, value)

    for item in soup.find_all("li"):
        text = visible_text(item)
        match = LABEL_VALUE_RE.match(text)
        if match:
            pairs.setdefault(match.group(1).strip(), match.group(2).strip())
            continue
        spans = [visible_text(span) for span in item.find_all(["span", "strong", "h6", "b"]) if visible_text(span)]
        if len(spans) >= 2:
            label = spans[0].rstrip(":")
            value = spans[-1]
            if label and value and label.lower() != value.lower():
                pairs.setdefault(label, value)

    for block in soup.select(
        ".additional-details li, .lp-listing-additional li, .features-list li, "
        ".features-listing li, .extra-fields li, .list-st-img li"
    ):
        text = visible_text(block)
        match = LABEL_VALUE_RE.match(text)
        if match:
            pairs.setdefault(match.group(1).strip(), match.group(2).strip())

    return pairs


def _absolutize(src: str, base_url: str) -> str:
    rewritten = rewrite_wayback_url(src)
    if rewritten.startswith("http://") or rewritten.startswith("https://"):
        return rewritten
    original_base = rewrite_wayback_url(base_url)
    return urljoin(original_base, rewritten)


def extract_images(soup: BeautifulSoup, base_url: str) -> list[dict[str, str]]:
    images: list[dict[str, str]] = []
    seen: set[str] = set()
    og = soup.find("meta", property="og:image")
    if isinstance(og, Tag) and og.get("content"):
        abs_url = _absolutize(str(og["content"]), base_url)
        images.append({"src": abs_url, "alt": "featured", "class": "og-image featured"})
        seen.add(abs_url)
    for img in soup.find_all("img"):
        if not isinstance(img, Tag):
            continue
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if not src:
            continue
        src_text = str(src)
        if src_text.startswith("data:"):
            continue
        abs_url = _absolutize(src_text, base_url)
        if abs_url in seen:
            continue
        seen.add(abs_url)
        parent_classes: list[str] = []
        parent = img.parent
        for _ in range(8):
            if not isinstance(parent, Tag):
                break
            parent_classes.extend(str(item).lower() for item in (parent.get("class") or []))
            if parent.name:
                parent_classes.append(str(parent.name).lower())
            parent = parent.parent
        context = " ".join(parent_classes)
        role = "other"
        if any(token in context for token in ("post-detail-content", "entry-content", "lp-listing-description")):
            role = "content"
        elif any(token in context for token in ("gallery", "listing-slide", "lp-listing-slider", "slick-slide")):
            role = "gallery"
        images.append(
            {
                "src": abs_url,
                "alt": str(img.get("alt") or ""),
                "class": " ".join(img.get("class") or []),
                "context": context[:500],
                "role": role,
            }
        )
    return images


def extract_links(soup: BeautifulSoup, base_url: str, domain: str) -> dict[str, list[str]]:
    internal: list[str] = []
    external: list[str] = []
    source: list[str] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = _absolutize(str(anchor["href"]), base_url)
        if href in seen or href.startswith("mailto:") or href.startswith("javascript:"):
            continue
        seen.add(href)
        if is_internal_url(href, domain):
            internal.append(href)
        else:
            external.append(href)
        text = visible_text(anchor).lower()
        social = any(
            token in href.lower()
            for token in ("facebook.com/sharer", "twitter.com/intent", "linkedin.com/share", "pinterest.com", "reddit.com", "stumbleupon.com")
        )
        if social:
            continue
        if "source" in text or "rediff" in href.lower():
            if not is_internal_url(href, domain):
                source.append(href)
    return {"internal": internal, "external": external, "source": source}


def extract_metadata(soup: BeautifulSoup) -> dict[str, str]:
    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        if not isinstance(tag, Tag):
            continue
        key = tag.get("property") or tag.get("name")
        value = tag.get("content")
        if key and value:
            meta[str(key)] = str(value)
    return meta


def parse_archived_page(html: str, source_url: str, domain: str) -> dict[str, Any]:
    soup = parse_html(html)
    breadcrumbs = extract_breadcrumbs(soup)
    labeled = extract_labeled_pairs(soup)
    title = extract_title(soup)
    location = labeled.get("LOCATION") or labeled.get("Location") or extract_location_text(soup)
    document = {
        "source_url": rewrite_wayback_url(source_url),
        "title": title,
        "canonical_url": extract_canonical(soup, source_url),
        "breadcrumbs": breadcrumbs,
        "category": extract_category(soup, breadcrumbs, title=title),
        "location": location,
        "coordinates": extract_coordinates(soup),
        "content": extract_main_content(soup),
        "content_html": extract_main_content_html(soup),
        "labeled_fields": labeled,
        "images": extract_images(soup, source_url),
        "links": extract_links(soup, source_url, domain),
        "metadata": extract_metadata(soup),
        "publication_date": extract_publication_date(soup),
        "videos": extract_videos(soup, source_url),
        "site_chrome": extract_site_chrome(soup, source_url),
        "sections": extract_page_sections(soup),
        "listingpro_signals": detect_listingpro_signals(html, soup),
    }
    return document


def detect_listingpro_signals(html: str, soup: BeautifulSoup) -> dict[str, Any]:
    lowered = html.lower()
    class_hits = []
    for token in (
        "listingpro",
        "lp-listing",
        "listing-second-view",
        "lp-listing-title",
        "additional-details",
        "features-listing",
        "extra-fields",
        "post-detail-content",
        "single_listing",
        "listing-cat",
        "listing-location",
        "lp_",
    ):
        if token in lowered:
            class_hits.append(token)
    body_classes = []
    if soup.body and soup.body.get("class"):
        body_classes = [str(item) for item in soup.body.get("class", [])]
    return {
        "is_listingpro": bool(class_hits) or any("listing" in item.lower() for item in body_classes),
        "markers": class_hits,
        "body_classes": body_classes,
    }


def looks_like_html(content_type: str, body: bytes) -> bool:
    if "html" in (content_type or "").lower():
        return True
    start = body.lstrip()[:80].lower()
    return start.startswith(b"<!doctype html") or start.startswith(b"<html")
