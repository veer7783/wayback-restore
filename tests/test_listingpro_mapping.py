from pathlib import Path

from extractor.listing_extractor import extract_listing
from scraper.parser import parse_archived_page
from utils.config import load_field_mapping
from wordpress.importer import build_payload
from wordpress.listingpro_mapping import (
    classify_media_urls,
    extra_form_fields,
    listingpro_options,
    source_to_listingpro_mapping,
    wordpress_slug,
)


FIXTURE = Path(__file__).parent / "fixtures" / "varanasi.html"


def _listing():
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
    return listing, parsed


def test_listingpro_mapping_uses_live_field_names():
    listing, parsed = _listing()
    mapping = source_to_listingpro_mapping(listing, parsed)
    payload = build_payload(
        listing,
        "https://web.archive.org/web/20260215104259/https://projecthindukush.com/incident/x/",
        "20260215104259",
        {"post_types": ["listing"], "taxonomies": ["listing-category", "location", "features", "list-tags"]},
        "http://localhost/projecthindukush",
        parsed,
    )
    assert payload["post_type"] == "listing"
    assert payload["post_name"] == "2006-varanasi-bombings-varanasi-india"
    assert payload["lp_listingpro_options"]["gAddress"] == listing.location.raw
    if listing.location.lat:
        assert payload["lp_listingpro_options"]["latitude"] == listing.location.lat
    if listing.location.lng:
        assert payload["lp_listingpro_options"]["longitude"] == listing.location.lng
    assert "date" not in payload["lp_listingpro_options"]
    assert payload["lp_listingpro_options_fields"]["date"] == "07/03/2006"
    assert payload["taxonomies"]["listing-category"] == ([listing.category] if listing.category else [])
    assert payload["taxonomies"]["location"] == ([listing.location.raw] if listing.location.raw else [])
    assert payload["taxonomies"]["features"] == []
    assert "web.archive.org" not in payload["post_content"]
    assert payload["meta"]["_pkh_source_id"]
    assert payload["collected_by_status"] == "missing_from_archive" or listing.fields.get("collected_by")
    collected = [row for row in mapping["mappings"] if row["source"] == "collected_by"]
    if listing.field_status.get("collected_by") == "missing_from_archive":
        assert collected[0]["status"] == "missing_from_archive"


def test_extra_field_slugs_and_gallery_selection():
    listing, parsed = _listing()
    fields = extra_form_fields(listing)
    slugs = {item["slug"] for item in fields}
    assert wordpress_slug("How many were Murdered?") == "how-many-were-murdered"
    assert "date" in slugs
    assert "perpetrators" in slugs
    media = classify_media_urls(
        listing,
        {
            "images": [
                {"src": "https://projecthindukush.com/wp-content/uploads/07varanasi2-550x420.jpg"},
                {"src": "https://projecthindukush.com/wp-content/uploads/support-MOHH-683x1024.jpg"},
            ],
            "links": {
                "internal": ["https://projecthindukush.com/wp-content/uploads/07varanasi2.jpg"]
            },
            "metadata": {
                "og:image": "https://projecthindukush.com/wp-content/uploads/bomb-1445240686_835x547.jpg"
            },
        },
    )
    assert media["featured_image_url"] and "bomb-1445240686" in media["featured_image_url"]
    assert any(url.endswith("07varanasi2.jpg") for url in media["gallery_urls"])
    assert all("support-mohh" not in url.lower() for url in media["all_urls"])
    assert all("550x420" not in url for url in media["all_urls"])
    options = listingpro_options(listing)
    assert set(options) <= {"gAddress", "latitude", "longitude"}
