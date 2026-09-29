from pathlib import Path

from scraper.parser import parse_archived_page
from scraper.urls import rewrite_wayback_text
from utils.config import load_field_mapping
from wordpress.importer import build_payload


FIXTURE = Path(__file__).parent / "fixtures" / "varanasi.html"


def test_parse_varanasi_fixture():
    html = FIXTURE.read_text(encoding="utf-8")
    parsed = parse_archived_page(
        html,
        "https://projecthindukush.com/incident/2006-varanasi-bombings-varanasi-india/",
        "projecthindukush.com",
    )
    assert parsed["title"] == "2006 Varanasi Bombings [Varanasi, India]"
    assert "28 persons were killed" in parsed["content"]
    assert parsed["listingpro_signals"]["is_listingpro"] is True
    labels = {key.lower(): value for key, value in parsed["labeled_fields"].items()}
    assert "07/03/2006" in labels.get("date", "")
    assert any("skull-red-1.png" in image["src"] for image in parsed["images"])
    assert parsed["canonical_url"].startswith("https://projecthindukush.com/")


def test_wordpress_payload_rewrites_wayback_and_uses_source_url():
    html = FIXTURE.read_text(encoding="utf-8")
    parsed = parse_archived_page(
        html,
        "https://projecthindukush.com/incident/2006-varanasi-bombings-varanasi-india/",
        "projecthindukush.com",
    )
    from extractor.listing_extractor import extract_listing

    listing = extract_listing(
        parsed,
        load_field_mapping(),
        ["date", "murdered", "perpetrators"],
        "20260215104259",
    )
    listing.content += " https://web.archive.org/web/20260215104259/https://projecthindukush.com/incident/x/"
    payload = build_payload(
        listing,
        "https://web.archive.org/web/20260215104259/https://projecthindukush.com/incident/x/",
        "20260215104259",
        {"post_types": ["listing"], "taxonomies": ["listing-category", "location"]},
        "http://localhost:8080",
    )
    assert "web.archive.org" not in payload["content"]
    assert payload["meta"]["_archive_source_url"].startswith("https://projecthindukush.com/")
    assert payload["post_type"] == "listing"
    assert rewrite_wayback_text(payload["content"]) == payload["content"]
