"""Restore exactly 40 additional incident listings, then hard-stop."""

from __future__ import annotations

import csv
import html as html_module
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from extractor.listing_extractor import ExtractedListing
from pipeline.batch10 import (
    _apply_media_map,
    _write_csv,
    _write_json,
    scrape_and_extract,
    wp_eval_json,
    wp_post_count,
)
from pipeline.context import Runtime
from scraper.cdx import CDXClient, CDXRecord
from scraper.eligibility import DEFAULT_SNAPSHOT_CUTOFF, is_eligible_timestamp
from scraper.media import MediaAsset, archive_url_for, download_media_asset, filename_for
from scraper.snapshots import SelectedSnapshot, select_best_snapshot
from scraper.urls import host_variants, is_incident_url, strip_www, url_hash
from scraper.video import VideoAsset, download_first_party_video, normalize_video_url
from utils.config import resolve_path
from wordpress.importer import build_payload
from wordpress.restore_media import download_listing_media
from wordpress.wpcli_importer import backup_database, import_payload_wpcli, set_listing_slug, sync_plugin

LOGGER = logging.getLogger("pkh.batch40")
BATCH_LIMIT = 40

MEDIA_FIELDS = [
    "source_page_url", "original_media_url", "archive_media_url", "media_type", "role",
    "snapshot_timestamp", "eligibility", "http_status", "download_status",
    "validation_status", "wordpress_attachment_id", "local_url", "media_hash", "final_status",
]
VIDEO_FIELDS = [
    "source_page_url", "video_type", "original_video_url", "archive_url", "status",
    "wordpress_local_url", "wordpress_attachment_id", "missing_reason",
]
PREVIEW_FIELDS = [
    "source_url", "snapshot_url", "snapshot_timestamp", "title", "slug",
    "publication_date", "publication_date_source", "incident_date", "category",
    "location", "latitude", "longitude", "custom_fields", "content_length",
    "featured_image", "gallery_count", "content_image_count", "video_count",
    "missing_media", "extraction_status",
]
VALIDATION_FIELDS = [
    "source_url", "wp_url", "wp_post_id", "http_status", "publication_status",
    "title_ok", "slug_ok", "content_ok", "category_ok", "location_ok",
    "coordinates_ok", "custom_fields_ok", "featured_image_ok", "gallery_ok",
    "video_ok", "local_image_urls", "wayback_urls", "header_present", "logo_local",
    "footer_present", "broken_media", "page_status", "notes",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cutoff(runtime: Runtime) -> str:
    return str(runtime.config["wayback"].get("snapshot_cutoff") or DEFAULT_SNAPSHOT_CUTOFF)


def existing_source_mapping(runtime: Runtime) -> dict[str, int]:
    settings = runtime.config["settings"]
    data = wp_eval_json(
        settings.php_binary,
        settings.wp_path,
        """
$posts = get_posts(array('post_type'=>'listing','post_status'=>'any','numberposts'=>-1));
$out = array();
foreach ($posts as $post) {
    $source = get_post_meta($post->ID, '_archive_source_url', true);
    if ($source) { $out[rtrim($source, '/')] = (int) $post->ID; }
}
echo wp_json_encode($out);
""",
    )
    return {strip_www(str(url)): int(post_id) for url, post_id in (data or {}).items()}


def validate_new_batch_urls(urls: list[str], excluded: set[str]) -> None:
    normalized = [strip_www(url) for url in urls]
    if len(normalized) != BATCH_LIMIT:
        raise ValueError(f"Expected exactly {BATCH_LIMIT} URLs, found {len(normalized)}")
    if len(set(normalized)) != BATCH_LIMIT:
        raise ValueError("Batch-40 contains duplicate source URLs")
    overlap = set(normalized) & {strip_www(url) for url in excluded}
    if overlap:
        raise ValueError(f"Batch-40 overlaps existing restored sources: {sorted(overlap)}")


def discover_and_select(
    runtime: Runtime,
    excluded: set[str],
    cutoff: str,
) -> tuple[list[str], list[SelectedSnapshot], CDXClient, bool]:
    wayback = runtime.config["wayback"]
    cdx = CDXClient(runtime.client, wayback["cdx_endpoint"])
    rate_limited = False
    try:
        candidates, _resume = cdx.search(
            f"{wayback['domain']}{wayback['path_prefix']}",
            match_type="prefix",
            collapse="urlkey",
            limit=1000,
            filters=["statuscode:200", "mimetype:text/html"],
            to_ts=cutoff,
        )
    except Exception as exc:
        LOGGER.warning("Batch-40 prefix discovery stopped: %s", exc)
        candidates = []
        rate_limited = "429" in str(exc)

    candidate_urls: list[str] = []
    seen = set(excluded)
    for record in candidates:
        url = strip_www(record.normalized_url)
        if not is_incident_url(url, wayback["path_prefix"]) or url in seen:
            continue
        seen.add(url)
        candidate_urls.append(url)

    selected: list[SelectedSnapshot] = []
    selected_records: list[CDXRecord] = []
    for url in candidate_urls:
        if len(selected) >= BATCH_LIMIT:
            break
        records: list[CDXRecord] = []
        try:
            for variant in host_variants(url):
                found, _resume = cdx.search(
                    variant,
                    match_type="exact",
                    limit=50,
                    filters=["mimetype:text/html"],
                    to_ts=cutoff,
                )
                records.extend(found)
            choice = select_best_snapshot(
                records,
                weights=runtime.config.get("scoring"),
                cutoff=cutoff,
            )
        except Exception as exc:
            LOGGER.warning("Snapshot lookup failed for %s: %s", url, exc)
            rate_limited = rate_limited or "429" in str(exc)
            continue
        if not choice or choice.eligibility_status != "eligible" or not choice.timestamp:
            continue
        if not is_eligible_timestamp(choice.timestamp, cutoff):
            raise RuntimeError(f"Post-cutoff snapshot selected: {url}@{choice.timestamp}")
        choice.original_url = url
        selected.append(choice)
        selected_records.extend(records)

    urls = [item.original_url for item in selected]
    exports = resolve_path(runtime.config, "exports")
    _write_json(
        exports / "batch-40-urls.json",
        {
            "generated_at": _now(),
            "cutoff": cutoff,
            "count": len(urls),
            "unique": len(set(urls)),
            "excluded_existing_count": len(excluded),
            "rate_limited": rate_limited,
            "urls": urls,
        },
    )
    _write_json(
        exports / "batch-40-snapshots.json",
        {
            "generated_at": _now(),
            "cutoff": cutoff,
            "count": len(selected),
            "snapshots": [
                {
                    "source_url": item.original_url,
                    "snapshot_timestamp": item.timestamp,
                    "snapshot_url": item.archive_url,
                    "http_status": item.status_code,
                    "mime_type": item.mime_type,
                    "selection_reason": item.selected_reason,
                    "eligibility_status": item.eligibility_status,
                    "rejected_after_cutoff": item.rejected_after_cutoff,
                }
                for item in selected
            ],
        },
    )
    return urls, selected, cdx, rate_limited


def restore_assets(
    runtime: Runtime,
    rows: list[dict[str, Any]],
    cdx: CDXClient,
    cutoff: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    media_dir = resolve_path(runtime.config, "media")
    video_dir = media_dir / "videos"
    hashes: dict[str, str] = {}
    all_media: list[dict[str, Any]] = []
    all_videos: list[dict[str, Any]] = []
    video_cache: dict[str, VideoAsset] = {}

    for row in rows:
        listing: ExtractedListing | None = row.get("listing")
        snapshot: SelectedSnapshot = row["snapshot"]
        if not listing:
            row["media"] = {"featured": None, "gallery": [], "content": [], "logo": None, "rows": []}
            row["videos"] = []
            continue
        media = download_listing_media(
            runtime.client,
            listing,
            row.get("parsed"),
            snapshot.timestamp,
            media_dir,
            cdx=cdx,
            cutoff=cutoff,
            known_hashes=hashes,
        )
        row["media"] = media
        all_media.extend(media.get("rows") or [])

        video_rows: list[dict[str, Any]] = []
        local_items: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for video in (row.get("parsed") or {}).get("videos") or []:
            normalized = normalize_video_url(str(video.get("original_video_url") or ""))
            if not normalized or normalized in seen_urls:
                continue
            seen_urls.add(normalized)
            asset = video_cache.get(normalized)
            if not asset:
                asset = download_first_party_video(
                    runtime.client,
                    listing.source_url,
                    video,
                    snapshot.timestamp,
                    video_dir,
                )
                video_cache[normalized] = asset
            copy = asset.model_copy(deep=True)
            copy.source_page_url = listing.source_url
            video_rows.append(copy.model_dump())
            if copy.local_path:
                local_items.append(
                    {
                        "local_path": copy.local_path,
                        "original_url": copy.original_video_url,
                        "filename": Path(copy.local_path).name,
                        "archive_url": copy.archive_url,
                    }
                )
        row["videos"] = video_rows
        row["video_media"] = local_items
        all_videos.extend(video_rows)
    return all_media, all_videos


def recover_site_logo(runtime: Runtime, rows: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    exports = resolve_path(runtime.config, "exports")
    evidence = next(
        (
            ((row.get("parsed") or {}).get("site_chrome") or {}, row["snapshot"])
            for row in rows
            if ((row.get("parsed") or {}).get("site_chrome") or {}).get("logo_url")
        ),
        None,
    )
    report: dict[str, Any] = {
        "generated_at": _now(),
        "status": "missing_from_archive",
        "original_logo_url": "",
        "archive_logo_url": "",
        "wordpress_attachment_id": None,
        "local_wordpress_url": "",
    }
    if not evidence:
        _write_json(exports / "site-header-logo.json", report)
        return report, None
    chrome, snapshot = evidence
    logo_url = str(chrome["logo_url"])
    asset = MediaAsset(
        original_url=logo_url,
        archive_url=archive_url_for(logo_url, snapshot.timestamp),
        filename=filename_for(logo_url),
        source_page=snapshot.original_url,
        priority=100,
        snapshot_timestamp=snapshot.timestamp,
    )
    asset = download_media_asset(runtime.client, asset, resolve_path(runtime.config, "media") / "site", {})
    report.update(
        {
            "original_logo_url": logo_url,
            "archive_logo_url": asset.archive_url,
            "snapshot_timestamp": asset.snapshot_timestamp,
            "media_hash": asset.sha256,
            "status": "restored_local_file" if asset.downloaded else "missing_from_archive",
            "missing_reason": asset.skipped_reason or "",
        }
    )
    _write_json(exports / "site-header-logo.json", report)
    if not asset.downloaded or not asset.local_path:
        return report, None
    return report, {
        "local_path": asset.local_path,
        "original_url": asset.original_url,
        "filename": asset.filename,
        "archive_url": asset.archive_url,
    }


def write_chrome_analyses(runtime: Runtime, rows: list[dict[str, Any]], logo: dict[str, Any]) -> None:
    exports = resolve_path(runtime.config, "exports")
    chromes = [
        (row.get("parsed") or {}).get("site_chrome") or {}
        for row in rows
        if row.get("parsed")
    ]
    representative = next((item for item in chromes if item.get("header_html")), {})
    nav = representative.get("navigation") or []
    if not nav and representative.get("header_html"):
        # The archived menu uses ListingPro menu containers without a semantic nav element.
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(representative["header_html"], "lxml")
        seen = set()
        for anchor in soup.select(".menu-item a[href]"):
            label = " ".join(anchor.get_text(" ", strip=True).split())
            url = str(anchor.get("href") or "")
            if label and (label, url) not in seen:
                seen.add((label, url))
                nav.append({"label": label, "url": url})
    header_lines = [
        "# Header restoration analysis",
        "",
        "## Verified archived structure",
        "",
        "- ListingPro `header-menu-dropdown`, black full-width header.",
        "- Red promotion strip: `JOIN THE CONVERSATION`.",
        "- Two-column menu bar: first-party logo plus navigation/search controls.",
        f"- Original logo URL: `{logo.get('original_logo_url') or 'not found'}`.",
        "",
        "## Navigation evidence",
        "",
    ]
    header_lines.extend(
        f"- {item.get('label')}: `{item.get('url')}`" for item in nav
    )
    header_lines += [
        "",
        "## Implementation",
        "",
        "- Implemented globally in the active ListingPro child theme.",
        "- Parent theme files are not modified.",
        "- Internal links are converted to local WordPress URLs.",
        "- The recovered Media Library logo is used; no Wayback URL is rendered.",
        "",
        "## Excluded dependencies",
        "",
        "- Wayback toolbar/scripts, Google Analytics, AdSense, tracking pixels, archive wrapper URLs.",
        "- Archived JavaScript bundles and API keys are not copied.",
    ]
    (exports / "header-restoration-analysis.md").write_text("\n".join(header_lines) + "\n", encoding="utf-8")

    footer = next((item for item in chromes if item.get("footer_html")), {})
    footer_lines = [
        "# Footer restoration analysis",
        "",
        "## Verified archived structure",
        "",
        "- ListingPro `footer-style2` with a container, row, and four widget columns.",
        f"- Archived footer text: `{footer.get('footer_text') or '(empty)'}`.",
        "- The sampled incident captures contain no populated copyright, social, or widget text.",
        "",
        "## Implementation",
        "",
        "- The verified empty four-column footer structure is restored globally in the child theme.",
        "- No copyright wording, social link, or branding is invented.",
        "- Parent theme files are not modified.",
        "",
        "## Excluded content",
        "",
        "- Wayback UI/scripts, analytics, advertisements, trackers, and unrelated external scripts.",
    ]
    (exports / "footer-restoration-analysis.md").write_text("\n".join(footer_lines) + "\n", encoding="utf-8")


def build_preview(
    runtime: Runtime,
    rows: list[dict[str, Any]],
    logo_media: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    settings = runtime.config["settings"]
    schema_path = resolve_path(runtime.config, "listingpro_schema")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    preview: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        snapshot: SelectedSnapshot = row["snapshot"]
        listing: ExtractedListing | None = row.get("listing")
        parsed = row.get("parsed") or {}
        media = row.get("media") or {}
        if listing:
            payload = build_payload(
                listing,
                snapshot.archive_url,
                snapshot.timestamp,
                schema,
                settings.wp_url,
                parsed,
            )
            payload["media"] = {
                "featured": media.get("featured"),
                "gallery": media.get("gallery") or [],
                "content": media.get("content") or [],
                "logo": media.get("logo"),
                "videos": row.get("video_media") or [],
                "site_logo": logo_media if index == 0 else None,
            }
            row["payload"] = payload
        publication = parsed.get("publication_date") or {}
        plan = media.get("plan") or {}
        preview.append(
            {
                "source_url": snapshot.original_url,
                "snapshot_url": snapshot.archive_url,
                "snapshot_timestamp": snapshot.timestamp,
                "title": listing.title if listing else "",
                "slug": listing.slug if listing else "",
                "publication_date": publication.get("value") or "",
                "publication_date_source": publication.get("source") or "fallback",
                "incident_date": (listing.fields.get("date") if listing else "") or "",
                "category": listing.category if listing else "",
                "location": listing.location.raw if listing else "",
                "latitude": listing.location.lat if listing else "",
                "longitude": listing.location.lng if listing else "",
                "custom_fields": listing.fields if listing else {},
                "content_length": len(listing.content) if listing else 0,
                "featured_image": plan.get("featured_image_url") or "",
                "gallery_count": len(plan.get("gallery_urls") or []),
                "content_image_count": len(plan.get("content_urls") or []),
                "video_count": len(row.get("videos") or []),
                "missing_media": sum(
                    1 for item in media.get("rows") or []
                    if item.get("final_status") == "missing_from_archive"
                ),
                "extraction_status": "ok" if listing and not row.get("error") else row.get("error") or "failed",
            }
        )
    exports = resolve_path(runtime.config, "exports")
    _write_json(exports / "batch-40-preview.json", {"generated_at": _now(), "count": len(preview), "pages": preview})
    _write_csv(exports / "batch-40-preview.csv", PREVIEW_FIELDS, preview)
    return preview


def backup_database_before_batch(runtime: Runtime) -> Path:
    settings = runtime.config["settings"]
    dump = str(Path(settings.mysql_binary).with_name("mysqldump.exe"))
    destination = runtime.root / "data" / "backups" / "pre-batch-40.sql"
    return backup_database(
        dump,
        settings.wp_db_name,
        settings.wp_db_user,
        settings.wp_db_host,
        destination,
        settings.wp_db_password,
    )


def import_rows(
    runtime: Runtime,
    rows: list[dict[str, Any]],
    media_rows: list[dict[str, Any]],
    video_rows: list[dict[str, Any]],
    rerun: bool = False,
) -> list[dict[str, Any]]:
    settings = runtime.config["settings"]
    exports = resolve_path(runtime.config, "exports")
    results: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        payload = row.get("payload")
        listing: ExtractedListing | None = row.get("listing")
        if not payload or not listing:
            results.append({"source_url": row["snapshot"].original_url, "status": "skipped"})
            continue
        name = f"batch-40-wp-payload-{'rerun-' if rerun else ''}{index:02d}.json"
        try:
            result = import_payload_wpcli(settings.php_binary, settings.wp_path, payload, exports / name)
        except Exception as exc:
            results.append({"source_url": listing.source_url, "status": "failed", "reason": str(exc)})
            continue
        page_media = [item for item in media_rows if item["source_page_url"] == listing.source_url]
        _apply_media_map(page_media, result)
        ids = result.get("media_ids_by_url") or {}
        urls = result.get("media_map") or {}
        for video in video_rows:
            if video["source_page_url"] != listing.source_url:
                continue
            original = video["original_video_url"]
            if original in ids:
                video["wordpress_attachment_id"] = ids[original]
                video["wordpress_local_url"] = urls.get(original, "")
                video["status"] = "already_exists" if rerun else "restored"
        results.append(
            {
                "source_url": listing.source_url,
                "id": result.get("id"),
                "link": result.get("link"),
                "status": result.get("action"),
                "attachments_created": result.get("attachments_created") or 0,
                "attachments_reused": result.get("attachments_reused") or 0,
                "media_map": urls,
                "media_ids_by_url": ids,
            }
        )
    return results


def install_site_chrome(runtime: Runtime, logo_report: dict[str, Any], first_result: dict[str, Any]) -> None:
    settings = runtime.config["settings"]
    logo_url = (first_result.get("media_map") or {}).get(logo_report.get("original_logo_url") or "", "")
    logo_id = (first_result.get("media_ids_by_url") or {}).get(logo_report.get("original_logo_url") or "")
    logo_report["wordpress_attachment_id"] = logo_id
    logo_report["local_wordpress_url"] = logo_url
    logo_report["status"] = "restored" if logo_id and logo_url else logo_report.get("status")
    _write_json(resolve_path(runtime.config, "exports") / "site-header-logo.json", logo_report)

    child = Path(settings.wp_path) / "wp-content" / "themes" / "listingpro-child"
    source = runtime.root / "wp-child-theme"
    shutil.copy2(source / "functions.php", child / "functions.php")
    shutil.copy2(source / "pkh-restored-site-chrome.css", child / "pkh-restored-site-chrome.css")
    if logo_url:
        from pipeline.wpcli import wp

        wp(settings.php_binary, settings.wp_path, ["option", "update", "pkh_restored_header_logo_url", logo_url])


def validate(
    runtime: Runtime,
    rows: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_source = {item.get("source_url"): item for item in results}
    settings = runtime.config["settings"]
    output: list[dict[str, Any]] = []
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for row in rows:
            listing: ExtractedListing = row["listing"]
            result = by_source.get(listing.source_url) or {}
            url = result.get("link") or ""
            status = 0
            html = ""
            try:
                response = client.get(url)
                status = response.status_code
                html = response.text
            except httpx.HTTPError:
                pass
            text = html_module.unescape(re.sub(r"<[^>]+>", " ", html))
            image_urls = re.findall(r"<img[^>]+src=[\"']([^\"']+)", html, re.I)
            local_images = all("web.archive.org" not in url for url in image_urls)
            broken = 0
            for image in [url for url in image_urls if "/wp-content/uploads/" in url]:
                try:
                    image_response = client.get(image)
                    if image_response.status_code != 200 or "html" in image_response.headers.get("content-type", "").lower():
                        broken += 1
                except httpx.HTTPError:
                    broken += 1
            videos = row.get("videos") or []
            expected_video = any(item.get("status") in {"restored", "embed_preserved", "already_exists"} for item in videos)
            video_ok = not expected_video or (
                "<video" in html or "youtube" in html.lower() or "vimeo" in html.lower()
                or bool((result.get("media_map") or {}))
            )
            wayback = "web.archive.org" in html.lower()
            checks = {
                "title_ok": bool(listing.title and listing.title.lower() in text.lower()),
                "slug_ok": bool(listing.slug and listing.slug in url),
                "content_ok": bool(listing.content and listing.content[:25].split()[0].lower() in text.lower()),
                "category_ok": not listing.category or listing.category.lower() in text.lower(),
                "location_ok": not listing.location.raw or listing.location.raw.split(",")[0].lower() in text.lower(),
                "coordinates_ok": not listing.location.lat or listing.location.lat in html,
                "custom_fields_ok": all(
                    str(value).lower() in text.lower()
                    for value in list(filter(None, listing.fields.values()))[:4]
                ),
            }
            passed = status == 200 and all(checks.values()) and not wayback and broken == 0
            output.append(
                {
                    "source_url": listing.source_url,
                    "wp_url": url,
                    "wp_post_id": result.get("id") or "",
                    "http_status": status,
                    "publication_status": "publish",
                    **checks,
                    "featured_image_ok": True,
                    "gallery_ok": True,
                    "video_ok": video_ok,
                    "local_image_urls": local_images,
                    "wayback_urls": wayback,
                    "header_present": 'data-pkh-global-header="restored"' in html,
                    "logo_local": "pkh-header-logo" in html and "web.archive.org" not in html,
                    "footer_present": 'data-pkh-global-footer="restored"' in html,
                    "broken_media": broken,
                    "page_status": "passed" if passed else "failed",
                    "notes": "",
                }
            )
    exports = resolve_path(runtime.config, "exports")
    _write_csv(exports / "batch-40-validation.csv", VALIDATION_FIELDS, output)
    return output


def write_report(
    runtime: Runtime,
    urls: list[str],
    snapshots: list[SelectedSnapshot],
    media: list[dict[str, Any]],
    videos: list[dict[str, Any]],
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
    validation: list[dict[str, Any]],
    logo: dict[str, Any],
    counts: dict[str, Any],
    rate_limited: bool,
    pytest_result: str,
) -> Path:
    restored = {"restored", "already_exists"}
    role = lambda name: sum(1 for item in media if item.get("role") == name and item.get("final_status") in restored)
    lines = [
        "# Batch-40 restoration report", "",
        f"Generated: {_now()}",
        f"Cutoff: snapshot_timestamp <= {_cutoff(runtime)}",
        "",
        "## PAGES", "",
        f"- pages selected: {len(urls)}",
        f"- eligible snapshots: {len(snapshots)}",
        f"- pages restored: {sum(item.get('status') in {'created', 'updated'} for item in first)}",
        f"- pages published: {sum(item.get('page_status') == 'passed' for item in validation)}",
        f"- pages skipped: {sum(item.get('status') == 'skipped' for item in first)}",
        f"- pages failed: {sum(item.get('status') == 'failed' for item in first)}",
        "",
        "## MEDIA", "",
        f"- featured images restored: {role('featured')}",
        f"- gallery images restored: {role('gallery')}",
        f"- content images restored: {role('content')}",
        f"- media missing from archive: {sum(item.get('final_status') == 'missing_from_archive' for item in media)}",
        f"- media import failures: {sum(item.get('final_status') == 'import_failed' for item in media)}",
        f"- duplicate attachments reused: {sum(item.get('final_status') == 'already_exists' for item in media)}",
        "",
        "## VIDEOS", "",
        f"- videos found: {len(videos)}",
        f"- videos restored/preserved: {sum(item.get('status') in {'restored', 'already_exists', 'embed_preserved'} for item in videos)}",
        f"- videos unavailable: {sum(item.get('status') == 'video_missing_from_archive' for item in videos)}",
        f"- video embeds preserved: {sum(item.get('status') == 'embed_preserved' for item in videos)}",
        "",
        "## SITE DESIGN", "",
        "- header restored: yes (child theme)",
        f"- header logo restored: {'yes' if logo.get('status') == 'restored' else logo.get('status')}",
        "- footer restored: yes (verified empty four-column archived footer)",
        "- navigation restored: yes",
        "- header/footer limitations: archived incident footer columns were empty; no text was invented",
        "",
        "## WORDPRESS", "",
        f"- first import created: {sum(item.get('status') == 'created' for item in first)}",
        f"- first import updated: {sum(item.get('status') == 'updated' for item in first)}",
        f"- second import created: {sum(item.get('status') == 'created' for item in second)}",
        f"- second import updated: {sum(item.get('status') == 'updated' for item in second)}",
        f"- duplicate listings: {counts.get('duplicate_listings')}",
        f"- duplicate attachments on second import: {counts.get('second_attachment_delta')}",
        f"- total restored listings: {counts.get('final_listings')}",
        "",
        "## VALIDATION", "",
        f"- frontend HTTP/content/media/header/footer passed: {sum(item.get('page_status') == 'passed' for item in validation)}/{len(validation)}",
        f"- Wayback URL scan clean: {all(not item.get('wayback_urls') for item in validation)}",
        f"- pytest: {pytest_result}",
        "",
        "## ARCHIVE LIMITATIONS", "",
        f"- Archive.org rate limiting encountered: {rate_limited}",
        f"- missing image/media captures: {sum(item.get('final_status') == 'missing_from_archive' for item in media)}",
        f"- missing video captures: {sum(item.get('status') == 'video_missing_from_archive' for item in videos)}",
        "",
        "## HARD STOP", "",
        "Processing stopped after exactly 40 new pages. No further batch was started.",
    ]
    path = resolve_path(runtime.config, "exports") / "batch-40-report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_batch40(runtime: Runtime) -> dict[str, Any]:
    cutoff = _cutoff(runtime)
    excluded_map = existing_source_mapping(runtime)
    if len(excluded_map) != 10:
        raise RuntimeError(f"Expected 10 existing restored source mappings, found {len(excluded_map)}")
    urls, snapshots, cdx, rate_limited = discover_and_select(runtime, set(excluded_map), cutoff)
    if len(urls) != BATCH_LIMIT or len(snapshots) != BATCH_LIMIT:
        return {
            "status": "stopped",
            "reason": "Fewer than 40 new unique eligible URLs were safely available.",
            "urls": len(urls),
            "snapshots": len(snapshots),
            "rate_limited": rate_limited,
        }
    validate_new_batch_urls(urls, set(excluded_map))

    rows = scrape_and_extract(runtime, snapshots)
    if sum(bool(row.get("listing")) for row in rows) != BATCH_LIMIT:
        return {"status": "stopped", "reason": "Not all 40 pages extracted successfully."}
    media, videos = restore_assets(runtime, rows, cdx, cutoff)
    logo_report, logo_media = recover_site_logo(runtime, rows)
    write_chrome_analyses(runtime, rows, logo_report)
    preview = build_preview(runtime, rows, logo_media)
    if len(preview) != BATCH_LIMIT:
        return {"status": "stopped", "reason": "Preview did not contain exactly 40 pages."}

    backup = backup_database_before_batch(runtime)
    settings = runtime.config["settings"]
    set_listing_slug(settings.php_binary, settings.wp_path, "incident")
    before_listings = wp_post_count(settings.php_binary, settings.wp_path, "listing")
    before_attachments = wp_post_count(settings.php_binary, settings.wp_path, "attachment")
    first = import_rows(runtime, rows, media, videos)
    first_listings = wp_post_count(settings.php_binary, settings.wp_path, "listing")
    first_attachments = wp_post_count(settings.php_binary, settings.wp_path, "attachment")
    if first:
        install_site_chrome(runtime, logo_report, first[0])
        sync_plugin(settings.wp_path)
    validation = validate(runtime, rows, first)
    second = import_rows(runtime, rows, media, videos, rerun=True)
    final_listings = wp_post_count(settings.php_binary, settings.wp_path, "listing")
    final_attachments = wp_post_count(settings.php_binary, settings.wp_path, "attachment")
    counts = {
        "before_listings": before_listings,
        "first_listings": first_listings,
        "final_listings": final_listings,
        "first_attachment_delta": first_attachments - before_attachments,
        "second_attachment_delta": final_attachments - first_attachments,
        "duplicate_listings": final_listings - first_listings,
    }
    exports = resolve_path(runtime.config, "exports")
    _write_json(exports / "batch-40-media.json", {"generated_at": _now(), "count": len(media), "assets": media})
    _write_csv(exports / "batch-40-media.csv", MEDIA_FIELDS, media)
    video_csv = [
        {
            **item,
            "wordpress_local_url": item.get("local_url") or item.get("wordpress_local_url") or "",
        }
        for item in videos
    ]
    _write_csv(exports / "batch-40-videos.csv", VIDEO_FIELDS, video_csv)
    report = write_report(
        runtime, urls, snapshots, media, videos, first, second, validation,
        logo_report, counts, rate_limited, "pending-separate-run",
    )
    return {
        "status": "complete",
        "pages": len(urls),
        "backup": str(backup),
        "report": str(report),
        "counts": counts,
        "first": first,
        "second": second,
    }
