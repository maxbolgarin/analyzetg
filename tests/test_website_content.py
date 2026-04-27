"""Content extraction + paragraph segmentation."""

from __future__ import annotations

from unread.website.content import _extract_with_bs4, _segment_paragraphs


def test_segment_short_text_one_segment() -> None:
    assert _segment_paragraphs("Hello world.") == ["Hello world."]


def test_segment_empty_returns_empty() -> None:
    assert _segment_paragraphs("") == []
    assert _segment_paragraphs("   ") == []


def test_segment_splits_on_blank_lines() -> None:
    p1 = "x" * 40
    p2 = "y" * 40
    p3 = "z" * 40
    text = f"{p1}\n\n{p2}\n\n{p3}"
    # Budget below "two paragraphs joined" — must split at blank lines.
    parts = _segment_paragraphs(text, max_chars=50)
    assert len(parts) >= 2
    for part in parts:
        assert len(part) <= 50


def test_segment_packs_short_paragraphs_together() -> None:
    text = "Short one.\n\nShort two.\n\nShort three."
    parts = _segment_paragraphs(text, max_chars=200)
    # All three fit comfortably under 200 → one packed segment.
    assert len(parts) == 1
    assert "Short one." in parts[0]
    assert "Short three." in parts[0]


def test_segment_breaks_when_paragraph_exceeds_budget() -> None:
    big = "x" * 5000
    parts = _segment_paragraphs(big, max_chars=1000)
    assert len(parts) >= 5
    for p in parts:
        assert len(p) <= 1000


def test_segment_sentence_boundary_for_long_paragraph() -> None:
    sentences = ["Sentence one." for _ in range(400)]
    text = " ".join(sentences)  # one giant paragraph (no blank lines)
    parts = _segment_paragraphs(text, max_chars=200)
    assert len(parts) > 1
    for p in parts:
        assert len(p) <= 200


# --- BS4 fallback extractor ------------------------------------------------


_SAMPLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <title>My Article Title</title>
  <meta property="og:site_name" content="Example Blog" />
  <meta name="author" content="Jane Doe" />
  <meta property="article:published_time" content="2024-03-15" />
</head>
<body>
  <header>Header chrome to ignore</header>
  <nav>Nav nav nav</nav>
  <article>
    <h1>My Article Title</h1>
    <p>First paragraph of the article body.</p>
    <p>Second paragraph with more substance and several words.</p>
    <h2>A subsection</h2>
    <p>Third paragraph belongs here.</p>
  </article>
  <aside>Sidebar to ignore</aside>
  <footer>Copyright stuff</footer>
</body>
</html>
"""


def test_extract_with_bs4_recovers_metadata() -> None:
    metadata, _text = _extract_with_bs4(
        _SAMPLE_HTML,
        url="https://example.com/article",
        normalized="https://example.com/article",
    )
    assert metadata.title == "My Article Title"
    assert metadata.site_name == "Example Blog"
    assert metadata.author == "Jane Doe"
    assert metadata.published == "2024-03-15"
    assert metadata.language == "en"
    assert metadata.domain == "example.com"
    assert metadata.url == "https://example.com/article"
    assert metadata.page_id  # 16-char hash


def test_extract_with_bs4_drops_chrome() -> None:
    _, text = _extract_with_bs4(
        _SAMPLE_HTML,
        url="https://example.com/article",
        normalized="https://example.com/article",
    )
    assert "Header chrome" not in text
    assert "Sidebar" not in text
    assert "Copyright" not in text
    assert "First paragraph" in text
    assert "Third paragraph" in text


def test_extract_with_bs4_falls_back_to_domain_for_site_name() -> None:
    """Pages without og:site_name surface the bare domain."""
    html = "<html><head><title>X</title></head><body><p>Body.</p></body></html>"
    metadata, _ = _extract_with_bs4(
        html, url="https://news.example.org/x", normalized="https://news.example.org/x"
    )
    assert metadata.site_name == "news.example.org"


def test_extract_with_bs4_whole_body_fallback() -> None:
    """No semantic tags → fall back to body.get_text() so we still produce something."""
    html = (
        "<html><body><div><span>Just a span.</span></div><div><span>And another.</span></div></body></html>"
    )
    _, text = _extract_with_bs4(html, url="https://example.com/x", normalized="https://example.com/x")
    assert "Just a span." in text
    assert "And another." in text


# --- _explain_empty_extraction --------------------------------------------


def test_explain_empty_extraction_detects_spa() -> None:
    from unread.website.content import _explain_empty_extraction

    html = "<html><head></head><body><md-root></md-root><noscript></noscript></body></html>"
    msg = _explain_empty_extraction("https://spa.example.com/", html, raw_size=1200)
    assert "JavaScript" in msg or "single-page" in msg


def test_explain_empty_extraction_short_non_spa() -> None:
    from unread.website.content import _explain_empty_extraction

    html = "<html><body></body></html>"  # tiny but no SPA markers
    msg = _explain_empty_extraction("https://example.com/", html, raw_size=100)
    assert "redirect" in msg or "login" in msg or "landing" in msg


def test_explain_empty_extraction_large_no_text() -> None:
    from unread.website.content import _explain_empty_extraction

    html = "<html><body>" + "<svg></svg>" * 5000 + "</body></html>"  # large but empty
    msg = _explain_empty_extraction("https://example.com/", html, raw_size=200_000)
    assert "scripted" in msg or "paywalled" in msg or "unusual" in msg
