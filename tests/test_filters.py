"""Tests for filter_messages and dedupe."""

from __future__ import annotations

from datetime import datetime

from analyzetg.analyzer.filters import FilterOpts, dedupe, filter_messages
from analyzetg.models import Message


def _m(
    msg_id: int, text: str | None = None, transcript: str | None = None, media: str | None = None
) -> Message:
    return Message(
        chat_id=1,
        msg_id=msg_id,
        date=datetime(2026, 4, 1, 12, msg_id),
        text=text,
        transcript=transcript,
        media_type=media,  # type: ignore[arg-type]
    )


def test_drops_empty() -> None:
    out = filter_messages([_m(1), _m(2, text="")], FilterOpts())
    assert out == []


def test_drops_short() -> None:
    out = filter_messages([_m(1, text="ok"), _m(2, text="hi there")], FilterOpts(min_msg_chars=3))
    assert len(out) == 1
    assert out[0].msg_id == 2


def test_text_only_drops_transcript_only() -> None:
    opts = FilterOpts(text_only=True, include_transcripts=False)
    out = filter_messages([_m(1, transcript="long audio transcript here")], opts)
    assert out == []


def test_includes_transcript_in_effective_text() -> None:
    opts = FilterOpts(include_transcripts=True, min_msg_chars=3)
    out = filter_messages([_m(1, text="", transcript="hello world")], opts)
    assert len(out) == 1


def test_dedupe_marks_duplicates() -> None:
    a = _m(1, text="same text!")
    b = _m(2, text="SAME   text!")  # normalized identical
    c = _m(3, text="different")
    out = dedupe([a, b, c])
    assert len(out) == 2
    assert out[0].duplicates == 1  # one extra copy
    assert out[1].duplicates == 0


def test_dedupe_preserves_order_of_first_occurrence() -> None:
    a = _m(1, text="foo")
    b = _m(2, text="bar")
    c = _m(3, text="foo")
    out = dedupe([a, b, c])
    assert [m.msg_id for m in out] == [1, 2]
    assert out[0].duplicates == 1
