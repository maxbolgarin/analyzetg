"""Regression tests for analysis cache key materiality."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from unread.analyzer import pipeline
from unread.analyzer.openai_client import ChatResult
from unread.analyzer.pipeline import AnalysisOptions, run_analysis
from unread.db.repo import Repo
from unread.models import Message


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


async def _run_hash(repo: Repo, msg: Message, *, title: str = "Chat") -> str:
    async def fake_chat_complete(*args, **kwargs) -> ChatResult:
        return ChatResult(
            text="ok",
            prompt_tokens=1,
            cached_tokens=0,
            completion_tokens=1,
            cost_usd=0.0,
        )

    def fake_make_client() -> object:
        return object()

    opts = AnalysisOptions(preset="summary", use_cache=False)
    old_chat_complete = pipeline.chat_complete
    old_make_client = pipeline.make_client
    pipeline.chat_complete = fake_chat_complete
    pipeline.make_client = fake_make_client
    try:
        result = await run_analysis(
            repo=repo,
            chat_id=msg.chat_id,
            thread_id=None,
            title=title,
            opts=opts,
            messages=[msg],
        )
    finally:
        pipeline.chat_complete = old_chat_complete
        pipeline.make_client = old_make_client
    return result.batch_hashes[0]


async def test_cache_key_changes_when_rendered_message_body_changes(repo: Repo) -> None:
    base = {
        "chat_id": 1,
        "msg_id": 10,
        "date": datetime(2026, 4, 24, 12, 0, tzinfo=UTC),
        "sender_name": "alice",
    }
    first = await _run_hash(repo, Message(**base, text="first body"))
    second = await _run_hash(repo, Message(**base, text="changed body"))
    assert first != second


async def test_cache_key_changes_when_static_chat_context_changes(repo: Repo) -> None:
    msg = Message(
        chat_id=1,
        msg_id=10,
        date=datetime(2026, 4, 24, 12, 0, tzinfo=UTC),
        sender_name="alice",
        text="same body",
    )
    first = await _run_hash(repo, msg, title="Old title")
    second = await _run_hash(repo, msg, title="New title")
    assert first != second
