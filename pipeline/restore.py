"""Restore pipeline: scrape, extract, media, import, with dry-run support."""

from __future__ import annotations

import csv
import json
import logging
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from extractor.ai_extractor import apply_ai_extraction
from extractor.listing_extractor import ExtractedListing, extract_listing, write_extracted
from pipeline.context import Runtime
from pipeline.wpcli import wp
from scraper.archive_client import ArchiveClientError
from scraper.cdx import CDXClient, _snapshots_for_url, read_cdx_jsonl, write_cdx_jsonl
from scraper.downloader import download_snapshot, load_html
from scraper.eligibility import DEFAULT_SNAPSHOT_CUTOFF
from scraper.media import MediaAsset, download_media_asset
from scraper.parser import parse_archived_page
from scraper.snapshots import SelectedSnapshot, read_selected_snapshots, select_best_snapshot, write_selected_snapshots
from scraper.urls import is_incident_url, slug_from_url, strip_www, url_hash
from utils.config import resolve_path
from wordpress.importer import build_payload
from wordpress.listingpro import choose_post_type
from wordpress.listingpro_mapping import source_to_listingpro_mapping
from wordpress.restore_media import download_listing_media
from wordpress.wpcli_importer import (
    backup_database,
    import_payload_wpcli,
    json_from_output,
    set_listing_slug,
)

LOGGER = logging.getLogger("pkh.restore")


def is_placeholder_listing(title: str, content: str) -> bool:
    """True when the capture is a bot wall or has no incident text."""
    title_text = (title or "").strip().lower()
    if any(
        marker in title_text
        for marker in ("one moment", "attention required", "just a moment", "checking your browser")
    ):
        return True
    return len((content or "").strip()) < 80


def snapshot_has_listing(runtime: Runtime, snapshot: SelectedSnapshot) -> bool:
    html_dir = resolve_path(runtime.config, "raw_html")
    download_snapshot(runtime.client, snapshot, html_dir)
    html, _meta = load_html(html_dir, snapshot.original_url)
    if not html:
        return False
    parsed = parse_archived_page(html, snapshot.original_url, runtime.config["wayback"]["domain"])
    if is_placeholder_listing(str(parsed.get("title") or ""), str(parsed.get("content") or "")):
        return False
    return True


def next_unique_urls(candidates: list[str], excluded: set[str], limit: int) -> list[str]:
    """Return up to ``limit`` incident URLs that are not already imported."""
    chosen: list[str] = []
    seen: set[str] = set()
    blocked = {strip_www(url) for url in excluded}
    for raw in candidates:
        try:
            url = strip_www(raw)
        except ValueError:
            continue
        if url in blocked or url in seen:
            continue
        seen.add(url)
        chosen.append(url)
        if len(chosen) >= limit:
            break
    return chosen


def wordpress_imported_urls(runtime: Runtime) -> set[str]:
    """Source URLs already stored on listings. Stops if WordPress cannot be read."""
    settings = runtime.config["settings"]
    snippet = """
$posts = get_posts(array('post_type'=>'listing','post_status'=>'any','numberposts'=>-1));
$out = array();
foreach ($posts as $post) {
    $source = get_post_meta($post->ID, '_archive_source_url', true);
    if ($source) { $out[rtrim($source, '/')] = (int) $post->ID; }
}
echo wp_json_encode($out);
"""
    snippet_path = Path(settings.wp_path) / "wp-content" / "uploads" / "pkh-imported-urls.php"
    snippet_path.parent.mkdir(parents=True, exist_ok=True)
    snippet_path.write_text("<?php\n" + snippet, encoding="utf-8")
    result = wp(settings.php_binary, settings.wp_path, ["eval-file", str(snippet_path)], check=False)
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(f"Could not read existing WordPress listings: {combined[-500:]}")
    try:
        data = json_from_output(combined)
    except ValueError as exc:
        raise RuntimeError("Could not read existing WordPress listings.") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Could not read existing WordPress listings.")
    return {strip_www(str(url)) for url in data}


