"""Restore the archived home and verified static navigation pages."""

from __future__ import annotations

import csv
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup, Tag

from pipeline.batch10 import _write_csv, _write_json, wp_eval_json, wp_post_count
from pipeline.context import Runtime
from pipeline.wpcli import wp
from scraper.cdx import CDXClient, CDXRecord
from scraper.downloader import download_snapshot, load_html
from scraper.eligibility import DEFAULT_SNAPSHOT_CUTOFF, is_eligible_timestamp
from scraper.media import (
    MediaAsset,
    archive_url_for,
    classify_media_status,
    download_media_asset,
    filename_for,
    lookup_eligible_media_capture,
)
from scraper.snapshots import SelectedSnapshot, select_best_snapshot
from scraper.static_page import extract_static_page
from scraper.urls import host_variants, strip_www, url_hash
from scraper.video import download_first_party_video
from utils.config import resolve_path
from wordpress.wpcli_importer import backup_database, import_payload_wpcli

STATIC_PATHS = (
    "/new-home/",
    "/about-us/",
    "/contact/",
    "/resistance/",
    "/job/",
    "/report-hunduphobia/",
    "/tracker/",
    "/news/",
    "/coming-soon/",
)

PREVIEW_FIELDS = [
    "source_url", "snapshot_url", "snapshot_timestamp", "title", "slug",
    "publication_date", "publication_date_source", "content_length",
    "media_count", "video_count", "forms_removed", "extraction_status",
]
MEDIA_FIELDS = [
    "source_page_url", "original_media_url", "archive_media_url", "snapshot_timestamp",
    "http_status", "download_status", "validation_status", "wordpress_attachment_id",
    "local_url", "media_hash", "final_status",
]
VALIDATION_FIELDS = [
    "source_url", "wp_url", "wp_post_id", "http_status", "publish_status",
    "title_ok", "content_ok", "media_local", "wayback_urls", "header_present",
    "footer_present", "page_status", "notes",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cutoff(runtime: Runtime) -> str:
    return str(runtime.config["wayback"].get("snapshot_cutoff") or DEFAULT_SNAPSHOT_CUTOFF)


def select_pages(runtime: Runtime, cutoff: str) -> tuple[list[SelectedSnapshot], CDXClient]:
    cdx = CDXClient(runtime.client, runtime.config["wayback"]["cdx_endpoint"])
    selected: list[SelectedSnapshot] = []
    for path in STATIC_PATHS:
        source = f"https://projecthindukush.com{path}".rstrip("/")
        records: list[CDXRecord] = []
        for variant in host_variants(source):
            found, _resume = cdx.search(
                variant,
                match_type="exact",
                limit=50,
                filters=["mimetype:text/html"],
                to_ts=cutoff,
            )
            records.extend(found)
        choice = select_best_snapshot(records, weights=runtime.config.get("scoring"), cutoff=cutoff)
        if not choice or choice.eligibility_status != "eligible" or not choice.timestamp:
            continue
        if not is_eligible_timestamp(choice.timestamp, cutoff):
            raise RuntimeError(f"Post-cutoff static snapshot selected: {source}@{choice.timestamp}")
        choice.original_url = source
        selected.append(choice)
    exports = resolve_path(runtime.config, "exports")
    _write_json(
        exports / "static-pages-snapshots.json",
        {
            "generated_at": _now(),
            "cutoff": cutoff,
            "requested": len(STATIC_PATHS),
            "selected": len(selected),
            "pages": [
                {
                    "source_url": item.original_url,
                    "snapshot_url": item.archive_url,
                    "snapshot_timestamp": item.timestamp,
                    "eligibility_status": item.eligibility_status,
                    "selection_reason": item.selected_reason,
                }
                for item in selected
            ],
        },
    )
    return selected, cdx


def _slug(url: str) -> str:
    return urlsplit(url).path.strip("/").split("/")[-1] or "home"


def extract_pages(runtime: Runtime, snapshots: list[SelectedSnapshot]) -> list[dict[str, Any]]:
    html_dir = resolve_path(runtime.config, "raw_html") / "pages"
    rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        download_snapshot(runtime.client, snapshot, html_dir)
        html, _meta = load_html(html_dir, snapshot.original_url)
        page = extract_static_page(html, snapshot.original_url, _slug(snapshot.original_url))
        rows.append({"snapshot": snapshot, "page": page, "html": html})
        artifact = html_dir / f"{_slug(snapshot.original_url)}_{snapshot.timestamp}.extracted.json"
        artifact.write_text(json.dumps(page, indent=2), encoding="utf-8")
    return rows


def _retry_media_capture(
    runtime: Runtime,
    cdx: CDXClient,
    asset: MediaAsset,
    media_dir: Path,
    hashes: dict[str, str],
    cutoff: str,
) -> MediaAsset:
    if asset.downloaded:
        return asset
    records: list[CDXRecord] = []
    for variant in host_variants(asset.original_url):
        found, _resume = cdx.search(
            variant,
            match_type="exact",
            limit=30,
            filters=["statuscode:200"],
            to_ts=cutoff,
        )
        records.extend(found)
    best, eligibility = lookup_eligible_media_capture(records, cutoff)
    if not best:
        asset.eligibility = eligibility
        return asset
    retry = MediaAsset(
        original_url=asset.original_url,
        archive_url=archive_url_for(asset.original_url, best.timestamp),
        filename=asset.filename,
        source_page=asset.source_page,
        priority=asset.priority,
        snapshot_timestamp=best.timestamp,
        eligibility="eligible",
    )
    return download_media_asset(runtime.client, retry, media_dir, hashes)


def restore_page_media(
    runtime: Runtime,
    rows: list[dict[str, Any]],
    cdx: CDXClient,
    cutoff: str,
) -> list[dict[str, Any]]:
    media_dir = resolve_path(runtime.config, "media") / "pages"
    video_dir = media_dir / "videos"
    hashes: dict[str, str] = {}
    output: list[dict[str, Any]] = []
    for row in rows:
        page = row["page"]
        snapshot: SelectedSnapshot = row["snapshot"]
        local_items: list[dict[str, Any]] = []
        for original in page["media_urls"]:
            original = original.split("?", 1)[0]
            asset = MediaAsset(
                original_url=original,
                archive_url=archive_url_for(original, snapshot.timestamp),
                filename=filename_for(original),
                source_page=page["source_url"],
                priority=80,
                snapshot_timestamp=snapshot.timestamp,
                eligibility="eligible",
            )
            asset = download_media_asset(runtime.client, asset, media_dir, hashes)
            asset = _retry_media_capture(runtime, cdx, asset, media_dir, hashes, cutoff)
            status = classify_media_status(asset)
            if asset.downloaded and asset.local_path:
                local_items.append(
                    {
                        "local_path": asset.local_path,
                        "original_url": original,
                        "filename": asset.filename,
                        "archive_url": asset.archive_url,
                    }
                )
            output.append(
                {
                    "source_page_url": page["source_url"],
                    "original_media_url": original,
                    "archive_media_url": asset.archive_url,
                    "snapshot_timestamp": asset.snapshot_timestamp,
                    "http_status": asset.http_status,
                    "download_status": "downloaded" if asset.downloaded else asset.skipped_reason,
                    "validation_status": "valid" if asset.downloaded else "unavailable",
                    "wordpress_attachment_id": "",
                    "local_url": "",
                    "media_hash": asset.sha256,
                    "final_status": status,
                }
            )
        video_items: list[dict[str, Any]] = []
        for video in page.get("videos") or []:
            asset = download_first_party_video(
                runtime.client,
                page["source_url"],
                video,
                snapshot.timestamp,
                video_dir,
            )
            if asset.local_path:
                video_items.append(
                    {
                        "local_path": asset.local_path,
                        "original_url": asset.original_video_url,
                        "filename": Path(asset.local_path).name,
                        "archive_url": asset.archive_url,
                    }
                )
        row["local_media"] = local_items
        row["local_videos"] = video_items
    return output


def _existing_local_map(runtime: Runtime) -> dict[str, str]:
    settings = runtime.config["settings"]
    result = wp_eval_json(
        settings.php_binary,
        settings.wp_path,
        """
$posts = get_posts(array('post_type'=>array('listing','page'),'post_status'=>'any','numberposts'=>-1));
$out = array();
foreach ($posts as $post) {
    $source = get_post_meta($post->ID, '_archive_source_url', true);
    if ($source) { $out[rtrim($source, '/')] = get_permalink($post); }
}
echo wp_json_encode($out);
""",
    )
    return {strip_www(str(key)): str(value) for key, value in (result or {}).items()}


def _rewrite_mapped_links(content: str, mapping: dict[str, str]) -> str:
    soup = BeautifulSoup(content, "lxml")
    for anchor in soup.find_all("a", href=True):
        original = strip_www(str(anchor["href"]).split("#", 1)[0].rstrip("/"))
        if original in mapping:
            anchor["href"] = mapping[original]
    body = soup.body
    return "".join(str(child) for child in body.contents) if body else str(soup)


def build_preview_and_payloads(runtime: Runtime, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    settings = runtime.config["settings"]
    mapping = _existing_local_map(runtime)
    for row in rows:
        page = row["page"]
        mapping[strip_www(page["source_url"])] = (
            settings.wp_url.rstrip("/") + "/" + page["slug"].strip("/") + "/"
        )
    preview: list[dict[str, Any]] = []
    for row in rows:
        page = row["page"]
        snapshot: SelectedSnapshot = row["snapshot"]
        publication = page.get("publication_date") or {}
        content = _rewrite_mapped_links(page["content_html"], mapping)
        meta = {
            "_archive_source_url": page["source_url"],
            "_archive_snapshot_url": snapshot.archive_url,
            "_archive_timestamp": snapshot.timestamp,
            "_archive_imported_at": _now(),
            "_pkh_source_id": url_hash(page["source_url"]),
            "_archive_publication_date_source": publication.get("source") or "fallback",
            "_archive_video_urls": [
                item.get("original_video_url")
                for item in page.get("videos") or []
                if item.get("original_video_url")
            ],
            "_wp_page_template": "default",
        }
        payload = {
            "post_type": "page",
            "post_title": page["title"],
            "post_name": page["slug"],
            "post_status": "publish",
            "post_content": content,
            "post_date": publication.get("value"),
            "publication_date_source": publication.get("source") or "fallback",
            "taxonomies": {},
            "form_fields": [],
            "meta": meta,
            "archive_metadata": meta,
            "media": {
                "featured": None,
                "gallery": [],
                "content": row.get("local_media") or [],
                "videos": row.get("local_videos") or [],
                "logo": None,
                "site_logo": None,
            },
        }
        row["payload"] = payload
        preview.append(
            {
                "source_url": page["source_url"],
                "snapshot_url": snapshot.archive_url,
                "snapshot_timestamp": snapshot.timestamp,
                "title": page["title"],
                "slug": page["slug"],
                "publication_date": publication.get("value") or "",
                "publication_date_source": publication.get("source") or "fallback",
                "content_length": page["content_length"],
                "media_count": len(page["media_urls"]),
                "video_count": len(page.get("videos") or []),
                "forms_removed": page.get("forms_removed") or 0,
                "extraction_status": "ok" if page["content_length"] else "empty",
            }
        )
    exports = resolve_path(runtime.config, "exports")
    _write_json(exports / "static-pages-preview.json", {"generated_at": _now(), "count": len(preview), "pages": preview})
    _write_csv(exports / "static-pages-preview.csv", PREVIEW_FIELDS, preview)
    return preview


def backup_before_import(runtime: Runtime) -> Path:
    settings = runtime.config["settings"]
    dump = str(Path(settings.mysql_binary).with_name("mysqldump.exe"))
    destination = runtime.root / "data" / "backups" / "pre-static-pages.sql"
    if destination.exists() and destination.stat().st_size:
        return destination
    return backup_database(
        dump,
        settings.wp_db_name,
        settings.wp_db_user,
        settings.wp_db_host,
        destination,
        settings.wp_db_password,
    )


def import_pages(runtime: Runtime, rows: list[dict[str, Any]], rerun: bool = False) -> list[dict[str, Any]]:
    settings = runtime.config["settings"]
    exports = resolve_path(runtime.config, "exports")
    results: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        suffix = f"{'rerun-' if rerun else ''}{index:02d}"
        result = import_payload_wpcli(
            settings.php_binary,
            settings.wp_path,
            row["payload"],
            exports / f"static-page-payload-{suffix}.json",
        )
        results.append(
            {
                "source_url": row["page"]["source_url"],
                "id": result.get("id"),
                "link": result.get("link"),
                "action": result.get("action"),
                "attachments_created": result.get("attachments_created") or 0,
                "attachments_reused": result.get("attachments_reused") or 0,
                "media_map": result.get("media_map") or {},
                "media_ids_by_url": result.get("media_ids_by_url") or {},
            }
        )
    return results


def activate_home_and_navigation(runtime: Runtime, results: list[dict[str, Any]]) -> None:
    settings = runtime.config["settings"]
    home = next(item for item in results if item["source_url"].endswith("/new-home"))
    wp(settings.php_binary, settings.wp_path, ["option", "update", "show_on_front", "page"])
    wp(settings.php_binary, settings.wp_path, ["option", "update", "page_on_front", str(home["id"])])
    child = Path(settings.wp_path) / "wp-content" / "themes" / "listingpro-child"
    shutil.copy2(runtime.root / "wp-child-theme" / "functions.php", child / "functions.php")
    shutil.copy2(
        runtime.root / "wp-child-theme" / "pkh-restored-site-chrome.css",
        child / "pkh-restored-site-chrome.css",
    )


def apply_media_results(media_rows: list[dict[str, Any]], results: list[dict[str, Any]]) -> None:
    for result in results:
        source = result["source_url"]
        ids = result.get("media_ids_by_url") or {}
        urls = result.get("media_map") or {}
        for row in media_rows:
            if row["source_page_url"] != source:
                continue
            original = row["original_media_url"]
            if original in ids:
                row["wordpress_attachment_id"] = ids[original]
                row["local_url"] = urls.get(original, "")
                row["final_status"] = "restored"


def validate_pages(runtime: Runtime, rows: list[dict[str, Any]], results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_source = {item["source_url"]: item for item in results}
    output: list[dict[str, Any]] = []
    with httpx.Client(timeout=40, follow_redirects=True) as client:
        for row in rows:
            page = row["page"]
            result = by_source[page["source_url"]]
            response = client.get(result["link"])
            html = response.text
            text = re.sub(r"<[^>]+>", " ", html)
            images = re.findall(r"<img[^>]+src=[\"']([^\"']+)", html, re.I)
            imported_originals = {item["original_url"] for item in row.get("local_media") or []}
            local_ok = all(
                original not in html and "web.archive.org" not in html
                for original in imported_originals
            )
            title_ok = page["title"].split(" ")[0].lower() in text.lower()
            content_token = next((token for token in page["content_text"].split() if len(token) > 4), "")
            content_ok = not content_token or content_token.lower() in text.lower()
            wayback = "web.archive.org" in html.lower()
            passed = response.status_code == 200 and title_ok and content_ok and local_ok and not wayback
            output.append(
                {
                    "source_url": page["source_url"],
                    "wp_url": result["link"],
                    "wp_post_id": result["id"],
                    "http_status": response.status_code,
                    "publish_status": "publish",
                    "title_ok": title_ok,
                    "content_ok": content_ok,
                    "media_local": local_ok,
                    "wayback_urls": wayback,
                    "header_present": 'data-pkh-global-header="restored"' in html,
                    "footer_present": 'data-pkh-global-footer="restored"' in html,
                    "page_status": "passed" if passed else "failed",
                    "notes": f"rendered_images={len(images)}",
                }
            )
    exports = resolve_path(runtime.config, "exports")
    _write_csv(exports / "static-pages-validation.csv", VALIDATION_FIELDS, output)
    return output


def write_report(
    runtime: Runtime,
    rows: list[dict[str, Any]],
    media: list[dict[str, Any]],
    first: list[dict[str, Any]],
    second: list[dict[str, Any]],
    validation: list[dict[str, Any]],
    backup: Path,
) -> Path:
    lines = [
        "# Static page restoration report", "",
        f"Generated: {_now()}",
        f"Cutoff: snapshot_timestamp <= {_cutoff(runtime)}",
        f"Backup: {backup}", "",
        "## Pages", "",
        f"- valid archived pages selected: {len(rows)}",
        f"- first import created: {sum(item['action'] == 'created' for item in first)}",
        f"- first import updated: {sum(item['action'] == 'updated' for item in first)}",
        f"- second import created: {sum(item['action'] == 'created' for item in second)}",
        f"- second import updated: {sum(item['action'] == 'updated' for item in second)}",
        f"- frontend validation passed: {sum(item['page_status'] == 'passed' for item in validation)}/{len(validation)}",
        "", "## Media", "",
        f"- referenced first-party media: {len(media)}",
        f"- restored media: {sum(item['final_status'] == 'restored' for item in media)}",
        f"- missing from archive: {sum(item['final_status'] == 'missing_from_archive' for item in media)}",
        f"- import failures: {sum(item['final_status'] == 'import_failed' for item in media)}",
        f"- video/embed references preserved: {sum(len(row['page'].get('videos') or []) for row in rows)}",
        "", "## Safety and limitations", "",
        "- Dynamic forms and archived authentication/nonces were removed; no broken form handlers were copied.",
        "- Wayback toolbar/scripts, analytics, advertisements, and tracking scripts were excluded.",
        "- The archived tracker response itself is marked by Wayback as truncated to 1,048,576 bytes; available verified content was preserved.",
        "- Contact details are archived source values and were preserved without correction or invention.",
        "- `/contact-us/` had no eligible capture; the verified `/contact/` page was restored.",
        "- `new-home` is configured as the WordPress front page.",
    ]
    path = resolve_path(runtime.config, "exports") / "static-pages-report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_static_pages(runtime: Runtime) -> dict[str, Any]:
    cutoff = _cutoff(runtime)
    snapshots, cdx = select_pages(runtime, cutoff)
    rows = extract_pages(runtime, snapshots)
    rows = [
        row for row in rows
        if row["page"]["content_length"] > 0
        and "one moment" not in row["page"]["title"].lower()
    ]
    media = restore_page_media(runtime, rows, cdx, cutoff)
    preview = build_preview_and_payloads(runtime, rows)
    if len(preview) != len(rows):
        raise RuntimeError("Static page preview was incomplete")
    backup = backup_before_import(runtime)
    page_count_before = wp_post_count(
        runtime.config["settings"].php_binary,
        runtime.config["settings"].wp_path,
        "page",
    )
    first = import_pages(runtime, rows)
    activate_home_and_navigation(runtime, first)
    apply_media_results(media, first)
    validation = validate_pages(runtime, rows, first)
    second = import_pages(runtime, rows, rerun=True)
    page_count_after = wp_post_count(
        runtime.config["settings"].php_binary,
        runtime.config["settings"].wp_path,
        "page",
    )
    exports = resolve_path(runtime.config, "exports")
    _write_json(exports / "static-pages-media.json", {"generated_at": _now(), "count": len(media), "assets": media})
    _write_csv(exports / "static-pages-media.csv", MEDIA_FIELDS, media)
    report = write_report(runtime, rows, media, first, second, validation, backup)
    return {
        "status": "complete",
        "pages": len(rows),
        "page_count_before": page_count_before,
        "page_count_after": page_count_after,
        "first": first,
        "second": second,
        "validation_passed": sum(item["page_status"] == "passed" for item in validation),
        "backup": str(backup),
        "report": str(report),
    }
