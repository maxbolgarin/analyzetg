"""Tests for unread.core.run.PreparedRun.

These pin the dataclass's shape so a consumer (analyze / dump /
download-media) never wakes up to find a field it depended on removed.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from unread.config import get_settings, reset_settings
from unread.core.run import PreparedRun
from unread.db.repo import Repo
from unread.enrich.base import EnrichOpts
from unread.models import Message


def test_prepared_run_carries_all_consumer_contracts():
    # Every field a consumer might read. Adding a new field is fine —
    # removing or renaming one needs both this test and the consumers
    # to change in the same commit.
    expected = {
        "chat_id",
        "thread_id",
        "chat_title",
        "thread_title",
        "chat_username",
        "chat_internal_id",
        "messages",
        "period",
        "topic_titles",
        "topic_markers",
        "raw_msg_count",
        "enrich_stats",
        "mark_read_fn",
        "client",
        "repo",
        "settings",
        "comments_chat_id",
        "comments_chat_title",
        "comments_chat_username",
        "comments_chat_internal_id",
    }
    actual = {f.name for f in fields(PreparedRun)}
    assert actual == expected, f"missing {expected - actual}, extra {actual - expected}"


def test_prepared_run_slots_enforced():
    # Accidentally adding an attribute outside the declared set should
    # fail fast — slotted dataclass is how we get that guarantee.
    p = PreparedRun(
        chat_id=1,
        thread_id=None,
        chat_title="t",
        thread_title=None,
        chat_username=None,
        chat_internal_id=None,
        messages=[],
        period=(None, None),
        topic_titles=None,
        topic_markers=None,
        raw_msg_count=0,
        enrich_stats=None,
        mark_read_fn=None,
        client=None,
        repo=None,
        settings=None,
    )
    assert p.messages == []
    try:
        p.some_new_attribute = "nope"  # type: ignore[attr-defined]
    except AttributeError:
        return
    raise AssertionError("PreparedRun should be slotted (no dynamic attrs)")


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch, tmp_path):
    reset_settings()
    monkeypatch.chdir(tmp_path)
    yield
    reset_settings()


@pytest.fixture
async def repo(tmp_path: Path):
    r = await Repo.open(tmp_path / "t.sqlite")
    try:
        yield r
    finally:
        await r.close()


async def test_prepare_chat_run_returns_full_shape(repo, monkeypatch):
    """End-to-end smoke test: canned DB + stubbed Telethon client →
    prepare_chat_run returns a PreparedRun with every field populated
    as expected. Catches regressions where a consumer-required field
    stops being set during prep."""
    from unread.core.pipeline import prepare_chat_run

    await repo.upsert_messages(
        [
            Message(
                chat_id=-100,
                msg_id=10,
                date=datetime(2026, 4, 24, 12, 0),
                thread_id=None,
                sender_name="Alice",
                text="hello there",
            ),
            Message(
                chat_id=-100,
                msg_id=11,
                date=datetime(2026, 4, 24, 12, 1),
                thread_id=None,
                sender_name="Bob",
                text="good morning",
            ),
        ]
    )

    # Short-circuit the network: patch backfill to a no-op.
    backfill_calls = []

    async def _no_backfill(*args, **kwargs):
        backfill_calls.append(kwargs)
        return 0

    monkeypatch.setattr("unread.tg.sync.backfill", _no_backfill)

    client = MagicMock()
    client.get_messages = AsyncMock()
    settings = get_settings()

    prepared = await prepare_chat_run(
        client=client,
        repo=repo,
        settings=settings,
        chat_id=-100,
        thread_id=None,
        chat_title="Test Chat",
        since_dt=datetime(2026, 4, 24),
        until_dt=datetime(2026, 4, 25),
        enrich_opts=EnrichOpts(),
        mark_read=False,
    )

    assert prepared.chat_id == -100
    assert prepared.chat_title == "Test Chat"
    assert prepared.thread_id is None
    assert len(prepared.messages) == 2
    assert prepared.raw_msg_count == 2
    assert prepared.period == (datetime(2026, 4, 24), datetime(2026, 4, 25))
    assert prepared.topic_titles is None
    assert prepared.topic_markers is None
    assert prepared.enrich_stats is None
    assert prepared.mark_read_fn is None
    assert prepared.client is client
    assert prepared.repo is repo
    assert prepared.settings is settings


async def test_prepare_chat_run_skip_filter_preserves_media_only_messages(repo, monkeypatch):
    """Regression: download-media needs the raw message set (including
    media-only rows with no text / transcript). Without skip_filter=True,
    `filter_messages` drops every row where effective_text is empty,
    leaving save_raw_media with zero candidates — which is exactly what
    download-media would hit before the skip_filter knob existed.
    """
    from unread.core.pipeline import prepare_chat_run

    await repo.upsert_messages(
        [
            Message(
                chat_id=-100,
                msg_id=5,
                date=datetime(2026, 4, 24, 12, 0),
                thread_id=None,
                sender_name="Alice",
                text=None,
                media_type="photo",
                media_doc_id=500,
            ),
            Message(
                chat_id=-100,
                msg_id=6,
                date=datetime(2026, 4, 24, 12, 1),
                thread_id=None,
                sender_name="Bob",
                text=None,
                media_type="voice",
                media_doc_id=600,
                media_duration=4,
            ),
        ]
    )

    async def _no_backfill(*args, **kwargs):
        return 0

    monkeypatch.setattr("unread.tg.sync.backfill", _no_backfill)

    client = MagicMock()
    client.get_messages = AsyncMock()
    settings = get_settings()

    prepared_filtered = await prepare_chat_run(
        client=client,
        repo=repo,
        settings=settings,
        chat_id=-100,
        thread_id=None,
        chat_title="T",
        since_dt=datetime(2026, 4, 24),
        until_dt=datetime(2026, 4, 25),
        enrich_opts=EnrichOpts(),
        include_transcripts=False,
        mark_read=False,
    )
    # Default path filters media-only (empty body) → zero messages.
    assert prepared_filtered.messages == []
    assert prepared_filtered.raw_msg_count == 2  # pre-filter count preserved

    prepared_raw = await prepare_chat_run(
        client=client,
        repo=repo,
        settings=settings,
        chat_id=-100,
        thread_id=None,
        chat_title="T",
        since_dt=datetime(2026, 4, 24),
        until_dt=datetime(2026, 4, 25),
        enrich_opts=EnrichOpts(),
        include_transcripts=False,
        mark_read=False,
        skip_filter=True,
    )
    # skip_filter=True: download-media path preserves both media-only rows.
    assert len(prepared_raw.messages) == 2
    assert {m.msg_id for m in prepared_raw.messages} == {5, 6}
    assert {m.media_type for m in prepared_raw.messages} == {"photo", "voice"}


async def test_prepare_all_unread_forum_uses_per_topic_markers(repo, monkeypatch):
    from unread.core.pipeline import prepare_all_unread_runs
    from unread.tg.dialogs import UnreadDialog
    from unread.tg.topics import ForumTopic

    await repo.upsert_messages(
        [
            Message(chat_id=-100, msg_id=40, date=datetime(2026, 4, 24, 12, 0), thread_id=1, text="old t1"),
            Message(chat_id=-100, msg_id=100, date=datetime(2026, 4, 24, 12, 1), thread_id=1, text="new t1"),
            Message(chat_id=-100, msg_id=200, date=datetime(2026, 4, 24, 12, 2), thread_id=2, text="old t2"),
            Message(chat_id=-100, msg_id=300, date=datetime(2026, 4, 24, 12, 3), thread_id=2, text="new t2"),
        ]
    )

    async def fake_unread(_client):
        return [
            UnreadDialog(
                chat_id=-100,
                kind="forum",
                title="Forum",
                username=None,
                unread_count=2,
                read_inbox_max_id=0,
            )
        ]

    async def fake_topics(_client, _chat_id):
        return [
            ForumTopic(topic_id=1, title="T1", unread_count=1, read_inbox_max_id=50),
            ForumTopic(topic_id=2, title="T2", unread_count=1, read_inbox_max_id=250),
        ]

    backfill_calls = []

    async def _no_backfill(*args, **kwargs):
        backfill_calls.append(kwargs)
        return 0

    monkeypatch.setattr("unread.tg.dialogs.list_unread_dialogs", fake_unread)
    monkeypatch.setattr("unread.tg.topics.list_forum_topics", fake_topics)
    monkeypatch.setattr("unread.tg.sync.backfill", _no_backfill)

    runs = [
        prepared
        async for prepared in prepare_all_unread_runs(
            client=MagicMock(),
            repo=repo,
            settings=get_settings(),
            enrich_opts=EnrichOpts(),
            yes=True,
        )
    ]

    assert len(runs) == 1
    prepared = runs[0]
    assert prepared.topic_titles == {1: "T1", 2: "T2"}
    assert prepared.topic_markers == {1: 50, 2: 250}
    assert {m.msg_id for m in prepared.messages} == {100, 300}
    assert backfill_calls[0]["from_msg_id"] == 51


async def test_prepare_all_unread_clamps_stale_read_marker(repo, monkeypatch):
    """Broadcast channel with stale read marker: don't fetch the whole history.

    Telegram sometimes reports `read_inbox_max_id=0` for channels the
    user has never explicitly read, even when the unread badge is small.
    Without a clamp, the batch path would walk all 313988 messages just
    to surface 31 unread. The fix: when the implied window
    (latest - marker) is >10× unread_count, trust the badge and start
    at `latest - unread_count - 50`.
    """
    from unread.core.pipeline import prepare_all_unread_runs
    from unread.tg.dialogs import UnreadDialog

    async def fake_unread(_client):
        return [
            UnreadDialog(
                chat_id=-200,
                kind="channel",
                title="CLASH",
                username=None,
                unread_count=31,
                read_inbox_max_id=0,
            )
        ]

    backfill_calls = []

    async def _no_backfill(*args, **kwargs):
        backfill_calls.append(kwargs)
        return 0

    monkeypatch.setattr("unread.tg.dialogs.list_unread_dialogs", fake_unread)
    monkeypatch.setattr("unread.tg.sync.backfill", _no_backfill)

    client = MagicMock()
    # Latest message in the channel is msg_id=313988. Gap from marker
    # (313988 - 0 = 313988) is 10000x the unread badge — clamp should fire.
    fake_latest = MagicMock()
    fake_latest.id = 313988
    client.get_messages = AsyncMock(return_value=[fake_latest])

    runs = [
        prepared
        async for prepared in prepare_all_unread_runs(
            client=client,
            repo=repo,
            settings=get_settings(),
            enrich_opts=EnrichOpts(),
            yes=True,
        )
    ]

    assert len(runs) == 1
    # from_msg passed to prepare_chat_run = latest - unread - 50 = 313907.
    # _determine_start subtracts 1 → start_msg_id=313906. backfill called
    # with from_msg_id = start_msg_id + 1 = 313907.
    assert backfill_calls, "backfill should still be invoked once clamped"
    assert backfill_calls[0]["from_msg_id"] == 313907


async def test_prepare_all_unread_keeps_marker_when_consistent(repo, monkeypatch):
    """Normal case: read marker matches unread_count → no clamp."""
    from unread.core.pipeline import prepare_all_unread_runs
    from unread.tg.dialogs import UnreadDialog

    async def fake_unread(_client):
        return [
            UnreadDialog(
                chat_id=-300,
                kind="channel",
                title="Normal",
                username=None,
                unread_count=11,
                read_inbox_max_id=20516,
            )
        ]

    backfill_calls = []

    async def _no_backfill(*args, **kwargs):
        backfill_calls.append(kwargs)
        return 0

    monkeypatch.setattr("unread.tg.dialogs.list_unread_dialogs", fake_unread)
    monkeypatch.setattr("unread.tg.sync.backfill", _no_backfill)

    client = MagicMock()
    fake_latest = MagicMock()
    fake_latest.id = 20527  # gap = 11, exactly unread_count
    client.get_messages = AsyncMock(return_value=[fake_latest])

    [
        prepared
        async for prepared in prepare_all_unread_runs(
            client=client,
            repo=repo,
            settings=get_settings(),
            enrich_opts=EnrichOpts(),
            yes=True,
        )
    ]

    # Untouched: read_inbox_max_id + 1 = 20517. _determine_start - 1 +
    # _pull_history + 1 = 20517 again.
    assert backfill_calls[0]["from_msg_id"] == 20517
