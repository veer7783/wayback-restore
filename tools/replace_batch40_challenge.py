"""Replace a selected archive challenge page with the next valid eligible listing."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from pipeline.batch10 import _write_csv, _write_json, wp_eval_json
from pipeline.batch40 import (
    MEDIA_FIELDS,
    PREVIEW_FIELDS,
    VALIDATION_FIELDS,
    VIDEO_FIELDS,
    _cutoff,
    _now,
    existing_source_mapping,
    restore_assets,
    validate,
)
from pipeline.context import build_runtime
from scraper.cdx import CDXClient
from scraper.snapshots import select_best_snapshot
from scraper.urls import host_variants, is_incident_url, strip_www
from wordpress.importer import build_payload
from wordpress.wpcli_importer import import_payload_wpcli
from pipeline.batch10 import scrape_and_extract
from utils.config import resolve_path

BAD_SOURCE = (
    "https://projecthindukush.com/incident/"
    "15-year-old-nandlal-meghwars-throat-slit-and-corpse-hung-to-a-tree-in-pakistan"
)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    runtime = build_runtime()
    cutoff = _cutoff(runtime)
    exports = resolve_path(runtime.config, "exports")
    mapping = existing_source_mapping(runtime)
    bad_id = mapping.get(BAD_SOURCE)
    if not bad_id:
        raise RuntimeError("Challenge-page source mapping was not found")

    cdx = CDXClient(runtime.client, runtime.config["wayback"]["cdx_endpoint"])
    records, _resume = cdx.search(
        runtime.config["wayback"]["domain"] + runtime.config["wayback"]["path_prefix"],
        match_type="prefix",
        collapse="urlkey",
        limit=1000,
        filters=["statuscode:200", "mimetype:text/html"],
        to_ts=cutoff,
    )
    candidates: list[str] = []
    excluded = set(mapping)
    for record in records:
        url = strip_www(record.normalized_url)
        if is_incident_url(url, runtime.config["wayback"]["path_prefix"]) and url not in excluded and url not in candidates:
            candidates.append(url)

    replacement_row = None
    replacement_snapshot = None
    for url in candidates:
        exact = []
        for variant in host_variants(url):
            found, _resume = cdx.search(
                variant,
                match_type="exact",
                limit=50,
                filters=["mimetype:text/html"],
                to_ts=cutoff,
            )
            exact.extend(found)
        snapshot = select_best_snapshot(exact, weights=runtime.config.get("scoring"), cutoff=cutoff)
        if not snapshot or snapshot.eligibility_status != "eligible":
            continue
        snapshot.original_url = url
        row = scrape_and_extract(runtime, [snapshot])[0]
        listing = row.get("listing")
        if (
            listing
            and len(listing.content or "") > 80
            and "one moment" not in (listing.title or "").lower()
            and "attention required" not in (listing.title or "").lower()
        ):
            replacement_row = row
            replacement_snapshot = snapshot
            break
    if not replacement_row or not replacement_snapshot:
        raise RuntimeError("No valid replacement page was available")

    media, videos = restore_assets(runtime, [replacement_row], cdx, cutoff)
    listing = replacement_row["listing"]
    parsed = replacement_row["parsed"]
    schema = json.loads(resolve_path(runtime.config, "listingpro_schema").read_text(encoding="utf-8"))
    payload = build_payload(
        listing,
        replacement_snapshot.archive_url,
        replacement_snapshot.timestamp,
        schema,
        runtime.config["settings"].wp_url,
        parsed,
    )
    page_media = replacement_row["media"]
    payload["media"] = {
        "featured": page_media.get("featured"),
        "gallery": page_media.get("gallery") or [],
        "content": page_media.get("content") or [],
        "logo": page_media.get("logo"),
        "videos": replacement_row.get("video_media") or [],
        "site_logo": None,
    }
    replacement_row["payload"] = payload

    # Remove only the invalid listing and attachments parented to it.
    wp_eval_json(
        runtime.config["settings"].php_binary,
        runtime.config["settings"].wp_path,
        f"""
