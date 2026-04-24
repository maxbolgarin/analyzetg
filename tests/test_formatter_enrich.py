"""Formatter + filter behavior for enriched messages.

Covers how image descriptions, extracted doc text, and link summaries flow
into the rendered lines the analyzer sees — and how the filter treats a
photo-only message with a description (should not be dropped).
"""

from __future__ import annotations

from datetime import datetime

from analyzetg.analyzer.filters import FilterOpts, effective_text, filter_messages
from analyzetg.analyzer.formatter import format_messages
from analyzetg.models import Message


def _msg(**kw) -> Message:
    base = {
        "chat_id": 1,
        "msg_id": 10,
        "date": datetime(2026, 1, 1, 12, 0),
        "sender_name": "Alice",
        "sender_id": 100,
    }
    base.update(kw)
    return Message(**base)


def test_formatter_inlines_image_description():
    m = _msg(text=None, media_type="photo", media_doc_id=99, image_description="a red cube")
    out = format_messages([m])
    assert "[image: a red cube]" in out
    # With a description we don't ALSO emit the bare [photo] tag.
    assert "[photo]" not in out


def test_formatter_inlines_doc_extract():
    m = _msg(text=None, media_type="doc", media_doc_id=99, extracted_text="hello world pdf")
    out = format_messages([m])
    assert "[doc: hello world pdf]" in out


def test_formatter_renders_link_summaries():
    m = _msg(
        text="check https://example.com/a",
        link_summaries=[("https://example.com/a", "A news article about X.")],
    )
    out = format_messages([m])
    assert "↳ https://example.com/a: A news article about X." in out


def test_formatter_photo_without_description_shows_photo_tag():
    m = _msg(text="caption!", media_type="photo", media_doc_id=99)
    out = format_messages([m])
    assert "[photo]" in out
    assert "caption!" in out


def test_filter_keeps_photo_with_description():
    # Before enrichment a photo-only message would be dropped; with a
    # description it must survive filtering.
    m = _msg(text=None, media_type="photo", media_doc_id=99, image_description="a red cube")
    out = filter_messages([m], FilterOpts(min_msg_chars=3, include_transcripts=True))
    assert out == [m]


def test_filter_drops_media_only_when_text_only():
    m = _msg(text=None, media_type="photo", media_doc_id=99, image_description="a red cube")
    out = filter_messages(
        [m],
        FilterOpts(min_msg_chars=3, include_transcripts=True, text_only=True),
    )
    assert out == []  # --text-only respected even when enrichment is present.


def test_effective_text_includes_link_summaries():
    m = _msg(
        text="see link",
        link_summaries=[("https://example.com/a", "About X")],
    )
    body = effective_text(m, FilterOpts(include_transcripts=True))
    assert "see link" in body
    assert "About X" in body
