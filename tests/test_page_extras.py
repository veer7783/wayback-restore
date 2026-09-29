from bs4 import BeautifulSoup

from scraper.page_extras import extract_publication_date, extract_site_chrome, extract_videos
from scraper.video import normalize_video_url, validate_video_payload


HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"WebPage","datePublished":"2022-10-18T16:31:27+00:00"}
</script></head><body>
<div id="page" data-sitelogo="https://projecthindukush.com/wp-content/uploads/site-logo.png">
<header><nav><a href="/about-us/">Why this?</a></nav></header>
<div class="post-detail-content">
<video poster="/wp-content/uploads/poster.jpg"><source src="/wp-content/uploads/event.mp4?_=1"></video>
<iframe src="https://www.youtube.com/embed/abc"></iframe>
</div>
<footer><a href="/about-us/">About</a><span>Copyright Project Hindu Kush</span></footer>
</div></body></html>
"""


def test_publication_date_is_distinct_deterministic_metadata():
    soup = BeautifulSoup(HTML, "lxml")
    result = extract_publication_date(soup)
    assert result["value"] == "2022-10-18T16:31:27+00:00"
    assert result["source"] == "json_ld.datePublished"
    assert extract_publication_date(BeautifulSoup("<html></html>", "lxml")) == {
        "value": None,
        "source": "fallback",
    }


def test_video_extraction_preserves_first_party_and_external_embed():
    videos = extract_videos(BeautifulSoup(HTML, "lxml"), "https://projecthindukush.com/incident/x/")
    assert any(item["first_party"] and item["original_video_url"].endswith("event.mp4?_=1") for item in videos)
    assert any(item["video_type"] == "youtube" and not item["first_party"] for item in videos)
    assert normalize_video_url(videos[0]["original_video_url"]).endswith("event.mp4")


def test_header_logo_and_footer_are_extracted_without_wayback():
    chrome = extract_site_chrome(
        BeautifulSoup(HTML, "lxml"),
        "https://projecthindukush.com/incident/x/",
    )
    assert chrome["logo_url"].endswith("/site-logo.png")
    assert chrome["navigation"][0]["label"] == "Why this?"
    assert "Copyright Project Hindu Kush" in chrome["footer_text"]
    assert "web.archive.org" not in str(chrome)


def test_video_binary_validation_rejects_html_and_accepts_mp4():
    ok, reason = validate_video_payload(
        b"<!DOCTYPE html><html><body>missing</body></html>" + b"x" * 64,
        "text/html",
        "event.mp4",
    )
    assert not ok
    assert "html" in reason
    ok, reason = validate_video_payload(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 80, "video/mp4", "event.mp4")
    assert ok
    assert reason == "ok"
