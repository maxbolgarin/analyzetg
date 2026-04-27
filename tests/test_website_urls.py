"""URL detection, normalization, and stable id derivation."""

from __future__ import annotations

import pytest

from atg.website.urls import (
    domain_of,
    is_telegram_url,
    is_website_url,
    normalize_url,
    page_id,
)


class TestIsWebsiteUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com",
            "https://example.com/",
            "https://example.com/blog/post",
            "http://example.com/x?q=1",
            "https://www.paulgraham.com/greatwork.html",
            "https://t.me/durov",  # still True at this layer; is_telegram_url separates
        ],
    )
    def test_recognized(self, url: str) -> None:
        assert is_website_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            None,
            "",
            "example.com",  # missing scheme
            "@user",
            "12345",
            "ftp://example.com",
            "file:///etc/passwd",
            "tg://resolve?domain=foo",
        ],
    )
    def test_rejects_non_url(self, url) -> None:
        assert is_website_url(url) is False


class TestIsTelegramUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://t.me/durov",
            "https://t.me/c/123/456",
            "https://telegram.me/durov",
            "https://telegra.ph/post",
            "http://t.me/joinchat/abc",
        ],
    )
    def test_recognized(self, url: str) -> None:
        assert is_telegram_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            None,
            "",
            "https://example.com",
            "https://www.youtube.com/watch?v=x",
            "@durov",
            "12345",
            "https://blog.t.me.fake.com/post",  # spoof — must NOT be flagged
        ],
    )
    def test_rejects_non_telegram(self, url) -> None:
        assert is_telegram_url(url) is False


class TestNormalizeUrl:
    def test_strips_tracking_params(self) -> None:
        n = normalize_url("https://example.com/post?utm_source=newsletter&id=42")
        assert n == "https://example.com/post?id=42"

    def test_strips_fragment(self) -> None:
        assert normalize_url("https://example.com/post#section-2") == "https://example.com/post"

    def test_lowercases_scheme_and_host(self) -> None:
        assert normalize_url("HTTPS://Example.COM/Path") == "https://example.com/Path"

    def test_drops_default_ports(self) -> None:
        assert normalize_url("https://example.com:443/x") == "https://example.com/x"
        assert normalize_url("http://example.com:80/x") == "http://example.com/x"

    def test_keeps_non_default_ports(self) -> None:
        assert normalize_url("https://example.com:8443/x") == "https://example.com:8443/x"

    def test_trims_trailing_punctuation(self) -> None:
        assert normalize_url("https://example.com/post.") == "https://example.com/post"
        assert normalize_url("https://example.com/post),") == "https://example.com/post"

    def test_preserves_load_bearing_query(self) -> None:
        n = normalize_url("https://example.com/search?q=site:foo.com")
        assert "q=site" in n

    def test_preserves_trailing_slash(self) -> None:
        assert normalize_url("https://example.com/foo/") == "https://example.com/foo/"
        assert normalize_url("https://example.com/foo") == "https://example.com/foo"

    def test_drops_multiple_trackers(self) -> None:
        n = normalize_url("https://example.com/x?utm_source=a&utm_medium=b&fbclid=c&id=42")
        assert n == "https://example.com/x?id=42"


class TestPageId:
    def test_stable_for_same_url(self) -> None:
        a = page_id("https://example.com/post")
        b = page_id("https://example.com/post")
        assert a == b

    def test_differs_for_different_urls(self) -> None:
        a = page_id("https://example.com/a")
        b = page_id("https://example.com/b")
        assert a != b

    def test_is_16_chars(self) -> None:
        assert len(page_id("https://example.com")) == 16


class TestDomainOf:
    def test_strips_www(self) -> None:
        assert domain_of("https://www.example.com/post") == "example.com"

    def test_keeps_subdomain(self) -> None:
        assert domain_of("https://blog.example.com/post") == "blog.example.com"

    def test_lowercase(self) -> None:
        assert domain_of("https://Example.COM/x") == "example.com"

    def test_empty_for_garbage(self) -> None:
        # urlparse returns None for hostname on bare strings.
        assert domain_of("not-a-url") == ""
