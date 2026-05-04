"""Repo streaming-shape regression tests.

Pre-prod review flagged that `iter_messages`, `untranscribed_media`, and
`cache_iter_full` previously called `cursor.fetchall()` — loading the
entire result set into memory before yielding the first row. On a chat
with millions of messages that's an OOM. These tests pin the new async-
iterator shape so a future refactor doesn't accidentally re-introduce
the materialization.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from unread.db.repo import Repo
from unread.models import Message


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "stream.sqlite")
    yield r
    await r.close()


def _is_async_iterator(obj: object) -> bool:
    """An async generator (or any object with __aiter__) qualifies."""
    return hasattr(obj, "__aiter__")


# --- iter_messages ------------------------------------------------------


async def test_iter_messages_returns_async_iterator(repo: Repo) -> None:
    """Direct call (no `await`) yields an async iterator, not a coroutine."""
    result = repo.iter_messages(1)
    assert _is_async_iterator(result), (
        f"iter_messages must be an async generator / iterator (got {type(result).__name__})"
    )
    # Drain to release the cursor.
    async for _ in result:
        pass


async def test_iter_messages_streams_in_order(repo: Repo) -> None:
    """50-row seed: the streamed iterator yields all rows in (date, msg_id) ASC order."""
    base = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    msgs = [Message(chat_id=1, msg_id=i, date=base.replace(microsecond=i), text=f"m{i}") for i in range(50)]
    await repo.upsert_messages(msgs)

    yielded: list[int] = []
    async for m in repo.iter_messages(1):
        yielded.append(m.msg_id)

    assert yielded == list(range(50))


async def test_iter_messages_list_materialization_works(repo: Repo) -> None:
    """The recommended `[m async for m in ...]` pattern matches `len(seed)`."""
    base = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    await repo.upsert_messages(
        [Message(chat_id=1, msg_id=i, date=base.replace(microsecond=i), text="x") for i in range(10)]
    )
    rows = [m async for m in repo.iter_messages(1)]
    assert len(rows) == 10


# --- untranscribed_media -----------------------------------------------


async def test_untranscribed_media_returns_async_iterator(repo: Repo) -> None:
    result = repo.untranscribed_media(chat_id=1)
    assert _is_async_iterator(result)
    async for _ in result:
        pass


async def test_untranscribed_media_streams_only_pending(repo: Repo) -> None:
    base = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    await repo.upsert_messages(
        [
            Message(
                chat_id=1,
                msg_id=1,
                date=base,
                text="voice 1",
                media_type="voice",
                media_doc_id=111,
            ),
            Message(
                chat_id=1,
                msg_id=2,
                date=base,
                text="voice 2",
                media_type="voice",
                media_doc_id=222,
            ),
            Message(chat_id=1, msg_id=3, date=base, text="plain text"),
        ]
    )
    # Mark one as already transcribed → it must not appear in the stream.
    await repo.set_message_transcript(chat_id=1, msg_id=1, transcript="hello", model="w")

    pending = [m async for m in repo.untranscribed_media(chat_id=1)]
    assert [m.msg_id for m in pending] == [2]


# --- cache_iter_full ----------------------------------------------------


async def test_cache_iter_full_returns_async_iterator(repo: Repo) -> None:
    result = repo.cache_iter_full()
    assert _is_async_iterator(result)
    async for _ in result:
        pass


async def test_cache_iter_full_streams_rows(repo: Repo) -> None:
    for i in range(5):
        await repo.cache_put(
            f"h{i}",
            preset="p",
            model="m",
            prompt_version="v1",
            result=f"body-{i}",
            prompt_tokens=1,
            cached_tokens=0,
            completion_tokens=1,
            cost_usd=0.0,
        )
    rows = [r async for r in repo.cache_iter_full(preset="p")]
    assert len(rows) == 5
    assert {r["batch_hash"] for r in rows} == {f"h{i}" for i in range(5)}
