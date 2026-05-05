"""Tests for the analysis-cache layer on `Repo` plus the message-redaction
row-count fix.

Covers:
- cache_get / cache_put round-trip
- cache_purge with age + preset + model filters
- cache_stats totals + per-(preset, model) breakdown
- cache_list ordering / filters / limit
- cache_iter_full returns full rows (with result body)
- vacuum returns non-negative int
- count_redactable_messages distinguishes 'has text' vs 'nothing to redact'
- redact_old_messages rowcount reflects actual changes (regression for
  the 'second run says Redacted 1800' bug)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from unread.db.repo import Repo
from unread.models import Message


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    r = await Repo.open(tmp_path / "t.sqlite")
    yield r
    await r.close()


# --- analysis_cache -----------------------------------------------------


async def test_cache_put_and_get_round_trip(repo: Repo) -> None:
    await repo.cache_put(
        "h1",
        preset="summary",
        model="gpt-5.4",
        prompt_version="v1",
        result="hello",
        prompt_tokens=100,
        cached_tokens=0,
        completion_tokens=50,
        cost_usd=0.01,
    )
    row = await repo.cache_get("h1")
    assert row is not None
    assert row["result"] == "hello"
    assert row["preset"] == "summary"
    assert row["model"] == "gpt-5.4"
    assert row["prompt_version"] == "v1"


async def test_cache_get_missing_returns_none(repo: Repo) -> None:
    assert await repo.cache_get("nonexistent") is None


async def test_cache_put_is_upsert(repo: Repo) -> None:
    await repo.cache_put(
        "h1",
        preset="p",
        model="m",
        prompt_version="v1",
        result="first",
        prompt_tokens=10,
        cached_tokens=0,
        completion_tokens=5,
        cost_usd=0.001,
    )
    await repo.cache_put(
        "h1",
        preset="p",
        model="m",
        prompt_version="v1",
        result="second",
        prompt_tokens=10,
        cached_tokens=0,
        completion_tokens=5,
        cost_usd=0.001,
    )
    row = await repo.cache_get("h1")
    assert row["result"] == "second"


async def test_cache_purge_by_age(repo: Repo) -> None:
    # Populate 3 rows, then force two of them to look old via raw SQL.
    for i in range(3):
        await repo.cache_put(
            f"h{i}",
            preset="p",
            model="m",
            prompt_version="v1",
            result=f"r{i}",
            prompt_tokens=1,
            cached_tokens=0,
            completion_tokens=1,
            cost_usd=0.0,
        )
    old = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    await repo._conn.execute("UPDATE analysis_cache SET created_at=? WHERE batch_hash IN ('h0','h1')", (old,))
    await repo._conn.commit()

    removed = await repo.cache_purge(older_than_days=30)
    assert removed == 2
    # h2 survived intact — verify the body, not just existence, so a
    # bug that purged-but-rewrote the row would still trip the test.
    survivor = await repo.cache_get("h2")
    assert survivor is not None
    assert survivor["result"] == "r2"
    assert await repo.cache_get("h0") is None


async def test_cache_purge_by_preset_and_model(repo: Repo) -> None:
    await repo.cache_put(
        "a",
        preset="summary",
        model="gpt-5.4",
        prompt_version="v1",
        result="r",
        prompt_tokens=1,
        cached_tokens=0,
        completion_tokens=1,
        cost_usd=0.0,
    )
    await repo.cache_put(
        "b",
        preset="summary",
        model="gpt-5.4-nano",
        prompt_version="v1",
        result="r",
        prompt_tokens=1,
        cached_tokens=0,
        completion_tokens=1,
        cost_usd=0.0,
    )
    await repo.cache_put(
        "c",
        preset="digest",
        model="gpt-5.4",
        prompt_version="v1",
        result="r",
        prompt_tokens=1,
        cached_tokens=0,
        completion_tokens=1,
        cost_usd=0.0,
    )

    # Purge only summary@gpt-5.4 → 1 removed (a), b and c survive.
    removed = await repo.cache_purge(preset="summary", model="gpt-5.4")
    assert removed == 1
    assert await repo.cache_get("a") is None
    b = await repo.cache_get("b")
    c = await repo.cache_get("c")
    assert b is not None and b["preset"] == "summary" and b["model"] == "gpt-5.4-nano"
    assert c is not None and c["preset"] == "digest" and c["model"] == "gpt-5.4"


async def test_cache_purge_zero_days_is_noop(repo: Repo) -> None:
    await repo.cache_put(
        "zero",
        preset="summary",
        model="gpt-5.4",
        prompt_version="v1",
        result="r",
        prompt_tokens=1,
        cached_tokens=0,
        completion_tokens=1,
        cost_usd=0.0,
    )
    removed = await repo.cache_purge(older_than_days=0, preset="summary")
    assert removed == 0
    untouched = await repo.cache_get("zero")
    assert untouched is not None
    assert untouched["result"] == "r" and untouched["preset"] == "summary"


async def test_cache_purge_preview_totals_and_breakdown(repo: Repo) -> None:
    """`cache_purge_preview` mirrors `cache_purge`'s filter logic and
    returns the totals + per-(preset, model) breakdown the CLI shows
    before the destructive DELETE fires."""
    rows = [
        ("a1", "summary", "gpt-5.4", "hello world", 0.05),
        ("a2", "summary", "gpt-5.4", "another summary", 0.03),
        ("a3", "summary", "gpt-5.4-nano", "different model", 0.01),
        ("a4", "digest", "gpt-5.4", "different preset", 0.02),
    ]
    for h, p, m, body, cost in rows:
        await repo.cache_put(
            h,
            preset=p,
            model=m,
            prompt_version="v1",
            result=body,
            prompt_tokens=1,
            cached_tokens=0,
            completion_tokens=1,
            cost_usd=cost,
        )

    # Unfiltered (no age, no preset, no model) → matches every row.
    pre_all = await repo.cache_purge_preview()
    assert pre_all["rows"] == 4
    assert pre_all["saved_cost_usd"] == pytest.approx(0.11)
    # Top group: summary @ gpt-5.4 (2 rows).
    top = pre_all["by_group"][0]
    assert top["preset"] == "summary" and top["model"] == "gpt-5.4"
    assert top["rows"] == 2
    # by_group is ordered desc by row count.
    counts = [g["rows"] for g in pre_all["by_group"]]
    assert counts == sorted(counts, reverse=True)


async def test_cache_purge_preview_respects_filters(repo: Repo) -> None:
    """preset + model filters narrow `rows` AND `by_group` so the user
    only sees what the actual purge would delete — not the whole table."""
    for i, (p, m) in enumerate([("summary", "gpt-5.4"), ("summary", "gpt-5.4"), ("digest", "gpt-5.4")]):
        await repo.cache_put(
            f"h{i}",
            preset=p,
            model=m,
            prompt_version="v1",
            result="r",
            prompt_tokens=1,
            cached_tokens=0,
            completion_tokens=1,
            cost_usd=0.01,
        )

    pre = await repo.cache_purge_preview(preset="summary")
    assert pre["rows"] == 2
    # Only summary@gpt-5.4 in the breakdown — digest excluded by filter.
    assert {(g["preset"], g["model"]) for g in pre["by_group"]} == {("summary", "gpt-5.4")}


async def test_cache_purge_preview_zero_days_short_circuits(repo: Repo) -> None:
    """`older_than_days <= 0` short-circuits to empty totals (mirrors
    `cache_purge`'s no-op guard) so the CLI doesn't render a preview
    that promises to delete things and then deletes nothing."""
    await repo.cache_put(
        "h",
        preset="p",
        model="m",
        prompt_version="v1",
        result="r",
        prompt_tokens=1,
        cached_tokens=0,
        completion_tokens=1,
        cost_usd=0.01,
    )
    pre = await repo.cache_purge_preview(older_than_days=0)
    assert pre["rows"] == 0
    assert pre["by_group"] == []
    assert pre["saved_cost_usd"] == 0.0


async def test_cache_stats_totals_and_groups(repo: Repo) -> None:
    await repo.cache_put(
        "a",
        preset="summary",
        model="gpt-5.4",
        prompt_version="v1",
        result="hello world",
        prompt_tokens=1,
        cached_tokens=0,
        completion_tokens=1,
        cost_usd=0.01,
    )
    await repo.cache_put(
        "b",
        preset="summary",
        model="gpt-5.4",
        prompt_version="v1",
        result="longer text here",
        prompt_tokens=1,
        cached_tokens=0,
        completion_tokens=1,
        cost_usd=0.02,
    )
    await repo.cache_put(
        "c",
        preset="digest",
        model="gpt-5.4-nano",
        prompt_version="v1",
        result="d",
        prompt_tokens=1,
        cached_tokens=0,
        completion_tokens=1,
        cost_usd=0.005,
    )

    s = await repo.cache_stats()
    assert s["rows"] == 3
    assert s["saved_cost_usd"] == pytest.approx(0.035)
    assert s["result_bytes"] >= len("hello world") + len("longer text here") + 1
    assert s["oldest"] is not None and s["newest"] is not None

    groups = {(g["preset"], g["model"]): g for g in s["by_group"]}
    assert (("summary", "gpt-5.4")) in groups
    assert groups[("summary", "gpt-5.4")]["rows"] == 2
    assert groups[("digest", "gpt-5.4-nano")]["rows"] == 1


async def test_cache_stats_empty(repo: Repo) -> None:
    s = await repo.cache_stats()
    assert s["rows"] == 0
    assert s["saved_cost_usd"] == 0.0
    assert s["by_group"] == []


async def test_cache_list_filters_and_limit(repo: Repo) -> None:
    for i in range(5):
        await repo.cache_put(
            f"h{i}",
            preset="summary" if i % 2 == 0 else "digest",
            model="gpt-5.4",
            prompt_version="v1",
            result=f"body-{i}",
            prompt_tokens=10,
            cached_tokens=0,
            completion_tokens=5,
            cost_usd=0.001,
        )
    rows = await repo.cache_list(preset="summary", limit=10)
    assert {r["batch_hash"] for r in rows} == {"h0", "h2", "h4"}
    # result_bytes is reported (not the body itself) — body is omitted to keep ls cheap.
    assert all("result_bytes" in r for r in rows)
    assert all("result" not in r for r in rows), "cache_list must not include result body"


async def test_cache_list_honors_limit(repo: Repo) -> None:
    for i in range(5):
        await repo.cache_put(
            f"h{i}",
            preset="summary",
            model="m",
            prompt_version="v1",
            result="x",
            prompt_tokens=1,
            cached_tokens=0,
            completion_tokens=1,
            cost_usd=0.0,
        )
    assert len(await repo.cache_list(limit=2)) == 2


async def test_cache_iter_full_returns_body(repo: Repo) -> None:
    await repo.cache_put(
        "h",
        preset="p",
        model="m",
        prompt_version="v1",
        result="full body",
        prompt_tokens=1,
        cached_tokens=0,
        completion_tokens=1,
        cost_usd=0.0,
    )
    rows = [r async for r in repo.cache_iter_full(preset="p")]
    assert len(rows) == 1
    assert rows[0]["result"] == "full body"


async def test_vacuum_returns_nonnegative_int(repo: Repo) -> None:
    # After a bunch of inserts + purge, VACUUM should run and return bytes freed
    # (>= 0 — may be 0 if the empty pages weren't big enough to measure).
    for i in range(20):
        await repo.cache_put(
            f"h{i}",
            preset="p",
            model="m",
            prompt_version="v1",
            result="x" * 500,
            prompt_tokens=1,
            cached_tokens=0,
            completion_tokens=1,
            cost_usd=0.0,
        )
    await repo.cache_purge(older_than_days=0, preset="p")  # keeps all (0 days → no-op)
    reclaimed = await repo.vacuum()
    assert isinstance(reclaimed, int)
    assert reclaimed >= 0


# --- cleanup / redact ---------------------------------------------------


async def test_count_redactable_distinguishes_text_from_empty_rows(repo: Repo) -> None:
    now = datetime.now(UTC)
    long_ago = now - timedelta(days=180)
    msgs = [
        Message(chat_id=1, msg_id=1, date=long_ago, text="has text"),
        Message(chat_id=1, msg_id=2, date=long_ago, text="more text"),
        Message(chat_id=1, msg_id=3, date=long_ago, text=None),  # already empty
    ]
    await repo.upsert_messages(msgs)
    pre = await repo.count_redactable_messages(retention_days=90, keep_transcripts=True)
    # All 3 match age filter; only 2 have text to null out.
    assert pre["messages"] == 3
    assert pre["with_text"] == 2
    assert pre["to_redact"] == 2


async def test_count_redactable_zero_retention_short_circuits(repo: Repo) -> None:
    # retention_days <= 0 means "forever" — nothing is redactable.
    pre = await repo.count_redactable_messages(retention_days=0)
    assert pre["to_redact"] == 0
    assert pre["messages"] == 0


async def test_redact_rowcount_reflects_actual_changes_not_matches(repo: Repo) -> None:
    """Regression: the second run used to report 'Redacted 1800' even when
    every row's text was already NULL. Now rowcount reflects real updates."""
    now = datetime.now(UTC)
    long_ago = now - timedelta(days=180)
    msgs = [Message(chat_id=1, msg_id=i, date=long_ago, text=f"body{i}") for i in range(5)]
    await repo.upsert_messages(msgs)

    first = await repo.redact_old_messages(retention_days=90, keep_transcripts=True)
    assert first == 5
    # Second run: nothing left to null — count must be 0, not 5.
    second = await repo.redact_old_messages(retention_days=90, keep_transcripts=True)
    assert second == 0


async def test_redact_respects_keep_transcripts(repo: Repo) -> None:
    now = datetime.now(UTC)
    long_ago = now - timedelta(days=180)
    # upsert_messages doesn't write transcripts — use set_message_transcript.
    await repo.upsert_messages(
        [
            Message(chat_id=1, msg_id=1, date=long_ago, text=None),
            Message(chat_id=1, msg_id=2, date=long_ago, text="plain"),
        ]
    )
    await repo.set_message_transcript(chat_id=1, msg_id=1, transcript="voice-1", model="w")

    # keep_transcripts=True: only msg_id=2 (with text) gets redacted.
    n = await repo.redact_old_messages(retention_days=90, keep_transcripts=True)
    assert n == 1
    rows = [m async for m in repo.iter_messages(1)]
    by_id = {m.msg_id: m for m in rows}
    assert by_id[1].transcript == "voice-1"  # transcript intact
    assert by_id[2].text is None  # text nulled


async def test_redact_no_keep_transcripts_nulls_both(repo: Repo) -> None:
    now = datetime.now(UTC)
    long_ago = now - timedelta(days=180)
    await repo.upsert_messages([Message(chat_id=1, msg_id=1, date=long_ago, text=None)])
    await repo.set_message_transcript(chat_id=1, msg_id=1, transcript="voice", model="w")

    n = await repo.redact_old_messages(retention_days=90, keep_transcripts=False)
    assert n == 1
    rows = [m async for m in repo.iter_messages(1)]
    assert rows[0].transcript is None


async def test_redact_chat_filter(repo: Repo) -> None:
    now = datetime.now(UTC)
    long_ago = now - timedelta(days=180)
    msgs = [
        Message(chat_id=1, msg_id=1, date=long_ago, text="in chat 1"),
        Message(chat_id=2, msg_id=1, date=long_ago, text="in chat 2"),
    ]
    await repo.upsert_messages(msgs)
    n = await repo.redact_old_messages(retention_days=90, chat_id=1)
    assert n == 1
    # chat 2 untouched
    rows = [m async for m in repo.iter_messages(2)]
    assert rows[0].text == "in chat 2"


async def test_redact_all_messages_ignores_retention(repo: Repo) -> None:
    """`all_messages=True` blanks every text regardless of age — including
    rows newer than the retention threshold which the date predicate would
    otherwise skip."""
    now = datetime.now(UTC)
    fresh = now - timedelta(days=2)  # WAY younger than 90d default
    old = now - timedelta(days=400)
    await repo.upsert_messages(
        [
            Message(chat_id=1, msg_id=1, date=fresh, text="fresh"),
            Message(chat_id=1, msg_id=2, date=old, text="old"),
        ]
    )

    # Without all_messages: only the old row qualifies.
    pre_old = await repo.count_redactable_messages(retention_days=90)
    assert pre_old["to_redact"] == 1

    # With all_messages: both rows qualify.
    pre_all = await repo.count_redactable_messages(retention_days=90, all_messages=True)
    assert pre_all["to_redact"] == 2

    n = await repo.redact_old_messages(retention_days=90, all_messages=True)
    assert n == 2
    rows = [m async for m in repo.iter_messages(1)]
    assert all(r.text is None for r in rows)


async def test_redactable_breakdown_per_chat_summary(repo: Repo) -> None:
    """`redactable_breakdown` returns per-chat counts ordered by impact, joined
    with the chats table for human-readable titles. Used by `unread cache tg`
    (formerly `unread cleanup`) to show *what* will be redacted before the
    confirm prompt fires."""
    await repo.upsert_chat(chat_id=10, kind="user", title="Alpha")
    await repo.upsert_chat(chat_id=20, kind="channel", title="Beta")
    # chat 30 deliberately has no chats-table row → title falls back to "".
    long_ago = datetime.now(UTC) - timedelta(days=180)
    msgs = (
        [Message(chat_id=10, msg_id=i, date=long_ago, text=f"a{i}") for i in range(5)]
        + [Message(chat_id=20, msg_id=i, date=long_ago, text=f"b{i}") for i in range(2)]
        + [Message(chat_id=30, msg_id=1, date=long_ago, text="orphan")]
    )
    await repo.upsert_messages(msgs)

    rows = await repo.redactable_breakdown(retention_days=90)
    # Sorted desc by row count: Alpha (5) → Beta (2) → orphan (1).
    assert [r["chat_id"] for r in rows] == [10, 20, 30]
    assert [r["rows"] for r in rows] == [5, 2, 1]
    assert rows[0]["title"] == "Alpha"
    assert rows[2]["title"] == ""  # orphan chat has no row in chats table
    # oldest/newest cover the message dates (we wrote them all at long_ago).
    assert rows[0]["oldest"] is not None and rows[0]["newest"] is not None


async def test_redactable_breakdown_respects_keep_transcripts(repo: Repo) -> None:
    """When keep_transcripts=True, a row whose `text` is already NULL
    (transcript-only) shouldn't appear in the breakdown — same predicate
    the UPDATE uses."""
    long_ago = datetime.now(UTC) - timedelta(days=180)
    await repo.upsert_chat(chat_id=10, kind="user", title="X")
    await repo.upsert_messages(
        [
            Message(chat_id=10, msg_id=1, date=long_ago, text="has text"),
            Message(chat_id=10, msg_id=2, date=long_ago, text=None),
        ]
    )
    await repo.set_message_transcript(chat_id=10, msg_id=2, transcript="v", model="w")

    keep = await repo.redactable_breakdown(retention_days=90, keep_transcripts=True)
    assert keep[0]["rows"] == 1  # only the row with text

    nuke = await repo.redactable_breakdown(retention_days=90, keep_transcripts=False)
    assert nuke[0]["rows"] == 2  # text-row + transcript-only row


async def test_redactable_breakdown_zero_retention_returns_empty(repo: Repo) -> None:
    """retention_days <= 0 short-circuits to [] (mirrors count + redact)."""
    long_ago = datetime.now(UTC) - timedelta(days=180)
    await repo.upsert_messages([Message(chat_id=10, msg_id=1, date=long_ago, text="x")])
    rows = await repo.redactable_breakdown(retention_days=0)
    assert rows == []
    # …but all_messages=True overrides the short-circuit.
    rows_all = await repo.redactable_breakdown(retention_days=0, all_messages=True)
    assert rows_all and rows_all[0]["rows"] == 1