def prepare_new_imports(runtime: Runtime, limit: int) -> dict[str, Any]:
    """Choose ``limit`` incident pages that are not already WordPress listings."""
    if limit < 1:
        raise ValueError("Post count must be at least 1")
    excluded = wordpress_imported_urls(runtime)
    wayback = runtime.config["wayback"]
    cutoff = str(wayback.get("snapshot_cutoff") or DEFAULT_SNAPSHOT_CUTOFF)
    prefix = f"{wayback['domain']}{wayback['path_prefix']}"
    cdx = CDXClient(runtime.client, wayback["cdx_endpoint"])
    availability = wayback.get("availability_endpoint") or "https://archive.org/wayback/available"
    selected: list[SelectedSnapshot] = []
    seen: set[str] = set()
    records = []
    resume: str | None = None
    pages = 0
    while len(selected) < limit and pages < 8:
        try:
            collapsed, resume = cdx.search(
                prefix,
                match_type="prefix",
                collapse="urlkey",
                limit=400,
                filters=["statuscode:200", "mimetype:text/html"],
                to_ts=cutoff,
                resume_key=resume,
            )
        except ArchiveClientError as exc:
            LOGGER.warning("CDX discovery stopped: %s", exc)
            break
        pages += 1
        for record in collapsed:
            try:
                url = strip_www(record.normalized_url)
            except ValueError:
                continue
            if not is_incident_url(url, wayback["path_prefix"]):
                continue
            if url in excluded or url in seen:
                continue
            seen.add(url)
            try:
                snapshots = _snapshots_for_url(cdx, url, availability, cutoff)
            except ArchiveClientError as exc:
                LOGGER.warning("Snapshot lookup failed for %s: %s", url, exc)
                continue
            choice = select_best_snapshot(
                snapshots,
                weights=runtime.config.get("scoring"),
                cutoff=cutoff,
            )
            if not choice or not choice.timestamp or choice.eligibility_status != "eligible":
                LOGGER.info("No eligible snapshot for %s", url)
                continue
            if not snapshot_has_listing(runtime, choice):
                LOGGER.info("Skipping archive page with no listing text: %s", url)
                continue
            selected.append(choice)
            records.extend(snapshots)
            LOGGER.info("Selected new incident %s", url)
            if len(selected) >= limit:
                break
        if not resume:
            break

    write_selected_snapshots(selected, resolve_path(runtime.config, "selected_snapshots"))
    if records:
        write_cdx_jsonl(records, resolve_path(runtime.config, "cdx_jsonl"))
    LOGGER.info(
        "Already in WordPress: %s | New pages selected: %s | Requested: %s",
        len(excluded),
        len(selected),
        limit,
    )
    return {"already": len(excluded), "snapshots": selected}


def _selected(runtime: Runtime, limit: int) -> list[SelectedSnapshot]:
    selected = read_selected_snapshots(resolve_path(runtime.config, "selected_snapshots"))
    if not selected:
        records = read_cdx_jsonl(resolve_path(runtime.config, "cdx_jsonl"))
        from scraper.snapshots import select_snapshots_for_records

        selected = select_snapshots_for_records(records, weights=runtime.config.get("scoring"))
    return selected[:limit]


def run_scrape(runtime: Runtime, limit: int) -> int:
    html_dir = resolve_path(runtime.config, "raw_html")
    count = 0
    for snapshot in _selected(runtime, limit):
        try:
            meta = download_snapshot(runtime.client, snapshot, html_dir)
            row = runtime.store.upsert_url(snapshot.original_url, url_hash(snapshot.original_url))
            runtime.store.record_page(
                row.id,
                html_path=meta.html_path,
                http_status=meta.http_status,
                content_type=meta.content_type,
                downloaded_at=meta.downloaded_at,
                parse_status="downloaded",
            )
            count += 1
        except Exception as exc:
            _fail(runtime, snapshot.original_url, "scrape", exc)
    return count