$children = get_children(array('post_parent'=>{int(bad_id)},'post_type'=>'attachment','numberposts'=>-1));
foreach ($children as $child) {{ wp_delete_attachment($child->ID, true); }}
$result = wp_delete_post({int(bad_id)}, true);
echo wp_json_encode(array('deleted'=>(bool)$result,'attachments'=>count($children)));
""",
    )
    result = import_payload_wpcli(
        runtime.config["settings"].php_binary,
        runtime.config["settings"].wp_path,
        payload,
        exports / "batch-40-wp-payload-replacement.json",
    )
    first_result = {
        "source_url": listing.source_url,
        "id": result.get("id"),
        "link": result.get("link"),
        "status": result.get("action"),
        "media_map": result.get("media_map") or {},
        "media_ids_by_url": result.get("media_ids_by_url") or {},
    }
    rerun = import_payload_wpcli(
        runtime.config["settings"].php_binary,
        runtime.config["settings"].wp_path,
        payload,
        exports / "batch-40-wp-payload-replacement-rerun.json",
    )
    if result.get("action") != "created" or rerun.get("action") != "updated":
        raise RuntimeError(f"Replacement idempotency failed: {result.get('action')}/{rerun.get('action')}")

    old_validation = [
        row for row in read_csv(exports / "batch-40-validation.csv")
        if strip_www(row["source_url"]) != BAD_SOURCE
    ]
    new_validation = validate(runtime, [replacement_row], [first_result])
    validation = old_validation + new_validation
    _write_csv(exports / "batch-40-validation.csv", VALIDATION_FIELDS, validation)

    urls_doc = json.loads((exports / "batch-40-urls.json").read_text(encoding="utf-8"))
    urls_doc["urls"] = [url for url in urls_doc["urls"] if strip_www(url) != BAD_SOURCE] + [listing.source_url]
    urls_doc["count"] = len(urls_doc["urls"])
    urls_doc["unique"] = len(set(map(strip_www, urls_doc["urls"])))
    urls_doc["replacement"] = {"removed": BAD_SOURCE, "added": listing.source_url, "reason": "archived challenge page"}
    _write_json(exports / "batch-40-urls.json", urls_doc)

    snapshots_doc = json.loads((exports / "batch-40-snapshots.json").read_text(encoding="utf-8"))
    snapshots_doc["snapshots"] = [
        row for row in snapshots_doc["snapshots"] if strip_www(row["source_url"]) != BAD_SOURCE
    ] + [{
        "source_url": listing.source_url,
        "snapshot_timestamp": replacement_snapshot.timestamp,
        "snapshot_url": replacement_snapshot.archive_url,
        "http_status": replacement_snapshot.status_code,
        "mime_type": replacement_snapshot.mime_type,
        "selection_reason": replacement_snapshot.selected_reason,
        "eligibility_status": replacement_snapshot.eligibility_status,
        "rejected_after_cutoff": replacement_snapshot.rejected_after_cutoff,
    }]
    snapshots_doc["count"] = len(snapshots_doc["snapshots"])
    _write_json(exports / "batch-40-snapshots.json", snapshots_doc)

    preview_doc = json.loads((exports / "batch-40-preview.json").read_text(encoding="utf-8"))
    preview_doc["pages"] = [
        row for row in preview_doc["pages"] if strip_www(row["source_url"]) != BAD_SOURCE
    ]
    publication = parsed.get("publication_date") or {}
    plan = page_media.get("plan") or {}
    preview_row = {
        "source_url": listing.source_url,
        "snapshot_url": replacement_snapshot.archive_url,
        "snapshot_timestamp": replacement_snapshot.timestamp,
        "title": listing.title,
        "slug": listing.slug,
        "publication_date": publication.get("value") or "",
        "publication_date_source": publication.get("source") or "fallback",
        "incident_date": listing.fields.get("date") or "",
        "category": listing.category or "",
        "location": listing.location.raw or "",
        "latitude": listing.location.lat or "",
        "longitude": listing.location.lng or "",
        "custom_fields": listing.fields,
        "content_length": len(listing.content or ""),
        "featured_image": plan.get("featured_image_url") or "",
        "gallery_count": len(plan.get("gallery_urls") or []),
        "content_image_count": len(plan.get("content_urls") or []),
        "video_count": len(videos),
        "missing_media": sum(item.get("final_status") == "missing_from_archive" for item in media),
        "extraction_status": "ok",
    }
    preview_doc["pages"].append(preview_row)
    preview_doc["count"] = len(preview_doc["pages"])
    _write_json(exports / "batch-40-preview.json", preview_doc)
    _write_csv(exports / "batch-40-preview.csv", PREVIEW_FIELDS, preview_doc["pages"])

    media_doc = json.loads((exports / "batch-40-media.json").read_text(encoding="utf-8"))
    merged_media = [
        row for row in media_doc["assets"] if strip_www(row["source_page_url"]) != BAD_SOURCE
    ] + media
    _write_json(exports / "batch-40-media.json", {"generated_at": _now(), "count": len(merged_media), "assets": merged_media})
    _write_csv(exports / "batch-40-media.csv", MEDIA_FIELDS, merged_media)

    old_videos = [
        row for row in read_csv(exports / "batch-40-videos.csv")
        if strip_www(row["source_page_url"]) != BAD_SOURCE
    ]
    _write_csv(exports / "batch-40-videos.csv", VIDEO_FIELDS, old_videos + videos)
    print(json.dumps({"removed": BAD_SOURCE, "added": listing.source_url, "id": result.get("id")}, indent=2))


if __name__ == "__main__":
    main()
