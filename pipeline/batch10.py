"""Controlled 10-page restoration test with first-party media. Hard-stops at 10."""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from extractor.ai_extractor import apply_ai_extraction
from extractor.listing_extractor import ExtractedListing, extract_listing, write_extracted
from pipeline.context import Runtime
from pipeline.wpcli import wp
from scraper.archive_client import ArchiveClientError, RateLimitedError
from scraper.cdx import (
    CDXClient,
    CDXRecord,
    discover_incident_records,
    write_cdx_jsonl,
)
from scraper.downloader import download_snapshot, load_html
from scraper.eligibility import DEFAULT_SNAPSHOT_CUTOFF, is_eligible_timestamp
from scraper.parser import parse_archived_page
from scraper.snapshots import SelectedSnapshot, select_snapshots_for_records, write_selected_snapshots
from scraper.urls import (
    build_wayback_url,
    host_variants,
    is_incident_url,
    normalize_url,
    rewrite_wayback_url,
    slug_from_url,
    strip_www,
    url_hash,
)
from utils.config import resolve_path
from wordpress.importer import build_payload
from wordpress.restore_media import download_listing_media
from wordpress.wpcli_importer import backup_database, import_payload_wpcli, json_from_output, set_listing_slug

LOGGER = logging.getLogger("pkh.batch10")
BATCH_LIMIT = 10
VARANASI_LISTING_ID = 12
INDEX_PATHS = (
    "/incident/",
    "/incident/page/2/",
    "/incident/page/3/",
    "/incident-category/murder/",
    "/incident-category/massacre/",
    "/listing-category/murder/",
)

MEDIA_CSV_FIELDS = [
    "source_page_url",
    "original_media_url",
    "archive_media_url",
    "media_type",
    "role",
    "snapshot_timestamp",
    "eligibility",
    "http_status",
    "download_status",
    "validation_status",
    "wordpress_attachment_id",
    "local_url",
    "media_hash",
    "final_status",
]

PREVIEW_CSV_FIELDS = [
    "source_url",
    "selected_snapshot",
    "snapshot_timestamp",
    "title",
    "slug",
    "category",
    "location",
    "latitude",
    "longitude",
    "custom_fields",
    "content_length",
    "featured_image",
    "gallery_count",
    "content_image_count",
    "media_available",
    "media_missing",
    "extraction_errors",
]