def run_extract(runtime: Runtime, limit: int) -> int:
    html_dir = resolve_path(runtime.config, "raw_html")
    parsed_dir = resolve_path(runtime.config, "parsed")
    required = list(runtime.config["restore"]["required_fields"])
    skip = list(runtime.config["wayback"].get("skip_url_substrings") or [])
    settings = runtime.config["settings"]
    count = 0
    for snapshot in _selected(runtime, limit):
        try:
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
            out = parsed_dir / f"{slug_from_url(snapshot.original_url)}_{url_hash(snapshot.original_url)[:16]}.json"
            write_extracted(listing, out)
            (parsed_dir / f"{out.stem}.parsed.json").write_text(
                json.dumps(parsed, indent=2),
                encoding="utf-8",
            )
            row = runtime.store.upsert_url(snapshot.original_url, url_hash(snapshot.original_url))
            runtime.store.record_page(row.id, parsed_json_path=str(out), parse_status="parsed")
            count += 1
        except Exception as exc:
            _fail(runtime, snapshot.original_url, "extract", exc)
    return count


def run_media(runtime: Runtime, limit: int) -> int:
    parsed_dir = resolve_path(runtime.config, "parsed")
    media_dir = resolve_path(runtime.config, "media")
    known_hashes: dict[str, str] = {}
    count = 0
    for snapshot in _selected(runtime, limit):
        path = parsed_dir / f"{slug_from_url(snapshot.original_url)}_{url_hash(snapshot.original_url)[:16]}.json"
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        images = []
        for item in payload.get("images") or []:
            asset = MediaAsset.model_validate(item)
            try:
                asset = download_media_asset(runtime.client, asset, media_dir, known_hashes)
            except Exception as exc:
                _fail(runtime, snapshot.original_url, "media", exc)
                continue
            runtime.store.record_media(asset.model_dump())
            images.append(asset.model_dump())
            if asset.downloaded:
                count += 1
        payload["images"] = images
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return count


def _extract_paths(runtime: Runtime, snapshot: SelectedSnapshot) -> tuple[Path, Path]:
    parsed_dir = resolve_path(runtime.config, "parsed")
    stem = f"{slug_from_url(snapshot.original_url)}_{url_hash(snapshot.original_url)[:16]}"
    return parsed_dir / f"{stem}.json", parsed_dir / f"{stem}.parsed.json"


