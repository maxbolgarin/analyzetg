"""Pin Telethon `iter_messages` kwargs across (forward, since_date, from_msg_id).

Pre-prod review (regression watch): an earlier release walked the entire
chat history on `--refresh --last-days 7` because `iter_messages` was
called with both `min_id` and `offset_date` under `reverse=True` —
Telethon silently drops the date bound in that combination. The fix in
`tg/sync.py:289-309` says: when a date bound is set, it wins; otherwise
fall through to msg-id anchoring; otherwise full history.

These tests pin the exact iter_messages kwargs for every input
combination so a future refactor can't reintroduce the bug.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _FakeClient:
    """Captures the kwargs passed to iter_messages without yielding any rows.

    Async-iter contract: returns an empty list so the `async for` exits
    immediately. The test asserts on `last_kwargs` after the call.
    """

    def __init__(self):
        self.last_kwargs: dict | None = None

    def iter_messages(self, **kwargs):
        self.last_kwargs = kwargs

        async def _empty():
            return
            yield  # unreachable — makes this an async generator

        return _empty()

    async def get_messages(self, *_a, **_kw):  # used by the bar-estimate
        return []


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """Minimal Repo stub — backfill only needs get_subscription + put_message."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path / "home"))
    from unread.config import reset_settings

    reset_settings()

    repo = SimpleNamespace()
    repo.get_subscription = AsyncMock(return_value=None)
    repo.put_subscription = AsyncMock()
    repo.put_message = AsyncMock()
    repo.set_chat_kind = AsyncMock()
    repo.update_sync_state = AsyncMock()
    return repo


async def test_iter_kwargs_date_bound_wins_over_msg_id(fake_repo):
    """When BOTH since_date and from_msg_id are passed with direction=forward,
    the date bound wins (no min_id / offset_id in iter_kwargs)."""
    from unread.tg.sync import backfill

    client = _FakeClient()
    since = datetime(2026, 5, 1, tzinfo=UTC)

    await backfill(
        client,  # type: ignore[arg-type]
        fake_repo,
        chat_id=-1001234,
        from_msg_id=99999,
        since_date=since,
        direction="forward",
    )

    kw = client.last_kwargs or {}
    assert kw.get("reverse") is True
    assert kw.get("offset_date") == since
    # Critical regression pin: when the date wins, min_id MUST NOT be set
    # (Telethon would otherwise drop the date silently).
    assert "min_id" not in kw
    assert "offset_id" not in kw


async def test_iter_kwargs_msg_id_forward(fake_repo):
    """from_msg_id alone, direction=forward → reverse=True + min_id=N-1."""
    from unread.tg.sync import backfill

    client = _FakeClient()

    await backfill(
        client,  # type: ignore[arg-type]
        fake_repo,
        chat_id=-1001234,
        from_msg_id=500,
        since_date=None,
        direction="forward",
    )

    kw = client.last_kwargs or {}
    assert kw.get("reverse") is True
    assert kw.get("min_id") == 499
    assert "offset_date" not in kw


async def test_iter_kwargs_msg_id_back(fake_repo):
    """from_msg_id alone, direction=back → reverse=False + offset_id=N."""
    from unread.tg.sync import backfill

    client = _FakeClient()

    await backfill(
        client,  # type: ignore[arg-type]
        fake_repo,
        chat_id=-1001234,
        from_msg_id=500,
        since_date=None,
        direction="back",
    )

    kw = client.last_kwargs or {}
    assert kw.get("reverse") is False
    assert kw.get("offset_id") == 500
    assert "offset_date" not in kw
    assert "min_id" not in kw


async def test_iter_kwargs_full_history_forward(fake_repo):
    """No bounds, direction=forward → reverse=True only."""
    from unread.tg.sync import backfill

    client = _FakeClient()

    await backfill(
        client,  # type: ignore[arg-type]
        fake_repo,
        chat_id=-1001234,
        from_msg_id=None,
        since_date=None,
        direction="forward",
    )

    kw = client.last_kwargs or {}
    assert kw.get("reverse") is True
    assert "min_id" not in kw
    assert "offset_id" not in kw
    assert "offset_date" not in kw


async def test_iter_kwargs_thread_id_passes_reply_to(fake_repo):
    """thread_id → iter_kwargs["reply_to"] regardless of bound shape."""
    from unread.tg.sync import backfill

    client = _FakeClient()

    await backfill(
        client,  # type: ignore[arg-type]
        fake_repo,
        chat_id=-1001234,
        thread_id=42,
        from_msg_id=None,
        since_date=None,
        direction="forward",
    )

    kw = client.last_kwargs or {}
    assert kw.get("reply_to") == 42


async def test_iter_kwargs_min_id_clamps_to_zero(fake_repo):
    """from_msg_id=0 → min_id=max(0-1, 0) = 0 (not -1)."""
    from unread.tg.sync import backfill

    client = _FakeClient()

    await backfill(
        client,  # type: ignore[arg-type]
        fake_repo,
        chat_id=-1001234,
        from_msg_id=0,
        since_date=None,
        direction="forward",
    )

    kw = client.last_kwargs or {}
    assert kw.get("min_id") == 0
