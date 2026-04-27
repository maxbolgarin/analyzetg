"""Unit-level coverage for the website command helpers + cache-key wiring.

Avoids stubbing the full `cmd_analyze_website` async flow (which would
need httpx + OpenAI + storage path mocking); instead pins the small
helpers (metadata header, synthetic message construction, row → page
restoration) and the `options_payload` cache-key contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

from unread.analyzer.pipeline import AnalysisOptions
from unread.analyzer.prompts import get_presets
from unread.website.commands import (
    _build_synthetic_messages,
    _meta_header,
    _restore_page_from_row,
)
from unread.website.metadata import WebsiteMetadata
from unread.website.paths import website_report_path


def _meta(**overrides) -> WebsiteMetadata:
    base = {
        "url": "https://example.com/article",
        "normalized_url": "https://example.com/article",
        "page_id": "abc1234567890def",
        "domain": "example.com",
        "title": "My Article",
        "site_name": "Example Blog",
        "author": "Jane Doe",
        "published": "2024-03-15",
        "language": "en",
        "word_count": 500,
    }
    base.update(overrides)
    return WebsiteMetadata(**base)


# --- _meta_header ----------------------------------------------------------


def test_meta_header_includes_key_fields() -> None:
    h = _meta_header(_meta(), paragraphs_count=10)
    assert "My Article" in h
    assert "Example Blog" in h
    assert "Jane Doe" in h
    assert "2024-03-15" in h
    assert "https://example.com/article" in h
    assert "Paragraphs: 10" in h
    assert "Word count: 500" in h


def test_meta_header_omits_empty_fields() -> None:
    bare = _meta(
        title=None,
        site_name=None,
        author=None,
        published=None,
        language=None,
        word_count=0,
    )
    h = _meta_header(bare, paragraphs_count=5)
    # Falls back to URL when title is missing.
    assert "https://example.com/article" in h
    assert "Author:" not in h
    assert "Published:" not in h
    assert "Word count:" not in h


# --- _build_synthetic_messages --------------------------------------------


def test_build_synthetic_messages_zeroth_is_header() -> None:
    msgs = _build_synthetic_messages(_meta(), ["P1.", "P2.", "P3."])
    assert msgs[0].msg_id == 0
    assert msgs[0].chat_id == 0
    assert "My Article" in (msgs[0].text or "")
    assert msgs[0].sender_name == "Example Blog"


def test_build_synthetic_messages_indices_are_sequential() -> None:
    msgs = _build_synthetic_messages(_meta(), ["P1.", "P2.", "P3."])
    assert [m.msg_id for m in msgs] == [0, 1, 2, 3]
    assert msgs[1].text == "P1."
    assert msgs[3].text == "P3."


def test_build_synthetic_messages_empty_paragraphs_just_header() -> None:
    msgs = _build_synthetic_messages(_meta(), [])
    assert len(msgs) == 1
    assert msgs[0].msg_id == 0


def test_build_synthetic_messages_sender_falls_back_to_domain() -> None:
    msgs = _build_synthetic_messages(_meta(site_name=None, author=None), ["P1."])
    assert msgs[0].sender_name == "example.com"


# --- _restore_page_from_row ------------------------------------------------


def test_restore_page_from_row_round_trip() -> None:
    row = {
        "page_id": "abc1234567890def",
        "url": "https://example.com/article",
        "normalized_url": "https://example.com/article",
        "domain": "example.com",
        "title": "My Article",
        "site_name": "Example Blog",
        "author": "Jane Doe",
        "published": "2024-03-15",
        "language": "en",
        "word_count": 500,
        "paragraphs_json": '["P1.", "P2."]',
        "content_hash": "h1",
        "extractor": "trafilatura",
        "raw_html_size": 12345,
        "fetched_at": datetime.now(UTC),
    }
    page = _restore_page_from_row(row)
    assert page.metadata.title == "My Article"
    assert page.metadata.author == "Jane Doe"
    assert page.paragraphs == ["P1.", "P2."]
    assert page.content_hash == "h1"
    assert page.extractor == "trafilatura"
    assert page.raw_html_size == 12345


# --- options_payload cache-key contract -----------------------------------


def test_options_payload_includes_website_fields() -> None:
    """page_id + content_hash + source_kind must enter the cache key.

    Without this, switching pages or re-analyzing edited content would
    silently return stale results from analysis_cache.
    """
    presets = get_presets("en")
    summary = presets["summary"]

    chat_opts = AnalysisOptions(preset="summary")
    web_opts = AnalysisOptions(
        preset="summary",
        website_page_id="pid",
        website_content_hash="hash",
        source_kind="website",
    )

    chat_payload = chat_opts.options_payload(summary)
    web_payload = web_opts.options_payload(summary)

    assert "website_page_id" in web_payload
    assert "website_content_hash" in web_payload
    assert web_payload["website_page_id"] == "pid"
    assert web_payload["website_content_hash"] == "hash"
    assert web_payload["source_kind"] == "website"

    assert chat_payload["website_page_id"] is None
    assert chat_payload["website_content_hash"] is None
    assert chat_payload["source_kind"] == "chat"

    # The two payloads must hash differently.
    import json

    assert json.dumps(chat_payload, sort_keys=True) != json.dumps(web_payload, sort_keys=True)


# --- website_report_path ---------------------------------------------------


def test_report_path_layout() -> None:
    p = website_report_path(
        page_id="abc1234567890def",
        title="My Article",
        domain="example.com",
        preset="website",
    )
    parts = p.parts
    assert parts[0] == "reports"
    assert parts[1] == "website"
    assert parts[2] == "example-com"
    assert "my-article" in parts[3]
    assert parts[3].endswith(".md")
    assert "website" in parts[3]


def test_report_path_falls_back_for_missing_title() -> None:
    p = website_report_path(
        page_id="abc1234567890def",
        title=None,
        domain="example.com",
        preset="website",
    )
    # Page slug should fall back to "page-<suffix>".
    assert "page-" in p.parts[3]


def test_report_path_unknown_domain() -> None:
    p = website_report_path(
        page_id="abc1234567890def",
        title="x",
        domain=None,
        preset="website",
    )
    assert p.parts[2] == "unknown-domain"
