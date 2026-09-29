from scraper.cdx import CDXRecord
from scraper.eligibility import is_eligible_timestamp, partition_by_cutoff
from scraper.snapshots import select_best_snapshot


def _record(timestamp: str, length: int = 10000, status: str = "200") -> CDXRecord:
    return CDXRecord(
        timestamp=timestamp,
        original="https://projecthindukush.com/incident/sample/",
        mimetype="text/html",
        statuscode=status,
        length=length,
    )


def test_february_28_cutoff_is_inclusive():
    assert is_eligible_timestamp("20200101000000")
    assert is_eligible_timestamp("20260131235959")
    assert is_eligible_timestamp("20260201000000")
    assert is_eligible_timestamp("20260228235959")


def test_march_1_and_later_are_excluded():
    assert not is_eligible_timestamp("20260301000000")
    assert not is_eligible_timestamp("20260401000000")
    partitioned = partition_by_cutoff(
        ["20260228235959", "20260301000000", "20260401000000"]
    )
    assert partitioned["eligible"] == ["20260228235959"]
    assert partitioned["rejected"] == ["20260301000000", "20260401000000"]


def test_selects_latest_eligible_snapshot_not_march():
    selected = select_best_snapshot(
        [
            _record("20260101000000", length=9000),
            _record("20260228235959", length=11000),
            _record("20260315000000", length=90000),
        ]
    )
    assert selected is not None
    assert selected.timestamp == "20260228235959"
    assert selected.eligibility_status == "eligible"
    assert "latest-eligible" in selected.selected_reason
    assert "cutoff-applied" in selected.selected_reason
    assert selected.rejected_after_cutoff == 1
    assert int(selected.timestamp) <= 20260228235959


def test_no_eligible_snapshot_when_only_post_february_exists():
    selected = select_best_snapshot([_record("20260301000000", length=50000)])
    assert selected is not None
    assert selected.selected_reason == "no_eligible_snapshot"
    assert selected.eligibility_status == "no_eligible_snapshot"
    assert selected.timestamp == ""
    assert selected.archive_url == ""


def test_march_html_is_never_used_as_fallback():
    march_html = "<html class='listingpro'><div class='post-detail-content'>rich</div></html>"
    selected = select_best_snapshot(
        [_record("20250101000000", length=8000), _record("20260301000000", length=80000)],
        html_by_timestamp={"20260301000000": march_html},
    )
    assert selected is not None
    assert selected.timestamp == "20250101000000"
    assert selected.timestamp != "20260301000000"
