from scraper.urls import (
    build_wayback_url,
    html_basename,
    is_incident_url,
    normalize_url,
    rewrite_wayback_text,
    rewrite_wayback_url,
    strip_www,
    url_hash,
)


def test_normalize_url_strips_tracking_and_slash():
    left = "HTTPS://ProjectHinduKush.com/incident/2006-varanasi-bombings-varanasi-india/?utm_source=x"
    right = "https://projecthindukush.com/incident/2006-varanasi-bombings-varanasi-india"
    assert normalize_url(left) == right


def test_wayback_url_construction():
    url = build_wayback_url(
        "https://projecthindukush.com/incident/2006-varanasi-bombings-varanasi-india/",
        "20260215104259",
        raw=True,
    )
    assert url == (
        "https://web.archive.org/web/20260215104259id_/"
        "https://projecthindukush.com/incident/2006-varanasi-bombings-varanasi-india"
    )


def test_rewrite_wayback_url_and_text():
    archived = (
        "https://web.archive.org/web/20260215104259/"
        "https://projecthindukush.com/incident/2006-varanasi-bombings-varanasi-india/"
    )
    assert rewrite_wayback_url(archived) == (
        "https://projecthindukush.com/incident/2006-varanasi-bombings-varanasi-india/"
    )
    text = f'href="{archived}"'
    assert "web.archive.org" not in rewrite_wayback_text(text)


def test_incident_and_hash_are_stable():
    url = "https://projecthindukush.com/incident/2006-varanasi-bombings-varanasi-india/"
    assert strip_www("https://www.projecthindukush.com/incident/one/") == (
        "https://projecthindukush.com/incident/one"
    )
    assert is_incident_url(url)
    assert not is_incident_url("https://projecthindukush.com/incident/")
    assert url_hash(url) == url_hash(url.rstrip("/") + "/?utm_campaign=x")
    assert html_basename(url).endswith(".html")
