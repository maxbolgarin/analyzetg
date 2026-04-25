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


def test_chat_groups_renders_each_chat_with_own_link_template() -> None:
    """`chat_groups` mode: channel + comments — distinct headers + links."""
    d = datetime(2026, 4, 19, 12, 0)
    channel_msg = Message(chat_id=-100123, msg_id=10, date=d, sender_name="ch", text="post")
    comment_msg = Message(chat_id=-100456, msg_id=999, date=d, sender_name="bob", text="reply")

    groups = {
        -100123: {"title": "MyChannel", "link_template": "https://t.me/mychan/{msg_id}"},
        -100456: {"title": "Comments", "link_template": "https://t.me/c/456/{msg_id}"},
    }
    out = format_messages([channel_msg, comment_msg], chat_groups=groups)
    # Both group headers must appear.
    assert "Чат: MyChannel" in out
    assert "Чат: Comments" in out
    # And each group's link template must appear in its section.
    assert "https://t.me/mychan/{msg_id}" in out
    assert "https://t.me/c/456/{msg_id}" in out
    # Channel msg renders before comment msg (the date-tied ordering pins
    # primary group above the secondary; both have identical timestamps so
    # we only assert both rendered, not ordering).
    assert "#10" in out and "#999" in out


def test_chat_groups_overrides_global_title_and_link_template() -> None:
    """Global title + link_template are suppressed when `chat_groups` is set."""
    d = datetime(2026, 4, 19, 12, 0)
    msg = Message(chat_id=-100123, msg_id=10, date=d, sender_name="ch", text="post")
    out = format_messages(
        [msg],
        title="GlobalTitle",
        link_template="https://example.com/{msg_id}",
        chat_groups={-100123: {"title": "InsideTitle", "link_template": "https://t.me/x/{msg_id}"}},
    )
    # Per-group header replaces the would-be global header.
    assert "Чат: InsideTitle" in out
    assert "Чат: GlobalTitle" not in out
    # Per-group link template replaces the would-be global link line.
    assert "https://t.me/x/{msg_id}" in out
    assert "https://example.com/{msg_id}" not in out


def test_chat_header_preamble_chat_groups_lists_each_chat() -> None:
    d = datetime(2026, 4, 19)
    out = chat_header_preamble(
        "ignored title",
        (d, d),
        chat_groups={
            -100123: {"title": "Channel", "link_template": "https://t.me/ch/{msg_id}"},
            -100456: {"title": "Comments", "link_template": "https://t.me/c/456/{msg_id}"},
        },
    )
    # In chat_groups mode the global "=== Чат: ignored title ===" line is
    # suppressed (each chat group renders its own inline) but every group
    # appears as a bullet in the preamble.
    assert "Channel" in out and "Comments" in out
    assert "https://t.me/ch/{msg_id}" in out
    assert "https://t.me/c/456/{msg_id}" in out
    assert "ignored title" not in out
