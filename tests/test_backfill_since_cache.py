"""Cache-aware fast-path for the `--last-days N` / `--since DATE` backfill.

Real-world report: a channel with ~3 000 Telegram messages in the last
7 days but only ~300 locally synced was re-walking the entire window
on every `unread <ref>` invocation. Cause: `_pull_history`'s `since_dt`
branch dropped to `backfill(since_date=…)` without consulting
`local_max`, so Telethon's `iter_messages(offset_date=…, reverse=True)`
re-emitted every cached message.

The fix: when `since_dt` is set AND we already have at least one
message at-or-after that date locally, take the `from_msg_id` branch
anchored at `local_max + 1`. Telethon's known `min_id` + `offset_date`
interaction bug (see `sync.py:292`) is sidestepped — we use ONLY
`min_id`. When local_max lies BEFORE the window (cold start, or huge
gap), fall through to the original `since_date` path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_get_max_msg_id_respects_since_date(tmp_path: Path):
    """The repo helper must honor a date floor — that's the signal
    `_pull_history` uses to decide between fast-path and cold-walk."""
    from unread.db.repo import open_repo
    from unread.models import Message

    db_path = tmp_path / "data.sqlite"
    async with open_repo(db_path) as repo:
        now = datetime(2026, 5, 22, tzinfo=UTC)
        chat_id = -100
        # Three messages spanning a window: one a month ago, one in
        # the last 7 days, one yesterday.
        messages = [
            Message(chat_id=chat_id, msg_id=10, date=now - timedelta(days=30), text="old"),
            Message(chat_id=chat_id, msg_id=20, date=now - timedelta(days=3), text="recent"),
            Message(chat_id=chat_id, msg_id=30, date=now - timedelta(days=1), text="newest"),
        ]
        await repo.upsert_messages(messages)

        # Window starts 7 days ago — should see msgs 20 and 30, max = 30.
        cutoff = now - timedelta(days=7)
        got = await repo.get_max_msg_id(chat_id, since_date=cutoff)
        assert got == 30

        # Window starts 100 days ago — should see all three, max = 30.
        got_all = await repo.get_max_msg_id(chat_id, since_date=now - timedelta(days=100))
        assert got_all == 30

        # Window starts in the future — no messages match, returns None.
        got_none = await repo.get_max_msg_id(chat_id, since_date=now + timedelta(days=1))
        assert got_none is None

        # No date filter (legacy behavior) — max over everything.
        assert await repo.get_max_msg_id(chat_id) == 30


@pytest.mark.asyncio
async def test_pull_history_fast_paths_through_local_max(tmp_path: Path, monkeypatch):
    """When `since_dt` is set AND local_max sits inside the window,
    `_pull_history` must call backfill with `from_msg_id=local_max+1`,
    NOT with `since_date=since_dt`. Asserted by stubbing `backfill` and
    inspecting the kwargs it sees."""
    from unread.core import pipeline as pl
    from unread.db.repo import open_repo
    from unread.models import Message

    db_path = tmp_path / "data.sqlite"
    chat_id = -200
    now = datetime(2026, 5, 22, tzinfo=UTC)

    async with open_repo(db_path) as repo:
        # Local DB already has a message 3 days ago — well inside a 7-day window.
        await repo.upsert_messages(
            [Message(chat_id=chat_id, msg_id=50, date=now - timedelta(days=3), text="cached")]
        )

        captured: dict = {}

        async def fake_backfill(client, repo, **kwargs):
            captured.update(kwargs)
            return 0

        import unread.tg.sync as sync_mod

        monkeypatch.setattr(sync_mod, "backfill", fake_backfill)

        await pl._pull_history(
            client=SimpleNamespace(),
            repo=repo,
            chat_id=chat_id,
            thread_id=0,
            start_msg_id=None,
            since_dt=now - timedelta(days=7),
        )

        # Fast path taken: from_msg_id anchored on local_max+1, no since_date.
        assert captured.get("from_msg_id") == 51, f"expected from_msg_id=51 (local_max+1), got {captured!r}"
        assert captured.get("since_date") is None, (
            f"since_date must NOT be set when local_max covers the window; got {captured!r}"
        )
        assert captured.get("direction") == "forward"


@pytest.mark.asyncio
async def test_pull_history_cold_walks_window_when_no_cached_messages_in_range(tmp_path: Path, monkeypatch):
    """When local_max is OUTSIDE the time window (or absent), the
    `since_date` cold-walk path stays. We need to discover the start of
    the window, and `min_id` alone can't do that."""
    from unread.core import pipeline as pl
    from unread.db.repo import open_repo
    from unread.models import Message

    db_path = tmp_path / "data.sqlite"
    chat_id = -201
    now = datetime(2026, 5, 22, tzinfo=UTC)

    async with open_repo(db_path) as repo:
        # Only ancient messages locally — none in the 7-day window.
        await repo.upsert_messages(
            [Message(chat_id=chat_id, msg_id=5, date=now - timedelta(days=60), text="ancient")]
        )

        captured: dict = {}

        async def fake_backfill(client, repo, **kwargs):
            captured.update(kwargs)
            return 0

        import unread.tg.sync as sync_mod

        monkeypatch.setattr(sync_mod, "backfill", fake_backfill)

        cutoff = now - timedelta(days=7)
        await pl._pull_history(
            client=SimpleNamespace(),
            repo=repo,
            chat_id=chat_id,
            thread_id=0,
            start_msg_id=None,
            since_dt=cutoff,
        )

        assert captured.get("since_date") == cutoff, f"cold-walk path must pass since_date; got {captured!r}"
        assert captured.get("from_msg_id") is None


@pytest.mark.asyncio
async def test_pull_history_cold_walks_when_no_messages_at_all(tmp_path: Path, monkeypatch):
    """Empty local DB — falls back to the since_date cold walk."""
    from unread.core import pipeline as pl
    from unread.db.repo import open_repo

    db_path = tmp_path / "data.sqlite"
    chat_id = -202
    now = datetime(2026, 5, 22, tzinfo=UTC)

    async with open_repo(db_path) as repo:
        captured: dict = {}

        async def fake_backfill(client, repo, **kwargs):
            captured.update(kwargs)
            return 0

        import unread.tg.sync as sync_mod

        monkeypatch.setattr(sync_mod, "backfill", fake_backfill)

        cutoff = now - timedelta(days=7)
        await pl._pull_history(
            client=SimpleNamespace(),
            repo=repo,
            chat_id=chat_id,
            thread_id=0,
            start_msg_id=None,
            since_dt=cutoff,
        )

        assert captured.get("since_date") == cutoff
        assert captured.get("from_msg_id") is None
