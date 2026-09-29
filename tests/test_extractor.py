from pathlib import Path

from extractor.listing_extractor import extract_listing
from scraper.media import collect_media
from scraper.parser import parse_archived_page
from utils.config import load_field_mapping


FIXTURE = Path(__file__).parent / "fixtures" / "varanasi.html"


def test_extract_known_varanasi_fields_from_labels_not_constants():
    parsed = parse_archived_page(
        FIXTURE.read_text(encoding="utf-8"),
        "https://projecthindukush.com/incident/2006-varanasi-bombings-varanasi-india/",
        "projecthindukush.com",
    )
    listing = extract_listing(
        parsed,
        load_field_mapping(),
        ["date", "murdered", "perpetrators", "were_you_there", "collected_by", "source", "location"],
        "20260215104259",
    )
    assert listing.title == "2006 Varanasi Bombings [Varanasi, India]"
    assert listing.fields["date"] == "07/03/2006"
    assert listing.fields["murdered"] == "28"
    assert listing.fields["perpetrators"] == "Laskar-E-Qahab"
    assert listing.fields["were_you_there"] == "NO"
    assert listing.fields["collected_by"] == "Common Crawl"
    assert "rediff.com" in (listing.fields["source"] or "")
    assert listing.location.city == "Varanasi"
    assert listing.location.state == "Uttar Pradesh"
    assert listing.location.country == "India"
    assert listing.missing == []
    assert listing.listingpro_detected is True


def test_image_extraction_skips_trackers_and_keeps_uploads():
    parsed = parse_archived_page(
        FIXTURE.read_text(encoding="utf-8"),
        "https://projecthindukush.com/incident/2006-varanasi-bombings-varanasi-india/",
        "projecthindukush.com",
    )
    assets = collect_media(parsed["images"], parsed["source_url"], "20260215104259")
    kept = [asset for asset in assets if not asset.skipped_reason]
    skipped = [asset for asset in assets if asset.skipped_reason]
    assert any("skull-red-1.png" in asset.original_url for asset in kept)
    assert any("google-analytics" in (asset.original_url + (asset.skipped_reason or "")) for asset in skipped)
