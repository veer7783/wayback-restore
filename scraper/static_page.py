"""Extract safe, rendered content from archived WordPress static pages."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from scraper.page_extras import extract_publication_date, extract_videos
from scraper.urls import rewrite_wayback_url

STATIC_PAGE_SELECTORS = {
    "contact": (".contact-right",),
    "news": (".section-contianer",),
    "report-hunduphobia": (".page-container-four",),
}
DEFAULT_SELECTORS = (".vc_section", ".lp-section-row", ".page-container-four", ".section-contianer")
URL_RE = re.compile(r"url\((['\"]?)(.*?)\1\)", re.I)
CSS_RULE_RE = re.compile(r"\.([A-Za-z0-9_-]+)\s*\{([^{}]+)\}", re.S)


def _title(soup: BeautifulSoup, slug: str) -> str:
    if soup.title:
        text = " ".join(soup.title.get_text(" ", strip=True).split())
        for separator in (" – PROJECT HINDUKUSH", " - PROJECT HINDUKUSH"):
            if separator in text:
                text = text.split(separator, 1)[0]
        if text:
            return text
    return slug.replace("-", " ").title()


def _root_nodes(soup: BeautifulSoup, slug: str) -> list[Tag]:
    selectors = STATIC_PAGE_SELECTORS.get(slug, DEFAULT_SELECTORS)
    candidates: list[Tag] = []
    for selector in selectors:
        candidates.extend(node for node in soup.select(selector) if isinstance(node, Tag))
    roots: list[Tag] = []
    candidate_ids = {id(node) for node in candidates}
    for node in candidates:
        parent = node.parent
        nested = False
        while isinstance(parent, Tag):
            if id(parent) in candidate_ids:
                nested = True
                break
            parent = parent.parent
        if not nested and node not in roots:
            roots.append(node)
    return roots


def _absolute(url: str, source_url: str) -> str:
    clean = rewrite_wayback_url((url or "").strip())
    return clean if clean.startswith(("http://", "https://", "mailto:", "tel:")) else urljoin(source_url, clean)


def extract_static_page(html: str, source_url: str, slug: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    custom_css: dict[str, str] = {}
    for style in soup.select("style[data-type='vc_shortcodes-custom-css']"):
        for css_class, declarations in CSS_RULE_RE.findall(style.get_text() or ""):
            custom_css[css_class] = declarations.strip()
    roots = _root_nodes(soup, slug)
    content_soup = BeautifulSoup("<div class=\"pkh-archived-page\"></div>", "lxml")
    container = content_soup.select_one(".pkh-archived-page")
    assert isinstance(container, Tag)
    for node in roots:
        fragment = BeautifulSoup(str(node), "lxml")
        copied = fragment.body.contents[0] if fragment.body and fragment.body.contents else None
        if isinstance(copied, Tag):
            container.append(copied)

    for css_class, declarations in custom_css.items():
        for node in container.select(f".{css_class}"):
            existing = str(node.get("style") or "").strip()
            node["style"] = f"{existing.rstrip(';')};{declarations}" if existing else declarations

    archived_forms = len(container.select("form"))
    for form in container.select("form"):
        form.name = "div"
        form.attrs = {"class": ["pkh-archived-form"], "data-archived-form": "disabled"}
        for select in form.select("select"):
            options = [" ".join(option.get_text(" ", strip=True).split()) for option in select.select("option")]
            replacement = content_soup.new_tag("p")
            replacement.string = " / ".join(filter(None, options))
            select.replace_with(replacement)
        for field in form.select("input, textarea"):
            if str(field.get("type") or "").lower() == "hidden":
                field.decompose()
                continue
            placeholder = str(field.get("placeholder") or "").strip()
            if placeholder:
                replacement = content_soup.new_tag("span")
                replacement["class"] = "pkh-archived-field-label"
                replacement.string = placeholder
                field.replace_with(replacement)
            else:
                field.decompose()
        for button in form.select("button"):
            button.decompose()

    for unsafe in container.select(
        "script, noscript, style, link, meta, object, embed, "
        ".login-form-popup, .md-modal, .md-overlay, .advertisement"
    ):
        unsafe.decompose()
    for tag in container.find_all(True):
        for attr in list(tag.attrs):
            if attr.lower().startswith("on"):
                del tag.attrs[attr]
        if tag.name == "iframe":
            src = _absolute(str(tag.get("src") or ""), source_url)
            if "youtube.com" not in src and "youtu.be" not in src and "vimeo.com" not in src:
                tag.decompose()
                continue
            link = content_soup.new_tag("a", href=src)
            link.string = src
            tag.replace_with(link)
            continue
        if tag.get("href"):
            tag["href"] = _absolute(str(tag["href"]), source_url)
        for attr in ("src", "data-src", "data-lazy-src", "poster"):
            if tag.get(attr):
                tag[attr] = _absolute(str(tag[attr]), source_url)
        if tag.get("style"):
            tag["style"] = URL_RE.sub(
                lambda match: f"url('{_absolute(match.group(2), source_url)}')",
                str(tag["style"]),
            )

    images: list[str] = []
    seen: set[str] = set()
    for tag in container.find_all(True):
        candidates = [
            str(tag.get(attr) or "")
            for attr in ("src", "data-src", "data-lazy-src", "poster")
        ]
        style = str(tag.get("style") or "")
        candidates.extend(match.group(2) for match in URL_RE.finditer(style))
        for candidate in candidates:
            url = _absolute(candidate, source_url)
            host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
            if (
                url
                and host == "projecthindukush.com"
                and "/wp-content/uploads/" in url
                and url not in seen
            ):
                seen.add(url)
                images.append(url)

    videos = extract_videos(container, source_url)
    known_videos = {item["original_video_url"] for item in videos}
    for node in container.select("[data-vc-video-bg]"):
        url = _absolute(str(node.get("data-vc-video-bg") or ""), source_url)
        if url and url not in known_videos:
            known_videos.add(url)
            videos.append(
                {
                    "video_type": "youtube" if "youtu" in url.lower() else "video_background",
                    "original_video_url": url,
                    "poster_url": "",
                    "embed_html": "",
                    "first_party": "projecthindukush.com" in url.lower(),
                }
            )

    content_html = "".join(str(child) for child in container.contents).strip()
    text = " ".join(container.get_text(" ", strip=True).split())
    return {
        "source_url": source_url.rstrip("/"),
        "slug": slug,
        "title": _title(soup, slug),
        "content_html": content_html,
        "content_text": text,
        "content_length": len(text),
        "media_urls": images,
        "videos": videos,
        "publication_date": extract_publication_date(soup),
        "forms_removed": archived_forms,
    }
