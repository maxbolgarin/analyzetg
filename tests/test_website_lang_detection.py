"""HTML / HTTP language-signal extraction in `unread/website/content.py`.

Verifies the documented priority chain that resolves a page's
``WebsiteMetadata.language``:

  1. RFC 7231 ``Content-Language`` HTTP response header
  2. ``<html lang>`` attribute
  3. ``<meta http-equiv="content-language">``
  4. ``<meta property="og:locale">``
  5. ``<link rel="alternate" hreflang="..." href="..." />`` — only when
     the alternate's ``href`` matches the canonical URL or the input URL.

Also exercises BCP-47 → ISO 639-1 normalization (``ru-RU`` → ``ru``) and
the regex fallback used when BeautifulSoup is unavailable.
"""

from __future__ import annotations

import pytest

from unread.website.content import (
    _content_language_from_header,
    _detect_html_language,
    _normalize_lang_tag,
)


# --- _normalize_lang_tag ------------------------------------------------


@pytest.mark.parametrize(
    "tag, expected",
    [
        ("ru", "ru"),
        ("RU", "ru"),
        ("ru-RU", "ru"),
        ("en_US", "en"),
        ("zh-Hans", "zh"),
        ("zh-Hant-TW", "zh"),
        ("  de  ", "de"),
        ("DE-AT", "de"),
        # 3-letter ISO 639-2/3 not currently supported (preset / i18n
        # layer only knows 2-letter codes).
        ("eng", None),
        ("rus", None),
        # Empty / malformed.
        ("", None),
        (None, None),
        ("zzz_invalid", None),
        ("123", None),
    ],
)
def test_normalize_lang_tag(tag, expected) -> None:
    assert _normalize_lang_tag(tag) == expected


# --- _content_language_from_header --------------------------------------


@pytest.mark.parametrize(
    "header, expected",
    [
        ("ru", "ru"),
        ("ru-RU", "ru"),
        # Multi-language header (RFC 7231 allows comma-separated list);
        # take the first.
        ("en, ru", "en"),
        ("ru, en", "ru"),
        ("zh-Hans, en;q=0.5", "zh"),
        # Empty / missing.
        ("", None),
        (None, None),
    ],
)
def test_content_language_from_header(header, expected) -> None:
    assert _content_language_from_header(header) == expected


# --- _detect_html_language: priority chain ------------------------------


def test_html_lang_attribute_wins() -> None:
    """Rule 1 is the most reliable signal; it wins over later rules."""
    html = """
    <html lang="ru">
      <head>
        <meta http-equiv="content-language" content="en">
        <meta property="og:locale" content="de_DE">
      </head>
      <body>x</body>
    </html>
    """
    assert _detect_html_language(html) == "ru"


def test_html_lang_normalizes_region_tag() -> None:
    """`<html lang="zh-Hans">` → `zh`."""
    html = '<html lang="zh-Hans"><body>x</body></html>'
    assert _detect_html_language(html) == "zh"


def test_meta_http_equiv_when_html_lang_missing() -> None:
    """Rule 2 fires when rule 1 is absent."""
    html = """
    <html>
      <head><meta http-equiv="content-language" content="ru-RU"></head>
      <body>x</body>
    </html>
    """
    assert _detect_html_language(html) == "ru"


def test_og_locale_when_higher_priority_missing() -> None:
    """Rule 3 (Open Graph) fires when 1 and 2 are absent."""
    html = """
    <html>
      <head><meta property="og:locale" content="de_DE"></head>
      <body>x</body>
    </html>
    """
    assert _detect_html_language(html) == "de"


def test_hreflang_alternate_matches_canonical() -> None:
    """Rule 4: hreflang alternate whose href matches the canonical URL."""
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://example.com/ru/post">
        <link rel="alternate" hreflang="x-default" href="https://example.com/post">
        <link rel="alternate" hreflang="en" href="https://example.com/en/post">
        <link rel="alternate" hreflang="ru" href="https://example.com/ru/post">
        <link rel="alternate" hreflang="de" href="https://example.com/de/post">
      </head>
      <body>x</body>
    </html>
    """
    assert _detect_html_language(html) == "ru"


def test_hreflang_alternate_matches_input_url() -> None:
    """Rule 4 falls back to matching the input URL when no canonical."""
    html = """
    <html>
      <head>
        <link rel="alternate" hreflang="en" href="https://example.com/en/post">
        <link rel="alternate" hreflang="ja" href="https://example.com/ja/post">
      </head>
      <body>x</body>
    </html>
    """
    assert _detect_html_language(html, url="https://example.com/ja/post") == "ja"


def test_hreflang_without_url_match_returns_none() -> None:
    """An hreflang list without a matching href doesn't tell us the
    CURRENT page's language — only that other versions exist."""
    html = """
    <html>
      <head>
        <link rel="alternate" hreflang="en" href="https://example.com/en/post">
        <link rel="alternate" hreflang="ru" href="https://example.com/ru/post">
      </head>
      <body>x</body>
    </html>
    """
    # No canonical, no matching input URL → can't infer current language.
    assert _detect_html_language(html, url="https://example.com/something/else") is None


def test_hreflang_x_default_skipped() -> None:
    """`hreflang="x-default"` is the language-neutral root entry; ignore it."""
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://example.com/post">
        <link rel="alternate" hreflang="x-default" href="https://example.com/post">
      </head>
      <body>x</body>
    </html>
    """
    assert _detect_html_language(html) is None


def test_no_signals_returns_none() -> None:
    """Page with no language metadata at all → caller falls through to
    URL-based / LLM-side detection."""
    html = "<html><head><title>x</title></head><body>x</body></html>"
    assert _detect_html_language(html) is None


def test_empty_html() -> None:
    assert _detect_html_language("") is None
    assert _detect_html_language(None) is None  # type: ignore[arg-type]


# --- regex fallback (when BS4 is unavailable) ---------------------------


def test_regex_fallback_for_html_lang(monkeypatch) -> None:
    """When BeautifulSoup isn't installed, the regex fallback still
    catches the most common signal: `<html lang="...">`."""
    monkeypatch.setattr("unread.website.content._HAS_BS4", False)
    html = '<html class="x" dir="ltr" lang="ru" id="main"><body>x</body></html>'
    assert _detect_html_language(html) == "ru"


def test_regex_fallback_misses_meta_signals(monkeypatch) -> None:
    """The regex fallback ONLY catches `<html lang>`. `<meta>` / hreflang
    rules need BS4 — caller falls through to URL / LLM detection."""
    monkeypatch.setattr("unread.website.content._HAS_BS4", False)
    html = """
    <html>
      <head><meta http-equiv="content-language" content="ru"></head>
      <body>x</body>
    </html>
    """
    assert _detect_html_language(html) is None
