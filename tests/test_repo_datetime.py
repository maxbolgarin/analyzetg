"""Regression tests for datetime-comparison bugs in repo.py."""

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


async def test_redact_old_messages_respects_retention(repo: Repo) -> None:
    now = datetime.now(UTC)
    fresh = Message(chat_id=1, msg_id=1, date=now - timedelta(days=1), text="keep me")
    stale = Message(chat_id=1, msg_id=2, date=now - timedelta(days=30), text="redact me")
    await repo.upsert_messages([fresh, stale])

    # Retention = 7 days → stale message's text nulled, fresh kept.
    redacted = await repo.redact_old_messages(retention_days=7)
    assert redacted == 1

    rows = await repo.iter_messages(1)
    texts = {r.msg_id: r.text for r in rows}
    assert texts[1] == "keep me"
    assert texts[2] is None


async def test_cache_purge_respects_age(repo: Repo) -> None:
    # Insert two cache rows; patch the created_at of one to be old.
    await repo.cache_put("hash_new", "summary", "gpt-4o", "v1", "result_new", 100, 0, 10, 0.001)
    await repo.cache_put("hash_old", "summary", "gpt-4o", "v1", "result_old", 100, 0, 10, 0.001)

    old_ts = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    await repo._conn.execute(
        "UPDATE analysis_cache SET created_at=? WHERE batch_hash='hash_old'",
        (old_ts,),
    )
    await repo._conn.commit()

    purged = await repo.cache_purge(older_than_days=30)
    assert purged == 1
    assert await repo.cache_get("hash_new") is not None
    assert await repo.cache_get("hash_old") is None