def run_restore(runtime: Runtime, limit: int, dry_run: bool) -> dict[str, Any]:
    schema_path = resolve_path(runtime.config, "listingpro_schema")
    schema = {"post_types": [], "taxonomies": [], "meta_fields": []}
    if schema_path.exists():
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

    preview_rows: list[dict[str, Any]] = []
    imported = 0
    settings = runtime.config["settings"]
    exports = resolve_path(runtime.config, "exports")
    exports.mkdir(parents=True, exist_ok=True)
    last_payload: dict[str, Any] | None = None
    last_result: dict[str, Any] | None = None

    if not dry_run:
        dump = str(Path(settings.mysql_binary).with_name("mysqldump.exe"))
        if not Path(dump).exists():
            dump = str(Path(settings.mysql_binary).with_name("mysqldump"))
        backup_path = runtime.root / "data" / "backups" / "pre-varanasi-import.sql"
        if not backup_path.exists() or backup_path.stat().st_size == 0:
            backup_database(
                dump,
                settings.wp_db_name,
                settings.wp_db_user,
                settings.wp_db_host,
                backup_path,
                settings.wp_db_password,
            )
        set_listing_slug(settings.php_binary, settings.wp_path, "incident")

    for snapshot in _selected(runtime, limit):
            path, parsed_path = _extract_paths(runtime, snapshot)
            row_out = {
                "url": snapshot.original_url,
                "snapshot": snapshot.archive_url,
                "title": "",
                "post_type": choose_post_type(schema, "listing", "post"),
                "category": "",
                "location": "",
                "field_count": 0,
                "image_count": 0,
                "content_length": 0,
                "status": "missing-extract",
            }
            if not path.exists():
                preview_rows.append(row_out)
                continue
            listing = ExtractedListing.model_validate(json.loads(path.read_text(encoding="utf-8")))
            if is_placeholder_listing(listing.title or "", listing.content or ""):
                row_out["status"] = "skipped-empty-archive"
                row_out["title"] = listing.title or ""
                LOGGER.info("Not importing empty archive page %s", snapshot.original_url)
                preview_rows.append(row_out)
                continue
            parsed = None
            if parsed_path.exists():
                parsed = json.loads(parsed_path.read_text(encoding="utf-8"))
            payload = build_payload(
                listing,
                snapshot.archive_url,
                snapshot.timestamp,
                schema,
                settings.wp_url,
                parsed,
            )
            mapping = source_to_listingpro_mapping(listing, parsed)
            (exports / "varanasi-listing-mapping.json").write_text(
                json.dumps(mapping, indent=2),
                encoding="utf-8",
            )
            (exports / "varanasi-import-preview.json").write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
            last_payload = payload
            row_out.update(
                {
                    "title": listing.title or "",
                    "category": listing.category or "",
                    "location": listing.location.raw or "",
                    "field_count": len([value for value in listing.fields.values() if value]),
                    "image_count": len(payload.get("gallery") or []),
                    "content_length": len(listing.content),
                    "status": "dry-run" if dry_run else "pending",
                }
            )
            if dry_run:
                preview_rows.append(row_out)
                continue
            try:
                media = download_listing_media(
                    runtime.client,
                    listing,
                    parsed,
                    snapshot.timestamp,
                    resolve_path(runtime.config, "media"),
                )
                payload["media"] = {
                    "featured": media.get("featured"),
                    "gallery": media.get("gallery") or [],
                    "content": media.get("content") or [],
                    "logo": media.get("logo"),
                }
                (exports / "varanasi-import-preview.json").write_text(
                    json.dumps(payload, indent=2),
                    encoding="utf-8",
                )
                result = import_payload_wpcli(
                    settings.php_binary,
                    settings.wp_path,
                    payload,
                    exports / "varanasi-wp-import-payload.json",
                )
                last_result = result
                row = runtime.store.upsert_url(snapshot.original_url, url_hash(snapshot.original_url))
                runtime.store.record_import(
                    row.id,
                    {
                        "source_url": listing.source_url,
                        "snapshot_url": snapshot.archive_url,
                        "wp_post_id": result["id"],
                        "wp_url": result.get("link"),
                        "status": result.get("action") or "imported",
                        "imported_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                runtime.store.map_url(listing.source_url, result.get("link") or "", result["id"], "mapped")
                row_out["status"] = result.get("action") or "imported"
                row_out["wp_url"] = result.get("link")
                row_out["wp_post_id"] = result.get("id")
                imported += 1 if result.get("action") == "created" else 0
            except Exception as exc:
                row_out["status"] = "failed"
                _fail(runtime, snapshot.original_url, "import", exc)
            preview_rows.append(row_out)

    _write_preview(runtime, preview_rows)
    return {
        "rows": preview_rows,
        "imported": imported,
        "payload": last_payload,
        "result": last_result,
    }


def _write_preview(runtime: Runtime, rows: list[dict[str, Any]]) -> None:
    json_path = resolve_path(runtime.config, "restore_preview_json")
    csv_path = resolve_path(runtime.config, "restore_preview_csv")
    json_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    fieldnames = [
        "url",
        "snapshot",
        "title",
        "post_type",
        "category",
        "location",
        "field_count",
        "image_count",
        "content_length",
        "status",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _fail(runtime: Runtime, url: str, stage: str, exc: Exception) -> None:
    LOGGER.exception("Failed %s during %s", url, stage)
    runtime.store.record_error(url, stage, str(exc), traceback.format_exc())
    runtime.store.append_jsonl(
        resolve_path(runtime.config, "errors_jsonl"),
        {
            "url": url,
            "stage": stage,
            "error": str(exc),
            "exception": traceback.format_exc(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
