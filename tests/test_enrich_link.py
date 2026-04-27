"""Tests for URL extraction + normalization in enrich/link.py.

The network/OpenAI paths are mocked in `test_enrich_pipeline.py`; here we
cover the pure helpers so we can change the regex / skip rules with
confidence.
"""

from atg.enrich.link import _normalize_url, _url_hash, extract_urls


def test_extract_urls_basic():
    text = "Check out https://example.com/foo and http://example.org"
    assert extract_urls(text) == ["https://example.com/foo", "http://example.org"]


def test_extract_urls_strips_trailing_punctuation():
    text = "see https://example.com/path. also https://a.co/b!"
    got = extract_urls(text)
    assert "https://example.com/path" in got
    assert "https://a.co/b" in got


def test_extract_urls_skips_telegram():
    text = "ref https://t.me/channel/12345 but also https://example.com/ok"
    assert extract_urls(text) == ["https://example.com/ok"]


def test_extract_urls_dedupes():
    text = "https://a.co/x and https://a.co/x again"
    assert extract_urls(text) == ["https://a.co/x"]


def test_extract_urls_empty():
    assert extract_urls(None) == []
    assert extract_urls("") == []
    assert extract_urls("no links here at all") == []


def test_normalize_url_drops_fragment():
    assert _normalize_url("https://a.co/p#section") == "https://a.co/p"


def test_normalize_url_strips_trailing_punct():
    assert _normalize_url("https://a.co/p.") == "https://a.co/p"
    assert _normalize_url("https://a.co/p)") == "https://a.co/p"


def test_url_hash_stable():
    # Same URL → same hash; different URL → different hash.
    h1 = _url_hash("https://example.com/a")
    h2 = _url_hash("https://example.com/a")
    h3 = _url_hash("https://example.com/b")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 24