VALIDATION_CSV_FIELDS = [
    "source_url",
    "wp_url",
    "wp_post_id",
    "http_status",
    "title_ok",
    "slug_ok",
    "content_ok",
    "category_ok",
    "location_ok",
    "coordinates_ok",
    "custom_fields_ok",
    "featured_image_ok",
    "gallery_ok",
    "content_images_ok",
    "attachment_ok",
    "local_image_urls",
    "wayback_in_content",
    "broken_imported_image",
    "duplicate_listing",
    "page_status",
    "media_status",
    "notes",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cutoff(runtime: Runtime) -> str:
    return str(runtime.config.get("wayback", {}).get("snapshot_cutoff") or DEFAULT_SNAPSHOT_CUTOFF)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _extract_paths(runtime: Runtime, url: str) -> tuple[Path, Path]:
    parsed_dir = resolve_path(runtime.config, "parsed")
    stem = f"{slug_from_url(url)}_{url_hash(url)[:16]}"
    return parsed_dir / f"{stem}.json", parsed_dir / f"{stem}.parsed.json"


def extract_incident_urls_from_html(html: str, domain: str, path_prefix: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"""href=["']([^"']+)["']""", html, re.I):
        href = rewrite_wayback_url(match.group(1))
        if href.startswith("/") and not href.startswith("//"):
            href = f"https://{domain}{href}"
        try:
            normalized = strip_www(href)
        except ValueError:
            continue
        if not is_incident_url(normalized, path_prefix):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
    return urls


def wp_post_count(php_binary: str, wp_path: str, post_type: str) -> int:
    result = wp(
        php_binary,
        wp_path,
        ["post", "list", f"--post_type={post_type}", "--post_status=any", "--format=count"],
        check=False,
    )
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped.isdigit():
            return int(stripped)
    numbers = [int(token) for token in text.split() if token.isdigit()]
    return numbers[-1] if numbers else 0


def wp_eval_json(php_binary: str, wp_path: str, snippet: str) -> Any:
    snippet_path = Path(wp_path).parent.parent / "tmp-pkh-batch10.php" if False else None
    exports = Path(wp_path)
    snippet_path = exports / "wp-content" / "uploads" / "pkh-batch10-eval.php"
    snippet_path.parent.mkdir(parents=True, exist_ok=True)
    snippet_path.write_text("<?php\n" + snippet, encoding="utf-8")
    result = wp(php_binary, wp_path, ["eval-file", str(snippet_path)], check=False)
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    try:
        return json_from_output(combined)
    except ValueError:
        LOGGER.warning("WP eval did not return JSON: %s", combined[-500:])
        return {}


def _discover_from_indexes(
    runtime: Runtime,
    cdx: CDXClient,
    have: list[str],
    needed: int,
    cutoff: str,
) -> tuple[list[str], list[CDXRecord], bool]:
    domain = runtime.config["wayback"]["domain"]
    prefix = runtime.config["wayback"]["path_prefix"]
    known_ts = str(runtime.config["project"].get("known_test_timestamp") or "20260215104259")
    records: list[CDXRecord] = []
    rate_limited = False
    html_dir = resolve_path(runtime.config, "raw_html") / "indexes"
    html_dir.mkdir(parents=True, exist_ok=True)
    for path in INDEX_PATHS:
        if len(have) >= needed:
            break
        index_url = f"https://{domain}{path}"
        timestamp = known_ts if is_eligible_timestamp(known_ts, cutoff) else ""
        try:
            found, _resume = cdx.search(
                index_url,
                match_type="exact",
                to_ts=cutoff,
                limit=5,
                filters=["statuscode:200", "mimetype:text/html"],
            )
            eligible = [row for row in found if is_eligible_timestamp(row.timestamp, cutoff)]
            if eligible:
                timestamp = max(row.timestamp for row in eligible)
        except (ArchiveClientError, RateLimitedError) as exc:
            LOGGER.warning("Index CDX failed for %s: %s", index_url, exc)
            rate_limited = "429" in str(exc) or isinstance(exc, RateLimitedError)
        if not timestamp:
            continue
        archive_url = build_wayback_url(index_url, timestamp, raw=True)
        dest = html_dir / f"{slug_from_url(index_url) or 'index'}_{timestamp}.html"
        html = ""
        if dest.exists() and dest.stat().st_size > 0:
            html = dest.read_text(encoding="utf-8", errors="replace")
        else:
            try:
                response, body = runtime.client.get_bytes(archive_url)
                if response.status_code != 200 or not body:
                    response, body = runtime.client.get_bytes(build_wayback_url(index_url, timestamp))
                html = body.decode("utf-8", errors="replace")
                dest.write_text(html, encoding="utf-8")
            except (ArchiveClientError, RateLimitedError) as exc:
                LOGGER.warning("Index HTML download failed for %s: %s", index_url, exc)
                rate_limited = rate_limited or "429" in str(exc) or isinstance(exc, RateLimitedError)
                continue
        for url in extract_incident_urls_from_html(html, domain, prefix):
            if url not in have:
                have.append(url)
            if len(have) >= needed:
                break
    return have, records, rate_limited


def discover_batch_urls(runtime: Runtime, cutoff: str) -> dict[str, Any]:
    wayback = runtime.config["wayback"]
    project = runtime.config["project"]
    cdx = CDXClient(runtime.client, wayback["cdx_endpoint"])
    known = strip_www(project["known_test_url"])
    urls = [known]
    records: list[CDXRecord] = []
    rate_limited = False
    methods = ["known-varanasi"]
    try:
        records = discover_incident_records(
            cdx,
            domain=wayback["domain"],
            path_prefix=wayback["path_prefix"],
            limit=BATCH_LIMIT,
            page_size=int(wayback.get("page_size") or 100),
            known_test_url=project.get("known_test_url"),
            known_test_timestamp=project.get("known_test_timestamp"),
            availability_endpoint=wayback.get("availability_endpoint")
            or "https://archive.org/wayback/available",
            cutoff=cutoff,
        )
        for record in records:
            if strip_www(record.normalized_url) not in urls and is_incident_url(record.normalized_url, wayback["path_prefix"]):
                urls.append(strip_www(record.normalized_url))
        methods.append("cdx")
    except (ArchiveClientError, RateLimitedError) as exc:
        LOGGER.warning("CDX discovery limited: %s", exc)
        rate_limited = True
        methods.append("cdx-rate-limited")

    if len(urls) < BATCH_LIMIT:
        extra, _index_records, index_limited = _discover_from_indexes(
            runtime, cdx, list(urls), BATCH_LIMIT + 15, cutoff
        )
        rate_limited = rate_limited or index_limited
        methods.append("archived-index")
        from scraper.cdx import _snapshots_for_url

        for url in extra:
            if url in urls:
                continue
            snaps = _snapshots_for_url(
                cdx,
                url,
                wayback.get("availability_endpoint") or "https://archive.org/wayback/available",
                cutoff,
            )
            if not snaps:
                LOGGER.info("Skipping %s; no eligible snapshot", url)
                continue
            records.extend(snaps)
            urls.append(url)
            if len(urls) >= BATCH_LIMIT:
                break

    urls = urls[:BATCH_LIMIT]
    payload = {
        "generated_at": _now(),
        "cutoff": cutoff,
        "count": len(urls),
        "unique": len(set(urls)),
        "discovery_methods": methods,
        "rate_limited": rate_limited,
        "urls": urls,
        "note": (
            "Exactly 10 unique incident URLs are required before WordPress import."
            if len(urls) == BATCH_LIMIT
            else "Fewer than 10 unique URLs were discovered; Archive.org may be rate-limiting."
        ),
    }
    exports = resolve_path(runtime.config, "exports")
    _write_json(exports / "batch-10-urls.json", payload)
    if records:
        write_cdx_jsonl(records, resolve_path(runtime.config, "cdx_jsonl"))
        for record in records:
            row = runtime.store.upsert_url(record.normalized_url, url_hash(record.normalized_url), record.urlkey)
            runtime.store.upsert_snapshot(
                row.id,
                {
                    "timestamp": record.timestamp,
                    "status_code": record.status_code,
                    "mime_type": record.mimetype,
                    "digest": record.digest,
                    "length": record.length,
                    "archive_url": record.archive_url,
                },
            )
    return {"urls": urls, "records": records, "cdx": cdx, "rate_limited": rate_limited, "methods": methods}


def select_batch_snapshots(
    runtime: Runtime,
    urls: list[str],
    records: list[CDXRecord],
    cdx: CDXClient,
    cutoff: str,
) -> list[SelectedSnapshot]:
    wayback = runtime.config["wayback"]
    grouped: dict[str, list[CDXRecord]] = {url: [] for url in urls}
    for record in records:
        key = strip_www(record.normalized_url)
        if key in grouped:
            grouped[key].append(record)

    for url in urls:
        if grouped[url]:
            continue
        try:
            found: list[CDXRecord] = []
            for variant in host_variants(url):
                batch, _resume = cdx.search(
                    variant,
                    match_type="exact",
                    to_ts=cutoff,
                    limit=50,
                    filters=["mimetype:text/html"],
                )
                found.extend(batch)
                if found:
                    break
            grouped[url].extend([row for row in found if is_eligible_timestamp(row.timestamp, cutoff)])
        except (ArchiveClientError, RateLimitedError) as exc:
            LOGGER.warning("Exact snapshot CDX failed for %s: %s", url, exc)
        if not grouped[url] and url == normalize_url(runtime.config["project"]["known_test_url"]):
            ts = str(runtime.config["project"]["known_test_timestamp"])
            if is_eligible_timestamp(ts, cutoff):
                grouped[url].append(
                    CDXRecord(
                        timestamp=ts,
                        original=url,
                        mimetype="text/html",
                        statuscode="200",
                        archive_url=build_wayback_url(url, ts),
                    )
                )

    flat = [record for group in grouped.values() for record in group]
    selected = select_snapshots_for_records(flat, weights=runtime.config.get("scoring"), cutoff=cutoff)
    by_url = {item.original_url: item for item in selected}
    ordered: list[SelectedSnapshot] = []
    for url in urls:
        item = by_url.get(url)
        if item:
            ordered.append(item)
    rejected_post_feb = sum(item.rejected_after_cutoff for item in ordered)
    late = [
        item
        for item in ordered
        if item.timestamp and not is_eligible_timestamp(item.timestamp, cutoff)
    ]
    if late:
        raise RuntimeError(
            "Selected snapshot after cutoff: "
            + ", ".join(f"{item.original_url}@{item.timestamp}" for item in late)
        )

    export_rows = []
    for item in ordered:
        export_rows.append(
            {
                "source_url": item.original_url,
                "snapshot_timestamp": item.timestamp,
                "snapshot_url": item.archive_url,
                "http_status": item.status_code,
                "mime_type": item.mime_type,
                "selection_reason": item.selected_reason,
                "eligibility_status": item.eligibility_status,
                "rejected_after_cutoff": item.rejected_after_cutoff,
                "eligible_candidates": item.eligible_candidates,
            }
        )
    exports = resolve_path(runtime.config, "exports")
    _write_json(
        exports / "batch-10-snapshots.json",
        {
            "generated_at": _now(),
            "cutoff": cutoff,
            "count": len(export_rows),
            "rejected_post_february": rejected_post_feb,
            "snapshots": export_rows,
        },
    )
    write_selected_snapshots(ordered, resolve_path(runtime.config, "selected_snapshots"))
    return ordered


def scrape_and_extract(
    runtime: Runtime,
    snapshots: list[SelectedSnapshot],
) -> list[dict[str, Any]]:
    html_dir = resolve_path(runtime.config, "raw_html")
    required = list(runtime.config["restore"]["required_fields"])
    skip = list(runtime.config["wayback"].get("skip_url_substrings") or [])
    settings = runtime.config["settings"]
    rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        row: dict[str, Any] = {
            "snapshot": snapshot,
            "listing": None,
            "parsed": None,
            "error": "",
        }
        if snapshot.eligibility_status != "eligible" or not snapshot.timestamp:
            row["error"] = "no_eligible_snapshot"
            rows.append(row)
            continue
        try:
            download_snapshot(runtime.client, snapshot, html_dir)
            html, _meta = load_html(html_dir, snapshot.original_url)
            if not html:
                raise RuntimeError("HTML not downloaded")
            parsed = parse_archived_page(html, snapshot.original_url, runtime.config["wayback"]["domain"])
            listing = extract_listing(
                parsed,
                runtime.mapping,
                required,
                snapshot.timestamp,
                skip,
            )
            listing = apply_ai_extraction(
                listing,
                html,
                required,
                enabled=bool(settings.ai_extraction_enabled),
                api_key=settings.openai_api_key,
                model=settings.openai_model,
            )
            extract_path, parsed_path = _extract_paths(runtime, snapshot.original_url)
            write_extracted(listing, extract_path)
            parsed_path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
            row["listing"] = listing
            row["parsed"] = parsed
        except Exception as exc:
            LOGGER.exception("Extract failed for %s", snapshot.original_url)
            row["error"] = str(exc)
        rows.append(row)
    return rows


def restore_media_for_rows(
    runtime: Runtime,
    rows: list[dict[str, Any]],
    cdx: CDXClient,
    cutoff: str,
) -> list[dict[str, Any]]:
    media_dir = resolve_path(runtime.config, "media")
    known_hashes: dict[str, str] = {}
    all_media: list[dict[str, Any]] = []
    for row in rows:
        listing: ExtractedListing | None = row.get("listing")
        snapshot: SelectedSnapshot = row["snapshot"]
        if listing is None:
            row["media"] = {"featured": None, "gallery": [], "content": [], "logo": None, "rows": [], "misses": []}
            continue
        media = download_listing_media(
            runtime.client,
            listing,
            row.get("parsed"),
            snapshot.timestamp,
            media_dir,
            cdx=cdx,
            cutoff=cutoff,
            known_hashes=known_hashes,
        )
        row["media"] = media
        all_media.extend(media.get("rows") or [])
    exports = resolve_path(runtime.config, "exports")
    _write_json(exports / "batch-10-media.json", {"generated_at": _now(), "count": len(all_media), "assets": all_media})
    _write_csv(exports / "batch-10-media.csv", MEDIA_CSV_FIELDS, all_media)
    return all_media


def build_preview(runtime: Runtime, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    schema_path = resolve_path(runtime.config, "listingpro_schema")
    schema = json.loads(schema_path.read_text(encoding="utf-8")) if schema_path.exists() else {}
    settings = runtime.config["settings"]
    preview: list[dict[str, Any]] = []
    for row in rows:
        snapshot: SelectedSnapshot = row["snapshot"]
        listing: ExtractedListing | None = row.get("listing")
        media = row.get("media") or {}
        plan = media.get("plan") or {}
        available = sum(1 for item in media.get("rows") or [] if item.get("final_status") in {"restored", "already_exists"})
        missing = sum(
            1
            for item in media.get("rows") or []
            if item.get("final_status") in {"missing_from_archive", "invalid_archive_response", "download_failed"}
        )
        payload = None
        if listing is not None:
            payload = build_payload(
                listing,
                snapshot.archive_url,
                snapshot.timestamp,
                schema,
                settings.wp_url,
                row.get("parsed"),
            )
            payload["media"] = {
                "featured": media.get("featured"),
                "gallery": media.get("gallery") or [],
                "content": media.get("content") or [],
                "logo": media.get("logo"),
            }
            row["payload"] = payload
        preview.append(
            {
                "source_url": snapshot.original_url,
                "selected_snapshot": snapshot.archive_url,
                "snapshot_timestamp": snapshot.timestamp,
                "title": listing.title if listing else "",
                "slug": listing.slug if listing else slug_from_url(snapshot.original_url),
                "category": listing.category if listing else "",
                "location": listing.location.raw if listing else "",
                "latitude": listing.location.lat if listing else "",
                "longitude": listing.location.lng if listing else "",
                "custom_fields": listing.fields if listing else {},
                "content_length": len(listing.content) if listing else 0,
                "featured_image": (plan.get("featured_image_url") or (media.get("featured") or {}).get("original_url") or ""),
                "gallery_count": len(plan.get("gallery_urls") or media.get("gallery") or []),
                "content_image_count": len(plan.get("content_urls") or media.get("content") or []),
                "media_available": available,
                "media_missing": missing,
                "extraction_errors": row.get("error") or "",
                "eligibility_status": snapshot.eligibility_status,
            }
        )
    exports = resolve_path(runtime.config, "exports")
    _write_json(exports / "batch-10-preview.json", {"generated_at": _now(), "count": len(preview), "pages": preview})
    _write_csv(exports / "batch-10-preview.csv", PREVIEW_CSV_FIELDS, preview)
    return preview


def backup_before_import(runtime: Runtime) -> Path:
    settings = runtime.config["settings"]
    dump = str(Path(settings.mysql_binary).with_name("mysqldump.exe"))
    if not Path(dump).exists():
        dump = str(Path(settings.mysql_binary).with_name("mysqldump"))
    destination = runtime.root / "data" / "backups" / "pre-batch-10.sql"
    return backup_database(
        dump,
        settings.wp_db_name,
        settings.wp_db_user,
        settings.wp_db_host,
        destination,
        settings.wp_db_password,
    )


def _apply_media_map(media_rows: list[dict[str, Any]], result: dict[str, Any]) -> None:
    mapping = result.get("media_map") or {}
    ids_by_url = result.get("media_ids_by_url") or {}
    created_count = int(result.get("attachments_created") or 0)
    reused_count = int(result.get("attachments_reused") or 0)
    for row in media_rows:
        original = row.get("original_media_url") or ""
        if original in ids_by_url:
            row["wordpress_attachment_id"] = ids_by_url[original]
        if original in mapping:
            row["local_url"] = mapping[original]
        if row.get("local_path") and original not in ids_by_url and row.get("final_status") in {"restored", "already_exists"}:
            row["final_status"] = "import_failed"
        elif original in ids_by_url:
            if reused_count and created_count == 0:
                row["final_status"] = "already_exists"
            elif row.get("final_status") not in {"already_exists"}:
                row["final_status"] = "restored"


def import_rows(runtime: Runtime, rows: list[dict[str, Any]], media_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    settings = runtime.config["settings"]
    set_listing_slug(settings.php_binary, settings.wp_path, "incident")
    exports = resolve_path(runtime.config, "exports")
    results: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        payload = row.get("payload")
        snapshot: SelectedSnapshot = row["snapshot"]
        listing: ExtractedListing | None = row.get("listing")
        if not payload or listing is None:
            results.append(
                {
                    "source_url": snapshot.original_url,
                    "status": "skipped",
                    "reason": row.get("error") or "no payload",
                }
            )
            continue
        payload_path = exports / f"batch-10-wp-payload-{index:02d}.json"
        try:
            result = import_payload_wpcli(settings.php_binary, settings.wp_path, payload, payload_path)
            page_media = [item for item in media_rows if item.get("source_page_url") == listing.source_url]
            _apply_media_map(page_media, result)
            store_row = runtime.store.upsert_url(listing.source_url, url_hash(listing.source_url))
            runtime.store.record_import(
                store_row.id,
                {
                    "source_url": listing.source_url,
                    "snapshot_url": snapshot.archive_url,
                    "wp_post_id": result["id"],
                    "wp_url": result.get("link"),
                    "status": result.get("action") or "imported",
                    "imported_at": _now(),
                },
            )
            runtime.store.map_url(listing.source_url, result.get("link") or "", result["id"], "mapped")
            results.append(
                {
                    "source_url": listing.source_url,
                    "status": result.get("action") or "imported",
                    "id": result.get("id"),
                    "link": result.get("link"),
                    "attachments_created": result.get("attachments_created") or 0,
                    "attachments_reused": result.get("attachments_reused") or 0,
                    "featured_media": result.get("featured_media"),
                    "gallery_ids": result.get("gallery_ids") or [],
                    "media_map": result.get("media_map") or {},
                }
            )
        except Exception as exc:
            LOGGER.exception("Import failed for %s", snapshot.original_url)
            for item in media_rows:
                if item.get("source_page_url") == listing.source_url and item.get("final_status") in {
                    "restored",
                    "already_exists",
                }:
                    item["final_status"] = "import_failed"
            results.append({"source_url": snapshot.original_url, "status": "failed", "reason": str(exc)})
    return results


def _text(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")


def validate_imported(
    runtime: Runtime,
    rows: list[dict[str, Any]],
    import_results: list[dict[str, Any]],
    media_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    settings = runtime.config["settings"]
    by_url = {item.get("source_url"): item for item in import_results}
    validation: list[dict[str, Any]] = []
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        for row in rows:
            listing: ExtractedListing | None = row.get("listing")
            snapshot: SelectedSnapshot = row["snapshot"]
            imported = by_url.get(snapshot.original_url) or {}
            wp_url = imported.get("link") or ""
            notes: list[str] = []
            html = ""
            status = 0
            title = ""
            if wp_url:
                try:
                    response = client.get(wp_url)
                    status = response.status_code
                    html = response.text
                    match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
                    title = re.sub(r"\s+", " ", match.group(1)).strip() if match else ""
                except httpx.HTTPError as exc:
                    notes.append(f"fetch-failed:{exc}")
            text = _text(html)
            wayback = bool(re.search(r"web\.archive\.org", html, re.I))
            img_srcs = re.findall(r"<img[^>]+src=[\"']([^\"']+)[\"']", html, re.I)
            local_imgs = [src for src in img_srcs if "/wp-content/uploads/" in src]
            archive_imgs = [src for src in img_srcs if "web.archive.org" in src.lower()]
            broken = 0
            for src in local_imgs:
                abs_src = src if src.startswith("http") else settings.wp_url.rstrip("/") + "/" + src.lstrip("/")
                try:
                    img_resp = client.get(abs_src)
                    if img_resp.status_code != 200:
                        broken += 1
                    elif "html" in (img_resp.headers.get("content-type") or "").lower():
                        broken += 1
                except httpx.HTTPError:
                    broken += 1

            title_ok = bool(listing and listing.title and listing.title.lower() in (title or "").lower())
            slug_ok = bool(listing and listing.slug and listing.slug in (wp_url or ""))
            content_ok = bool(
                listing and listing.content and listing.content[:40].split(" ")[0].lower() in text.lower()
            ) if listing and listing.content else bool(listing)
            category_ok = bool(not listing or not listing.category or listing.category.lower() in text.lower())
            location_ok = bool(not listing or not listing.location.raw or listing.location.raw.split(",")[0].lower() in text.lower())
            coords_ok = True
            if listing and listing.location.lat:
                coords_ok = listing.location.lat in html or listing.location.lat in text
            fields_ok = True
            if listing:
                present = [value for value in listing.fields.values() if value]
                fields_ok = all(str(value).lower() in text.lower() for value in present[:4]) if present else True

            page_media = [item for item in media_rows if item.get("source_page_url") == snapshot.original_url]
            restored_media = [item for item in page_media if item.get("final_status") in {"restored", "already_exists"}]
            featured_ok = True
            if any(item.get("role") == "featured" for item in restored_media):
                featured_ok = 'rel="image_src"' in html or "wp-post-image" in html or bool(local_imgs)
            gallery_ok = True
            attachment_ok = all(item.get("wordpress_attachment_id") for item in restored_media) if restored_media else True
            duplicate = imported.get("id") == VARANASI_LISTING_ID and imported.get("status") == "created"
            page_status = "passed" if status == 200 and title_ok and slug_ok and not wayback else "failed"
            if imported.get("status") in {"skipped", "failed"}:
                page_status = imported.get("status") or "failed"
            validation.append(
                {
                    "source_url": snapshot.original_url,
                    "wp_url": wp_url,
                    "wp_post_id": imported.get("id") or "",
                    "http_status": status,
                    "title_ok": title_ok,
                    "slug_ok": slug_ok,
                    "content_ok": content_ok,
                    "category_ok": category_ok,
                    "location_ok": location_ok,
                    "coordinates_ok": coords_ok,
                    "custom_fields_ok": fields_ok,
                    "featured_image_ok": featured_ok,
                    "gallery_ok": gallery_ok,
                    "content_images_ok": not archive_imgs,
                    "attachment_ok": attachment_ok,
                    "local_image_urls": not archive_imgs,
                    "wayback_in_content": wayback,
                    "broken_imported_image": broken,
                    "duplicate_listing": duplicate,
                    "page_status": page_status,
                    "media_status": "ok" if broken == 0 and not archive_imgs else "issues",
                    "notes": "|".join(notes),
                }
            )
    exports = resolve_path(runtime.config, "exports")
    _write_csv(exports / "batch-10-validation.csv", VALIDATION_CSV_FIELDS, validation)
    _write_json(exports / "batch-10-validation.json", validation)
    return validation


def rerun_import(runtime: Runtime, rows: list[dict[str, Any]]) -> dict[str, Any]:
    settings = runtime.config["settings"]
    exports = resolve_path(runtime.config, "exports")
    created = 0
    updated = 0
    failed = 0
    attachments_created = 0
    for index, row in enumerate(rows, start=1):
        payload = row.get("payload")
        if not payload:
            continue
        payload_path = exports / f"batch-10-wp-payload-rerun-{index:02d}.json"
        try:
            result = import_payload_wpcli(settings.php_binary, settings.wp_path, payload, payload_path)
            if result.get("action") == "created":
                created += 1
            else:
                updated += 1
            attachments_created += int(result.get("attachments_created") or 0)
        except Exception:
            failed += 1
    return {
        "created": created,
        "updated": updated,
        "failed": failed,
        "attachments_created": attachments_created,
    }


def write_report(
    runtime: Runtime,
    *,
    urls: list[str],
    snapshots: list[SelectedSnapshot],
    rows: list[dict[str, Any]],
    media_rows: list[dict[str, Any]],
    import_results: list[dict[str, Any]],
    validation: list[dict[str, Any]],
    idempotency: dict[str, Any],
    backup_path: Path | None,
    rate_limited: bool,
    methods: list[str],
    pytest_result: str,
) -> Path:
    restored_pages = [item for item in import_results if item.get("status") in {"created", "updated", "imported"}]
    failed_pages = [item for item in import_results if item.get("status") == "failed"]
    skipped_pages = [item for item in import_results if item.get("status") == "skipped"]
    eligible = [item for item in snapshots if item.eligibility_status == "eligible"]
    rejected = sum(item.rejected_after_cutoff for item in snapshots)
    no_eligible = [item for item in snapshots if item.eligibility_status == "no_eligible_snapshot"]

    def _count(role: str, statuses: set[str]) -> int:
        return sum(1 for item in media_rows if item.get("role") == role and item.get("final_status") in statuses)

    restored_status = {"restored", "already_exists"}
    featured_restored = _count("featured", restored_status)
    gallery_restored = _count("gallery", restored_status)
    content_restored = _count("content", restored_status)
    logos_restored = _count("logo", restored_status)
    missing_archive = sum(1 for item in media_rows if item.get("final_status") == "missing_from_archive")
    download_fail = sum(1 for item in media_rows if item.get("final_status") == "download_failed")
    invalid = sum(1 for item in media_rows if item.get("final_status") == "invalid_archive_response")
    import_fail = sum(1 for item in media_rows if item.get("final_status") == "import_failed")
    already = sum(1 for item in media_rows if item.get("final_status") == "already_exists")
    varanasi = next((item for item in import_results if "varanasi" in (item.get("source_url") or "")), {})
    frontend_pass = all(item.get("page_status") == "passed" for item in validation) if validation else False
    parsing_errors = [row for row in rows if row.get("error") and row.get("error") != "no_eligible_snapshot"]

    lines = [
        "# Batch-10 restoration report",
        "",
        f"Generated: {_now()}",
        f"Cutoff: snapshot_timestamp <= {runtime.config.get('wayback', {}).get('snapshot_cutoff') or DEFAULT_SNAPSHOT_CUTOFF}",
        f"Discovery methods: {', '.join(methods)}",
        f"Archive.org rate limited: {rate_limited}",
        f"Backup: {backup_path}" if backup_path else "Backup: not created",
        "",
        "## Totals",
        "",
        f"- total pages tested: {len(urls)}",
        f"- pages restored: {len(restored_pages)}",
        f"- pages failed: {len(failed_pages)}",
        f"- pages skipped: {len(skipped_pages)}",
        f"- eligible snapshots: {len(eligible)}",
        f"- rejected post-February snapshots: {rejected}",
        f"- featured images restored: {featured_restored}",
        f"- gallery images restored: {gallery_restored}",
        f"- content images restored: {content_restored}",
        f"- logos restored: {logos_restored}",
        f"- media missing from archive: {missing_archive}",
        f"- media download failures: {download_fail}",
        f"- media import failures: {import_fail}",
        f"- invalid archive media responses: {invalid}",
        f"- duplicate attachments (reused): {already}",
        f"- duplicate listings: {idempotency.get('duplicate_listings_created', 0)}",
        f"- Varanasi update result: {varanasi.get('status')} id={varanasi.get('id')}",
        f"- frontend validation result: {'passed' if frontend_pass else 'see CSV'}",
        f"- pytest result: {pytest_result}",
        f"- first_import_listing_count: {idempotency.get('first_import_listing_count')}",
        f"- second_import_listing_count: {idempotency.get('second_import_listing_count')}",
        f"- new_attachments_first_import: {idempotency.get('new_attachments_first_import')}",
        f"- new_attachments_second_import: {idempotency.get('new_attachments_second_import')}",
        "",
        "## SUCCESSFULLY RESTORED",
        "",
    ]
    if restored_pages:
        for item in restored_pages:
            lines.append(f"- {item.get('source_url')} → {item.get('link')} ({item.get('status')} id={item.get('id')})")
    else:
        lines.append("- none")
    lines += ["", "## ARCHIVE LIMITATIONS", ""]
    if no_eligible:
        for item in no_eligible:
            lines.append(f"- no_eligible_snapshot: {item.original_url}")
    if missing_archive:
        lines.append(f"- {missing_archive} first-party media files were referenced but not available from Wayback at or before the cutoff.")
        for item in media_rows:
            if item.get("final_status") == "missing_from_archive":
                lines.append(f"  - {item.get('original_media_url')} ({item.get('role')})")
    if rate_limited:
        lines.append("- Archive.org rate limiting affected discovery and/or media CDX lookups. Existing retry/backoff was respected; limits were not bypassed.")
    if not no_eligible and not missing_archive and not rate_limited:
        lines.append("- none observed")
    lines += ["", "## IMPORT/PARSING ERRORS", ""]
    if parsing_errors or failed_pages or import_fail or invalid or download_fail:
        for row in parsing_errors:
            lines.append(f"- parse/extract: {row['snapshot'].original_url}: {row.get('error')}")
        for item in failed_pages:
            lines.append(f"- import: {item.get('source_url')}: {item.get('reason')}")
        if import_fail:
            lines.append(f"- {import_fail} media import failures (file was available locally but WordPress attachment creation failed).")
        if invalid:
            lines.append(f"- {invalid} archive responses were HTML/error payloads, not media.")
        if download_fail:
            lines.append(f"- {download_fail} media downloads failed for non-archive-miss reasons.")
    else:
        lines.append("- none")
    lines += ["", "## MEDIA RESULTS", ""]
    lines.append(f"- restored: {sum(1 for item in media_rows if item.get('final_status') == 'restored')}")
    lines.append(f"- already_exists: {already}")
    lines.append(f"- missing_from_archive: {missing_archive}")
    lines.append(f"- invalid_archive_response: {invalid}")
    lines.append(f"- download_failed: {download_fail}")
    lines.append(f"- import_failed: {import_fail}")
    lines += [
        "",
        "## HARD STOP",
        "",
        "Processing stopped after these 10 pages. No 50/100/3600 continuation was started.",
    ]
    path = resolve_path(runtime.config, "exports") / "batch-10-report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_batch10(runtime: Runtime) -> dict[str, Any]:
    cutoff = _cutoff(runtime)
    LOGGER.info("Batch-10 cutoff %s; hard stop after %s pages", cutoff, BATCH_LIMIT)
    discovered = discover_batch_urls(runtime, cutoff)
    urls: list[str] = discovered["urls"]
    if len(urls) != BATCH_LIMIT:
        report = {
            "status": "stopped",
            "reason": "Could not discover exactly 10 unique incident URLs without fabricating them.",
            "count": len(urls),
            "urls": urls,
            "rate_limited": discovered["rate_limited"],
        }
        write_report(
            runtime,
            urls=urls,
            snapshots=[],
            rows=[],
            media_rows=[],
            import_results=[],
            validation=[],
            idempotency={},
            backup_path=None,
            rate_limited=bool(discovered["rate_limited"]),
            methods=list(discovered["methods"]),
            pytest_result="not-run",
        )
        return report

    snapshots = select_batch_snapshots(runtime, urls, discovered["records"], discovered["cdx"], cutoff)
    eligible = [item for item in snapshots if item.eligibility_status == "eligible" and item.timestamp]
    if len(urls) != BATCH_LIMIT or len(eligible) != BATCH_LIMIT:
        write_report(
            runtime,
            urls=urls,
            snapshots=snapshots,
            rows=[],
            media_rows=[],
            import_results=[],
            validation=[],
            idempotency={},
            backup_path=None,
            rate_limited=bool(discovered["rate_limited"]),
            methods=list(discovered["methods"]),
            pytest_result="not-run",
        )
        return {
            "status": "stopped",
            "reason": "Need exactly 10 unique pages with eligible snapshots before WordPress import.",
            "url_count": len(urls),
            "eligible_snapshots": len(eligible),
            "urls": urls,
            "rate_limited": discovered["rate_limited"],
        }
    rows = scrape_and_extract(runtime, snapshots)
    media_rows = restore_media_for_rows(runtime, rows, discovered["cdx"], cutoff)
    build_preview(runtime, rows)

    settings = runtime.config["settings"]
    listings_before = wp_post_count(settings.php_binary, settings.wp_path, "listing")
    attachments_before = wp_post_count(settings.php_binary, settings.wp_path, "attachment")
    backup_path = backup_before_import(runtime)
    import_results = import_rows(runtime, rows, media_rows)
    listings_after_first = wp_post_count(settings.php_binary, settings.wp_path, "listing")
    attachments_after_first = wp_post_count(settings.php_binary, settings.wp_path, "attachment")
    validation = validate_imported(runtime, rows, import_results, media_rows)
    second = rerun_import(runtime, rows)
    listings_after_second = wp_post_count(settings.php_binary, settings.wp_path, "listing")
    attachments_after_second = wp_post_count(settings.php_binary, settings.wp_path, "attachment")
    idempotency = {
        "first_import_listing_count": listings_after_first,
        "second_import_listing_count": listings_after_second,
        "new_attachments_first_import": max(0, attachments_after_first - attachments_before),
        "new_attachments_second_import": max(0, attachments_after_second - attachments_after_first),
        "duplicate_listings_created": max(0, listings_after_second - listings_after_first),
        "listings_before": listings_before,
        "second_pass": second,
    }
    exports = resolve_path(runtime.config, "exports")
    _write_json(exports / "batch-10-media.json", {"generated_at": _now(), "count": len(media_rows), "assets": media_rows})
    _write_csv(exports / "batch-10-media.csv", MEDIA_CSV_FIELDS, media_rows)
    report_path = write_report(
        runtime,
        urls=urls,
        snapshots=snapshots,
        rows=rows,
        media_rows=media_rows,
        import_results=import_results,
        validation=validation,
        idempotency=idempotency,
        backup_path=backup_path,
        rate_limited=bool(discovered["rate_limited"]),
        methods=list(discovered["methods"]),
        pytest_result="pending-separate-run",
    )
    return {
        "urls": urls,
        "snapshots": len(snapshots),
        "imported": import_results,
        "media": media_rows,
        "idempotency": idempotency,
        "report": str(report_path),
        "backup": str(backup_path),
    }
