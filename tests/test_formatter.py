"""Tests for analyzer.formatter."""

from __future__ import annotations

from datetime import datetime

from analyzetg.analyzer.formatter import (
    build_link_template,
    chat_header_preamble,
    format_messages,
)
from analyzetg.models import Message


def _m(msg_id: int, date: datetime, **kw) -> Message:
    base = {"chat_id": 1, "msg_id": msg_id, "date": date}
    base.update(kw)
    return Message(**base)


def test_empty_returns_empty_string() -> None:
    assert format_messages([]) == ""


def test_single_day_uses_hhmm_only() -> None:
    d = datetime(2026, 4, 19, 12, 34)
    msgs = [_m(1, d, sender_name="Alice", text="hello")]
    out = format_messages(msgs)
    assert "[12:34 #1]" in out
    assert "04-19" not in out


def test_message_line_includes_msg_id() -> None:
    d = datetime(2026, 4, 19, 12, 0)
    out = format_messages([_m(54321, d, sender_name="a", text="x")])
    assert "#54321" in out


def test_link_template_appears_in_header_when_given() -> None:
    d = datetime(2026, 4, 19, 12, 0)
    msgs = [_m(1, d, sender_name="a", text="x")]
    tmpl = "https://t.me/viberadar/{msg_id}"
    out = format_messages(msgs, link_template=tmpl)
    assert tmpl in out


def test_chat_header_preamble_carries_link_template() -> None:
    d = datetime(2026, 4, 19)
    tmpl = "https://t.me/c/1234567890/{msg_id}"
    out = chat_header_preamble("Chat", (d, d), link_template=tmpl)
    assert tmpl in out


def test_build_link_template_variants() -> None:
    # Public channel.
    assert (
        build_link_template(chat_username="viberadar", chat_internal_id=None)
        == "https://t.me/viberadar/{msg_id}"
    )
    # Private chat → /c/ form.
    assert (
        build_link_template(chat_username=None, chat_internal_id=1234567890)
        == "https://t.me/c/1234567890/{msg_id}"
    )
    # Forum topic — thread_id injected.
    assert (
        build_link_template(chat_username="forum", chat_internal_id=None, thread_id=7)
        == "https://t.me/forum/7/{msg_id}"
    )
    # No info → None (prompt will omit link instructions).
    assert build_link_template(chat_username=None, chat_internal_id=None) is None


def test_multi_day_same_year() -> None:
    msgs = [
        _m(1, datetime(2026, 4, 15, 10, 0), sender_name="a", text="one"),
        _m(2, datetime(2026, 4, 17, 11, 0), sender_name="b", text="two"),
    ]
    out = format_messages(msgs)
    assert "04-15" in out and "04-17" in out


def test_voice_transcript_tag() -> None:
    d = datetime(2026, 4, 19, 12, 0)
    m = _m(
        1,
        d,
        sender_name="Alice",
        transcript="hello world",
        media_type="voice",
        media_duration=23,
    )
    out = format_messages([m])
    assert "[voice 0:23]" in out
    assert "hello world" in out


def test_reply_marker_resolved() -> None:
    d1 = datetime(2026, 4, 19, 12, 0)
    a = _m(1, d1, sender_name="alice", text="hi")
    b = _m(2, d1, sender_name="bob", text="hello", reply_to=1)
    out = format_messages([a, b])
    assert "↩alice" in out


def test_duplicate_marker() -> None:
    d = datetime(2026, 4, 19, 12, 0)
    m = _m(
        1,
        d,
        sender_name="alice",
        text="news",
    )
    m.duplicates = 4
    out = format_messages([m])
    assert "[×5]" in out


def test_title_and_period_header() -> None:
    d = datetime(2026, 4, 19, 12, 0)
    m = _m(1, d, sender_name="alice", text="hi")
    out = format_messages([m], title="Test chat", period=(d, d))
    assert "Чат: Test chat" in out
    assert "Период:" in out
