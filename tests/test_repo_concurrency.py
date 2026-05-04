"""Concurrency invariants for `Repo` writes.

The Repo holds a single aiosqlite connection across coroutines. Multi-
statement writes go through ``Repo._transaction()`` which acquires
``_write_lock`` and wraps the body in ``BEGIN IMMEDIATE`` / ``COMMIT``.
Two simultaneous writers must therefore serialize cleanly without any
``database is locked`` flapping, and a reader observed mid-flight must
see either the pre-state or the post-state but never a partial mix.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from unread.db.repo import Repo
from unread.models import Message, Subscription


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


async def test_transaction_commits_on_success(repo: Repo) -> None:
    """Sanity: the helper actually commits when the body returns cleanly."""
    sub = Subscription(
        chat_id=42,
        thread_id=0,
        title="t",
        source_kind="chat",
        enabled=True,
        added_at=datetime.now(UTC),
    )
    await repo.upsert_subscription(sub)
    await repo.remove_subscription(42, 0)
    assert await repo.get_subscription(42, 0) is None


async def test_transaction_rolls_back_on_exception(repo: Repo) -> None:
    """A raise inside the body must rollback both pending writes."""
    sub = Subscription(
        chat_id=99,
        thread_id=0,
        title="t",
        source_kind="chat",
        enabled=True,
        added_at=datetime.now(UTC),
    )
    await repo.upsert_subscription(sub)

    boom = RuntimeError("synthetic")
    with pytest.raises(RuntimeError, match="synthetic"):
        async with repo._transaction():
            await repo._conn.execute("DELETE FROM subscriptions WHERE chat_id=?", (99,))
            raise boom

    # Rollback restored the row.
    assert await repo.get_subscription(99, 0) is not None


async def test_concurrent_remove_subscription_serializes(repo: Repo) -> None:
    """Twenty concurrent multi-statement removes must not raise SQLITE_BUSY.

    Each `remove_subscription` does 2-3 writes inside a single
    ``BEGIN IMMEDIATE``; without the per-Repo write lock, two
    coroutines would race for the writer lock and one would lose
    with "database is locked" despite the busy_timeout.
    """
    now = datetime.now(UTC)
    subs = [
        Subscription(
            chat_id=i,
            thread_id=0,
            title=f"chat-{i}",
            source_kind="chat",
            enabled=True,
            added_at=now,
        )
        for i in range(20)
    ]
    for s in subs:
        await repo.upsert_subscription(s)
        await repo.upsert_messages([Message(chat_id=s.chat_id, msg_id=1, date=now, text="x")])
    # Fire all removals concurrently. Any one of them raising
    # OperationalError("database is locked") would fail this test.
    await asyncio.gather(*[repo.remove_subscription(s.chat_id, 0, purge_messages=True) for s in subs])
    for s in subs:
        assert await repo.get_subscription(s.chat_id, 0) is None
