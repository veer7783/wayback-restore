from scraper.media import (
    MediaAsset,
    classify_media_status,
    lookup_eligible_media_capture,
    validate_media_payload,
)
from scraper.urls import normalize_url, rewrite_wayback_text, rewrite_wayback_url
from wordpress.listingpro_mapping import prefer_original_upload


def test_media_url_normalization_strips_wayback_wrapper():
    archived = (
        "https://web.archive.org/web/20260215104259/"
        "http://example.com/wp-content/uploads/file.jpg"
    )
    original = rewrite_wayback_url(archived)
    assert original == "http://example.com/wp-content/uploads/file.jpg"
    assert "web.archive.org" not in original
    text = f'<img src="{archived}">'
    assert "web.archive.org" not in rewrite_wayback_text(text)


def test_wayback_media_url_https_and_raw_variants():
    raw = (
        "https://web.archive.org/web/20260228235959id_/"
        "https://projecthindukush.com/wp-content/uploads/a.png"
    )
    assert rewrite_wayback_url(raw) == "https://projecthindukush.com/wp-content/uploads/a.png"
    resized = "https://projecthindukush.com/wp-content/uploads/a-550x420.jpg"
    assert prefer_original_upload(resized).endswith("/a.jpg")


def test_invalid_html_response_is_rejected():
    html = b"<!DOCTYPE html><html><body>Wayback Machine error</body></html>"
    ok, reason = validate_media_payload(html, "text/html", "photo.jpg")
    assert ok is False
    assert "html" in reason
    jpeg = b"\xff\xd8\xff" + b"\x00" * 40
    ok, reason = validate_media_payload(jpeg, "image/jpeg", "photo.jpg")
    assert ok is True


def test_missing_archive_media_classification():
    asset = MediaAsset(
        original_url="https://projecthindukush.com/wp-content/uploads/missing.jpg",
        archive_url="",
        filename="missing.jpg",
        source_page="https://projecthindukush.com/incident/x",
        skipped_reason="archive-miss",
        http_status=404,
    )
    assert classify_media_status(asset) == "missing_from_archive"
    only_march = MediaAsset(
        original_url="https://projecthindukush.com/wp-content/uploads/later.jpg",
        archive_url="",
        filename="later.jpg",
        source_page="https://projecthindukush.com/incident/x",
        skipped_reason="no_eligible_snapshot",
        eligibility="no_eligible_snapshot",
    )
    assert classify_media_status(only_march) == "missing_from_archive"


def test_media_deduplication_uses_normalized_original_url():
    existing = {
        normalize_url("https://projecthindukush.com/wp-content/uploads/a.jpg/"): 44,
    }
    key = normalize_url("https://projecthindukush.com/wp-content/uploads/a.jpg")
    assert key in existing
    assert existing[key] == 44


def test_idempotent_media_import_reuses_attachment():
    existing = {
        normalize_url("https://projecthindukush.com/wp-content/uploads/a.jpg"): 12,
    }

    def import_plan(original_url: str) -> tuple[str, int | None]:
        found = existing.get(normalize_url(original_url))
        if found:
            return "already_exists", found
        return "create", None

    status, attachment_id = import_plan("https://projecthindukush.com/wp-content/uploads/a.jpg")
    assert status == "already_exists"
    assert attachment_id == 12
    reused = MediaAsset(
        original_url="https://projecthindukush.com/wp-content/uploads/a.jpg",
        archive_url="",
        filename="a.jpg",
        source_page="https://projecthindukush.com/incident/x",
        downloaded=True,
        skipped_reason="duplicate-sha256",
        sha256="abc",
    )
    assert classify_media_status(reused, imported=True, attachment_id=12) == "already_exists"
    failed = MediaAsset(
        original_url="https://projecthindukush.com/wp-content/uploads/b.jpg",
        archive_url="",
        filename="b.jpg",
        source_page="https://projecthindukush.com/incident/x",
        downloaded=True,
    )
    assert classify_media_status(failed, imported=False) == "import_failed"


class _Capture:
    def __init__(self, timestamp: str, mimetype: str = "image/jpeg") -> None:
        self.timestamp = timestamp
        self.mimetype = mimetype


def test_media_lookup_ignores_march_captures():
    best, status = lookup_eligible_media_capture(
        [_Capture("20260101000000"), _Capture("20260301000000")],
    )
    assert status == "eligible"
    assert best is not None
    assert best.timestamp == "20260101000000"
    missing, status = lookup_eligible_media_capture([_Capture("20260301000000")])
    assert missing is None
    assert status == "no_eligible_snapshot"
