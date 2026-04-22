"""Tests for Repo.iter_messages() msg_id lower-bound filter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from analyzetg.db.repo import Repo
from analyzetg.models import Message


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


async def test_iter_messages_min_msg_id_filters_strictly(repo: Repo) -> None:
    now = datetime.now(UTC)
    msgs = [
        Message(chat_id=1, msg_id=10, date=now - timedelta(minutes=3), text="old"),
        Message(chat_id=1, msg_id=20, date=now - timedelta(minutes=2), text="at marker"),
        Message(chat_id=1, msg_id=21, date=now - timedelta(minutes=1), text="after marker"),
        Message(chat_id=1, msg_id=30, date=now, text="newest"),
    ]
    await repo.upsert_messages(msgs)

    # min_msg_id is exclusive: msg_id > 20 keeps 21 and 30 only.
    res = await repo.iter_messages(1, min_msg_id=20)
    assert [m.msg_id for m in res] == [21, 30]

    # None means no lower bound.
    all_rows = await repo.iter_messages(1, min_msg_id=None)
    assert [m.msg_id for m in all_rows] == [10, 20, 21, 30]


async def test_get_max_msg_id(repo: Repo) -> None:
    now = datetime.now(UTC)
    await repo.upsert_messages(
        [
            Message(chat_id=1, msg_id=10, date=now, text="a"),
            Message(chat_id=1, msg_id=25, date=now, text="b"),
            Message(chat_id=1, msg_id=40, date=now, text="c"),
            Message(chat_id=2, msg_id=999, date=now, text="other chat"),
        ]
    )
    # Overall max for chat 1.
    assert await repo.get_max_msg_id(1) == 40
    # With lower bound: exclusive, returns max of remaining rows.
    assert await repo.get_max_msg_id(1, min_msg_id=25) == 40
    # Bound above all rows → None.
    assert await repo.get_max_msg_id(1, min_msg_id=100) is None
    # Empty chat → None.
    assert await repo.get_max_msg_id(999) is None


async def test_iter_messages_min_msg_id_combines_with_time_window(repo: Repo) -> None:
    now = datetime.now(UTC)
    msgs = [
        Message(chat_id=1, msg_id=10, date=now - timedelta(days=5), text="old and before marker"),
        Message(chat_id=1, msg_id=20, date=now - timedelta(days=5), text="old but after marker"),
        Message(chat_id=1, msg_id=30, date=now - timedelta(hours=1), text="fresh and after marker"),
    ]
    await repo.upsert_messages(msgs)

    since = now - timedelta(days=1)
    res = await repo.iter_messages(1, since=since, min_msg_id=10)
    # msg_id=20 filtered out by since; msg_id=30 kept (after marker AND recent).
    assert [m.msg_id for m in res] == [30]
