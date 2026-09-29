from pipeline.restore import is_placeholder_listing, next_unique_urls


def test_next_unique_urls_skips_imported_and_stops_at_limit():
    excluded = {
        "https://projecthindukush.com/incident/2006-varanasi-bombings-varanasi-india",
        "https://www.projecthindukush.com/incident/already-saved/",
    }
    candidates = [
        "https://www.projecthindukush.com/incident/2006-varanasi-bombings-varanasi-india/",
        "https://projecthindukush.com/incident/already-saved",
        "https://projecthindukush.com/incident/new-one",
        "https://projecthindukush.com/incident/new-one/",
        "https://projecthindukush.com/incident/new-two",
        "https://projecthindukush.com/incident/new-three",
    ]
    chosen = next_unique_urls(candidates, excluded, 2)
    assert chosen == [
        "https://projecthindukush.com/incident/new-one",
        "https://projecthindukush.com/incident/new-two",
    ]


def test_placeholder_listing_rejects_archive_challenge_page():
    assert is_placeholder_listing("One moment, please...", "") is True
    assert is_placeholder_listing("Attention Required", "x" * 200) is True
    assert is_placeholder_listing("2006 Varanasi Bombings", "x" * 80) is False
