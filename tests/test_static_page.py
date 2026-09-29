from scraper.static_page import extract_static_page


def test_static_page_extraction_keeps_content_and_removes_unsafe_markup():
    html = """
    <html><head><title>About Us - PROJECT HINDUKUSH [Hindu Genocide Watch]</title></head>
    <body><header>Header</header>
    <div class="lp-section-row"><h1>Our history</h1>
      <img src="/wp-content/uploads/history.jpg" onerror="bad()">
      <a href="/resistance/">Resistance</a>
      <form action="https://bad.example"><input name="x"></form>
      <script>alert(1)</script>
    </div><footer>Footer</footer></body></html>
    """
    page = extract_static_page(html, "https://projecthindukush.com/about-us/", "about-us")
    assert page["title"] == "About Us"
    assert "Our history" in page["content_text"]
    assert page["media_urls"] == ["https://projecthindukush.com/wp-content/uploads/history.jpg"]
    assert "https://projecthindukush.com/resistance/" in page["content_html"]
    assert "<form" not in page["content_html"]
    assert "<script" not in page["content_html"]
    assert "onerror" not in page["content_html"]


def test_nested_sections_are_not_duplicated():
    html = """
    <title>Home – PROJECT HINDUKUSH [Hindu Genocide Watch]</title>
    <style data-type="vc_shortcodes-custom-css">
      .vc_custom_1{background-image:url(/wp-content/uploads/hero.jpg) !important;}
    </style>
    <section class="vc_section vc_custom_1"><div class="lp-section-row">Only once</div></section>
    """
    page = extract_static_page(html, "https://projecthindukush.com/new-home/", "new-home")
    assert page["content_text"] == "Only once"
    assert "background-image:" in page["content_html"]
    assert page["media_urls"] == ["https://projecthindukush.com/wp-content/uploads/hero.jpg"]
