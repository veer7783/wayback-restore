from scraper.cdx import dedupe_records, parse_cdx_json, seed_known_record, unique_original_urls
from scraper.snapshots import score_snapshot, select_best_snapshot


SAMPLE = [
    ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
    [
        "com,projecthindukush)/incident/one",
        "20240101000000",
        "https://projecthindukush.com/incident/one/",
        "text/html",
        "200",
        "ABC",
        "12000",
    ],
    [
        "com,projecthindukush)/incident/one",
        "20260215104259",
        "https://projecthindukush.com/incident/one/",
        "text/html",
        "200",
        "DEF",
        "24000",
    ],
    [
        "com,projecthindukush)/incident/one",
        "20220101000000",
        "https://projecthindukush.com/incident/one/",
        "warc/revisit",
        "302",
        "ABC",
        "200",
    ],
]


def test_seed_known_record():
    record = seed_known_record(
        "https://projecthindukush.com/incident/2006-varanasi-bombings-varanasi-india/",
        "20260215104259",
    )
    assert record.status_code == 200
    assert record.timestamp == "20260215104259"
    assert "20260215104259" in record.archive_url


def test_parse_and_dedupe_cdx():
    records = parse_cdx_json(SAMPLE)
    assert len(records) == 3
    assert records[0].archive_url.startswith("https://web.archive.org/web/")
    duplicates = dedupe_records(records + [records[0]])
    assert len(duplicates) == 3
    assert unique_original_urls(records) == [
        "https://projecthindukush.com/incident/one"
    ]


def test_snapshot_scoring_prefers_html_200_with_structure_not_just_latest():
    records = parse_cdx_json(SAMPLE)
    older_rich = records[0]
    latest = records[1]
    redirect = records[2]

    html = "<html class='listingpro'><div class='lp-listing-description'>body</div><img src='/wp-content/uploads/a.png'><li>Perpetrators: x</li></html>"
    rich_score = score_snapshot(older_rich, html=html)
    latest_score = score_snapshot(latest, html="<html></html>")
    redirect_score = score_snapshot(redirect)

    assert rich_score.total > latest_score.total
    assert latest_score.total > redirect_score.total

    selected = select_best_snapshot(
        records,
        html_by_timestamp={
            older_rich.timestamp: html,
            latest.timestamp: "<html></html>",
        },
    )
    assert selected is not None
    assert selected.timestamp == older_rich.timestamp
    assert "not-latest-by-choice" in selected.selected_reason
