"""URL-based source-language inference for the website analyzer.

Covers the three rules in :func:`unread.website.commands._source_language_from_url`:
  1. Leading subdomain (`<lang>.host.tld`).
  2. ccTLD with an unambiguous dominant language (`alex.ru`).
  3. 2-letter language segment anywhere in the path
     (`max.com/asfa/ru`, `app.com/en-US/help`).

False-positive guarding is just as important as positive matches —
inferring the wrong source language actively hurts the LLM prompt, so
the helper must return None on anything ambiguous.
"""

from __future__ import annotations

import pytest

from unread.website.commands import _source_language_from_url


# --- Rule 1: leading subdomain ------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://ru.wikipedia.org/wiki/X", "ru"),
        ("https://zh.wikipedia.org/wiki/%E7%89%A9", "zh"),
        ("https://de.example.com/article", "de"),
        ("https://ja.news.example.org/post/123", "ja"),
        # The leading-label rule needs at least 3 host labels to fire,
        # so a 2-label host like `de.com` falls through to the ccTLD rule.
        ("https://de.com/x", "de"),  # caught by ccTLD .com → no, .com unmapped → fallthrough
    ],
)
def test_subdomain_leading_iso_code(url: str, expected: str) -> None:
    # The third case is a placeholder — `.com` is not mapped, so the
    # actual fallthrough hits rule 3 (path); `/x` has nothing matching.
    # Only the first two assertions are meaningful for rule 1.
    if "de.com" in url:
        assert _source_language_from_url(url) is None
    else:
        assert _source_language_from_url(url) == expected


# --- Rule 2: ccTLD ------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://alex.ru/blog", "ru"),
        ("https://example.de/post/123", "de"),
        ("https://news.fr/articles/x", "fr"),
        ("https://site.it/news", "it"),
        ("https://example.br/news", "pt"),  # .br → Portuguese
        ("https://example.mx/news", "es"),  # .mx → Spanish
        ("https://anything.cn/page", "zh"),
    ],
)
def test_cctld_unambiguous_mapping(url: str, expected: str) -> None:
    assert _source_language_from_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        # Multilingual countries — intentionally NOT mapped.
        "https://example.ca/post",  # en/fr
        "https://example.ch/post",  # de/fr/it/rm
        "https://example.be/post",  # nl/fr
        "https://example.in/post",  # multi
        # Generic / vanity ccTLDs — intentionally NOT mapped.
        "https://example.io/post",
        "https://example.ai/post",
        "https://example.me/post",
        "https://example.tv/post",
        "https://example.co/post",
        # gTLDs with no language signal.
        "https://example.com/post",
        "https://example.org/post",
        "https://example.net/post",
    ],
)
def test_cctld_unmapped_returns_none(url: str) -> None:
    assert _source_language_from_url(url) is None


# --- Rule 3: path segment ------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://example.com/ru/page", "ru"),
        ("https://docs.example.com/de/intro", "de"),
        ("https://max.com/asfa/ru", "ru"),  # ru is not first segment
        ("https://app.example.com/en-US/help", "en"),  # split on `-`
        ("https://app.example.com/pt-BR/docs", "pt"),
        ("https://app.example.com/zh-Hans/page", "zh"),
    ],
)
def test_path_segment_two_letter_match(url: str, expected: str) -> None:
    assert _source_language_from_url(url) == expected


# --- False-positive guards ----------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # Empty / malformed.
        "",
        "not-a-url",
        "https://",
        # No language signal in any rule.
        "https://example.com/blog/2024/post",
        "https://github.com/anthropics/claude-code",
        "https://example.com/products/widget",
        # Path segments that are 3+ chars or non-ISO.
        "https://example.com/blog/post",
        "https://example.com/admin/login",
        # Subdomain that's a 2-letter NON-ISO string.
        "https://xx.example.com/post",  # xx not in allowlist
    ],
)
def test_no_signal_returns_none(url: str) -> None:
    assert _source_language_from_url(url) is None


def test_subdomain_takes_precedence_over_cctld() -> None:
    """When both rules would match, the subdomain wins (more specific)."""
    # `ru.example.de` — subdomain says ru, ccTLD says de. Subdomain wins.
    assert _source_language_from_url("https://ru.example.de/post") == "ru"


def test_cctld_takes_precedence_over_path() -> None:
    """A definite ccTLD wins over a path-segment match."""
    # `news.de/en/post` — ccTLD says de, path says en. ccTLD wins
    # because rule 2 fires before rule 3.
    assert _source_language_from_url("https://news.de/en/post") == "de"
