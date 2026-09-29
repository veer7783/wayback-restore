from pathlib import Path

import pytest

from pipeline.batch40 import BATCH_LIMIT, validate_new_batch_urls


def test_batch40_requires_exactly_40_new_unique_urls():
    excluded = {"https://projecthindukush.com/incident/existing"}
    urls = [f"https://projecthindukush.com/incident/new-{index}" for index in range(BATCH_LIMIT)]
    validate_new_batch_urls(urls, excluded)
    with pytest.raises(ValueError, match="exactly 40"):
        validate_new_batch_urls(urls[:-1], excluded)
    with pytest.raises(ValueError, match="duplicate"):
        validate_new_batch_urls(urls[:-1] + [urls[0]], excluded)
    with pytest.raises(ValueError, match="overlaps"):
        validate_new_batch_urls(urls[:-1] + [next(iter(excluded))], excluded)


def test_child_theme_chrome_is_safe_and_does_not_modify_parent():
    root = Path(__file__).resolve().parents[1]
    functions = (root / "wp-child-theme" / "functions.php").read_text(encoding="utf-8")
    styles = (root / "wp-child-theme" / "pkh-restored-site-chrome.css").read_text(encoding="utf-8")
    combined = functions + styles
    assert "wp_body_open" in functions
    assert "get_footer" in functions
    assert "data-pkh-global-header" in functions
    assert "data-pkh-global-footer" in functions
    assert "web.archive.org" not in combined
    assert "googletagmanager" not in combined
    assert "google-analytics" not in combined
    assert "file_put_contents" not in combined
    assert "get_template_directory()" not in combined
