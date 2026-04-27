"""Tests for analyzer.chunker."""

from __future__ import annotations

from datetime import datetime, timedelta

from unread.analyzer.chunker import build_chunks, model_context_window
from unread.models import Message


def _msg(i: int, text: str, date: datetime) -> Message:
    return Message(chat_id=1, msg_id=i, date=date, sender_name="alice", text=text)


def test_model_context_fallback() -> None:
    assert model_context_window("unknown-model") >= 8000


def test_single_chunk_when_budget_large() -> None:
    d = datetime(2026, 4, 1, 12, 0)
    msgs = [_msg(i, f"m{i}", d + timedelta(seconds=i)) for i in range(20)]
    chunks = build_chunks(
        msgs,
        model="gpt-4o",
        system_prompt="sys",
        user_overhead="ovh",
        output_budget=1000,
    )
    assert len(chunks) == 1
    assert [m.msg_id for m in chunks[0].messages] == list(range(20))


def test_soft_break_on_long_pause() -> None:
    d = datetime(2026, 4, 1, 12, 0)
    first = [_msg(i, "some meaningful text " * 100, d + timedelta(seconds=i)) for i in range(30)]
    # After a 2-hour gap, start new chunk even though budget allows it
    gap_start = d + timedelta(hours=3)
    second = [_msg(30 + i, "more text " * 100, gap_start + timedelta(seconds=i)) for i in range(5)]
    chunks = build_chunks(
        first + second,
        model="gpt-4o-mini",
        system_prompt="sys",
        user_overhead="ovh",
        output_budget=1000,
        soft_break_minutes=30,
        safety_margin=0,
    )
    assert len(chunks) >= 2


def test_all_messages_are_kept() -> None:
    d = datetime(2026, 4, 1, 12, 0)
    msgs = [_msg(i, f"msg {i} " * 5, d + timedelta(seconds=i)) for i in range(50)]
    chunks = build_chunks(
        msgs,
        model="gpt-4o",
        system_prompt="sys",
        user_overhead="ovh",
        output_budget=1000,
    )
    seen = {m.msg_id for c in chunks for m in c.messages}
    assert seen == set(range(50))
