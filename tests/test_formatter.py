"""Tests for analyzer.formatter."""

from __future__ import annotations

from datetime import datetime

from analyzetg.analyzer.formatter import format_messages
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
    assert "[12:34]" in out
    assert "04-19" not in out


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
        1, d, sender_name="Alice", transcript="hello world",
        media_type="voice", media_duration=23,
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
    m = _m(1, d, sender_name="alice", text="news", )
    m.duplicates = 4
    out = format_messages([m])
    assert "[×5]" in out


def test_title_and_period_header() -> None:
    d = datetime(2026, 4, 19, 12, 0)
    m = _m(1, d, sender_name="alice", text="hi")
    out = format_messages([m], title="Test chat", period=(d, d))
    assert "Чат: Test chat" in out
    assert "Период:" in out
