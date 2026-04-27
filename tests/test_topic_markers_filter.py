"""Per-topic unread filtering in run_analysis.

Real regression: on a forum with a dialog-level read marker at msg 3073
but per-topic markers ranging from 0 to 5000, the analyzer fetched "1
message" (those past the dialog marker) instead of the ~1436 the user
could see in Telegram's topic badges. The fix computes per-topic
markers in cmd_analyze and applies a second filter in run_analysis so
each message survives only against its own topic's marker.

These tests pin the pure filtering logic against a live Repo, which is
the closest we can get to run_analysis without stubbing the whole
pipeline.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from unread.db.repo import Repo
from unread.models import Message


@pytest.fixture
async def repo(tmp_path: Path):
    r = await Repo.open(tmp_path / "t.sqlite")
    try:
        yield r
    finally:
        await r.close()


def _m(msg_id: int, thread_id: int, text: str = "x") -> Message:
    return Message(
        chat_id=-1,
        msg_id=msg_id,
        date=datetime(2026, 4, 24, 12, 0),
        thread_id=thread_id,
        sender_name="Alice",
        text=text,
    )


def _apply_topic_markers(msgs, topic_markers):
    # Mirrors the exact block inside analyzer/pipeline.py:run_analysis so
    # the contract can be tested without spinning up OpenAI + a full run.
    return [
        m
        for m in msgs
        if m.thread_id is None or m.thread_id not in topic_markers or m.msg_id > topic_markers[m.thread_id]
    ]


def test_filter_drops_messages_below_per_topic_marker():
    # Topic 1 read to 100, topic 2 read to 500. All messages in the
    # range 50..600. The filter must keep:
    #   topic 1: msg_id > 100
    #   topic 2: msg_id > 500
    msgs = [_m(msg_id=50, thread_id=1), _m(150, 1), _m(400, 2), _m(600, 2)]
    kept = _apply_topic_markers(msgs, {1: 100, 2: 500})
    ids = [(m.msg_id, m.thread_id) for m in kept]
    assert (50, 1) not in ids  # below topic-1 marker
    assert (150, 1) in ids
    assert (400, 2) not in ids  # below topic-2 marker
    assert (600, 2) in ids


def test_filter_preserves_messages_for_unknown_topics():
    # Topic 99 isn't in the markers map (e.g. created after our fetch).
    # We keep those messages rather than drop — losing a whole topic
    # silently is worse than showing read messages.
    msgs = [_m(50, 99), _m(100, 1)]
    kept = _apply_topic_markers(msgs, {1: 50})
    assert _m(50, 99).msg_id in {m.msg_id for m in kept}


def test_filter_preserves_thread_id_none_messages():
    # Some forum messages (e.g. service messages) carry thread_id=None.
    # Those can't be attributed to a topic marker — don't drop them.
    msgs = [Message(chat_id=-1, msg_id=500, date=datetime.now(), thread_id=None, text="svc")]
    kept = _apply_topic_markers(msgs, {1: 1000, 2: 1000})
    assert len(kept) == 1


def test_filter_noop_when_markers_all_zero():
    # Fresh account that never opened a topic — markers=0 means every
    # msg_id>0 survives, i.e. full history. The fix in cmd_analyze skips
    # computing a floor in this case so backfill still works; here we
    # just verify the filter itself doesn't over-drop.
    msgs = [_m(1, 1), _m(2, 2), _m(3, 1)]
    kept = _apply_topic_markers(msgs, {1: 0, 2: 0})
    assert len(kept) == 3


def test_filter_identical_to_no_filter_when_every_msg_past_marker():
    # If the floor was computed correctly in cmd_analyze, MOST messages
    # will be past the min marker but some may be past the max marker
    # too; post-filter should be a no-op on messages past their own
    # topic's marker regardless.
    msgs = [_m(200, 1), _m(200, 2)]
    kept = _apply_topic_markers(msgs, {1: 100, 2: 100})
    assert len(kept) == 2


def test_filter_against_real_iter_messages_respects_thread_markers(repo):
    # End-to-end-ish: insert messages into the DB with different
    # thread_ids, run iter_messages with a low min_msg_id floor (what
    # cmd_analyze would use), then apply _apply_topic_markers. Kept set
    # should match what a correctly-implemented forum unread returns.
    import asyncio

    async def _run():
        await repo.upsert_messages(
            [
                _m(50, 1),
                _m(100, 1),
                _m(150, 1),
                _m(200, 2),
                _m(300, 2),
                _m(400, 2),
            ]
        )
        # Floor = min(markers) = 75. iter_messages returns 100,150,200,300,400.
        raw = await repo.iter_messages(-1, thread_id=None, min_msg_id=75)
        assert {m.msg_id for m in raw} == {100, 150, 200, 300, 400}

        # Topic 1 read to 100, topic 2 read to 250. Expected survivors:
        # 150 (topic 1), 300/400 (topic 2).
        filtered = _apply_topic_markers(raw, {1: 100, 2: 250})
        assert {m.msg_id for m in filtered} == {150, 300, 400}

    asyncio.get_event_loop().run_until_complete(_run())
