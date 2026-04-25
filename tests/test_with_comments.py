"""Regression tests for the `--with-comments` (channel + linked discussion) path.

Covers:
- `core.pipeline._pull_linked_comments` returns ([], null meta) when the
  primary chat isn't a channel.
- Returns the linked chat's messages (and metadata) when the chat is a
  channel with `linked_chat_id` already on its row, using the date span
  of the primary messages as the comments time window.
- `analyzer.pipeline.AnalysisOptions.options_payload` includes
  `with_comments` so toggling it busts the cache.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from analyzetg.analyzer.pipeline import AnalysisOptions
from analyzetg.analyzer.prompts import PRESETS
from analyzetg.core.pipeline import _pull_linked_comments
from analyzetg.db.repo import Repo
from analyzetg.models import Message


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


def _msg(chat_id: int, msg_id: int, date: datetime, text: str = "x") -> Message:
    return Message(
        chat_id=chat_id,
        msg_id=msg_id,
        date=date,
        sender_name="user",
        text=text,
    )


async def test_pull_comments_skips_non_channel(repo: Repo) -> None:
    """A regular group / supergroup must short-circuit with no comments."""
    await repo.upsert_chat(-1001, "supergroup", title="Group")
    primary = [_msg(-1001, 1, datetime(2026, 4, 1, 12, 0, tzinfo=UTC))]

    meta, msgs = await _pull_linked_comments(
        client=AsyncMock(),
        repo=repo,
        chat_id=-1001,
        primary_msgs=primary,
        since_dt=None,
        until_dt=None,
    )
    assert msgs == []
    assert meta == {"chat_id": None, "title": None, "username": None, "internal_id": None}


async def test_pull_comments_returns_linked_msgs_using_primary_date_span(repo: Repo) -> None:
    """Channel with linked group: use min..max date of primary as the
    window, return what's in the local DB. No backfill in this test
    (client.get_messages.total is mocked away)."""
    channel_id = -1001000000001
    linked_id = -1001000000002

    await repo.upsert_chat(channel_id, "channel", title="MyChannel", linked_chat_id=linked_id)
    await repo.upsert_chat(linked_id, "supergroup", title="MyChannel-comments")

    # Pre-seed comments in the linked chat across a span. The pull should
    # return only those inside the [min(primary), max(primary)] window.
    early = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)  # before window
    in_a = datetime(2026, 4, 5, 12, 0, tzinfo=UTC)  # inside
    in_b = datetime(2026, 4, 5, 13, 0, tzinfo=UTC)  # inside
    late = datetime(2026, 4, 9, 8, 0, tzinfo=UTC)  # after window
    await repo.upsert_messages(
        [
            _msg(linked_id, 100, early, "early"),
            _msg(linked_id, 101, in_a, "ok-a"),
            _msg(linked_id, 102, in_b, "ok-b"),
            _msg(linked_id, 103, late, "late"),
        ]
    )

    primary_msgs = [
        _msg(channel_id, 10, datetime(2026, 4, 4, 0, 0, tzinfo=UTC), "post1"),
        _msg(channel_id, 11, datetime(2026, 4, 6, 0, 0, tzinfo=UTC), "post2"),
    ]

    # Mock the Telethon client. We do NOT want to hit the network; the
    # function calls `backfill(since_date=...)` which delegates to
    # client.iter_messages — patch backfill directly to be a no-op.
    import analyzetg.core.pipeline as _pipeline

    fake_client = AsyncMock()
    # `get_entity` is only called when a title is missing on the linked row,
    # which it isn't here (we set it via upsert_chat). The mock will absorb
    # an unexpected call without breaking the test.
    backfill_orig = _pipeline.backfill if hasattr(_pipeline, "backfill") else None  # noqa: F841

    async def _no_backfill(*_a, **_kw) -> int:  # type: ignore[no-untyped-def]
        return 0

    # `_pull_linked_comments` imports `backfill` lazily — patch where it's
    # referenced inside the function via the parent module.
    import analyzetg.tg.sync as _sync_mod

    real_backfill = _sync_mod.backfill
    _sync_mod.backfill = _no_backfill  # type: ignore[assignment]
    try:
        meta, msgs = await _pull_linked_comments(
            client=fake_client,
            repo=repo,
            chat_id=channel_id,
            primary_msgs=primary_msgs,
            since_dt=None,
            until_dt=None,
        )
    finally:
        _sync_mod.backfill = real_backfill  # type: ignore[assignment]

    assert meta["chat_id"] == linked_id
    assert meta["title"] == "MyChannel-comments"
    # Only the two in-window messages should survive the date filter.
    msg_ids = sorted(m.msg_id for m in msgs)
    assert msg_ids == [101, 102]


def test_options_payload_includes_with_comments() -> None:
    """Toggling `with_comments` MUST appear in the cache key payload so a
    re-run with the flag flipped doesn't return a stale answer."""
    preset = PRESETS["summary"]
    payload_off = AnalysisOptions(preset="summary", with_comments=False).options_payload(preset)
    payload_on = AnalysisOptions(
        preset="summary", with_comments=True, comments_chat_id=-100999
    ).options_payload(preset)
    assert payload_off["with_comments"] is False
    assert payload_on["with_comments"] is True
    assert payload_on["comments_chat_id"] == -100999
    # And the two payloads differ on the relevant key.
    assert payload_off != payload_on
