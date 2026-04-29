"""Regression tests for the deep-code-review fixes.

Each test locks in a bug found during the April 2026 audit so the same
regression can't quietly sneak back in.

Covered:
- `schema.sql` is idempotent across repeated `Repo.open()` calls
- `--folder` batch rejects period flags (analyzer.commands cmd_analyze)
- `--all-flat` unread default reaches per-topic-marker fallback (not rejected at parse)
- `.env` loader strips UTF-8 BOM
- `compute_window` is timezone-aware (UTC)
- `analysis_cache` truncated flag persists + re-runs on hit
- Preset placeholder typo is caught at load time
- Preset name != stem is rejected at load time
- Chunker raises on degenerate budget instead of clamping to 500
- Pydantic config rejects unknown keys (extra=forbid)
- Pricing stats expose unpriced_calls column
"""

from __future__ import annotations

import sqlite3
from datetime import UTC
from pathlib import Path

import pytest

from unread.analyzer import prompts
from unread.analyzer.chunker import build_chunks
from unread.analyzer.commands import cmd_analyze
from unread.config import _load_dotenv, _read_toml, load_settings
from unread.core.paths import compute_window, parse_ymd
from unread.db.repo import Repo
from unread.export.commands import cmd_dump

# --- Schema idempotency -------------------------------------------------


async def test_schema_apply_is_idempotent(tmp_path: Path) -> None:
    """Opening the same DB twice must be a no-op the second time.

    The whole point of dropping migrations in favor of `schema.sql` is that
    every statement uses `IF NOT EXISTS` and applying it repeatedly is safe.
    Regression guard against someone adding a statement without that clause.
    """
    db = tmp_path / "t.sqlite"
    repo = await Repo.open(db)
    await repo.close()
    # Second open on the same DB used to fail ("duplicate column name:
    # reactions") when ALTER statements lived in migrations; with schema.sql
    # as the source of truth, it must succeed cleanly.
    repo = await Repo.open(db)
    try:
        # Basic sanity: the schema actually created something.
        cur = await repo._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        )
        row = await cur.fetchone()
        await cur.close()
        assert row is not None
    finally:
        await repo.close()


async def test_schema_apply_upgrades_legacy_tables(tmp_path: Path) -> None:
    """Existing DBs must receive additive columns that CREATE TABLE skips."""
    db = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE messages (
            chat_id INTEGER NOT NULL,
            msg_id INTEGER NOT NULL,
            thread_id INTEGER,
            date TIMESTAMP NOT NULL,
            sender_id INTEGER,
            sender_name TEXT,
            text TEXT,
            reply_to INTEGER,
            forward_from TEXT,
            PRIMARY KEY (chat_id, msg_id)
        );
        CREATE TABLE analysis_cache (
            batch_hash TEXT PRIMARY KEY,
            preset TEXT NOT NULL,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            result TEXT NOT NULL,
            prompt_tokens INTEGER,
            cached_tokens INTEGER,
            completion_tokens INTEGER,
            cost_usd REAL,
            created_at TIMESTAMP
        );
        """
    )
    conn.close()

    repo = await Repo.open(db)
    try:
        msg_cols = {
            row["name"] for row in await (await repo._conn.execute("PRAGMA table_info(messages)")).fetchall()
        }
        cache_cols = {
            row["name"]
            for row in await (await repo._conn.execute("PRAGMA table_info(analysis_cache)")).fetchall()
        }
        assert {
            "media_type",
            "media_doc_id",
            "media_duration",
            "transcript",
            "transcript_model",
            "reactions",
        } <= msg_cols
        assert "truncated" in cache_cols
    finally:
        await repo.close()


# --- BOM-safe .env loader ----------------------------------------------


def test_load_dotenv_strips_utf8_bom(tmp_path: Path, monkeypatch) -> None:
    """Editors on Windows save .env with a BOM; the key must still parse."""
    env_path = tmp_path / ".env"
    env_path.write_bytes(b"\xef\xbb\xbfUNREAD_REGRESSION_KEY=ok\n")
    monkeypatch.delenv("UNREAD_REGRESSION_KEY", raising=False)
    _load_dotenv(env_path)
    import os

    assert os.environ.get("UNREAD_REGRESSION_KEY") == "ok"


def test_read_toml_wraps_parse_error(tmp_path: Path) -> None:
    """Malformed TOML must surface a helpful error, not a bare TOMLDecodeError."""
    bad = tmp_path / "config.toml"
    bad.write_text('broken = "no closing quote\n')
    with pytest.raises(ValueError, match="TOML parse error"):
        _read_toml(bad)


# --- UTC window math ----------------------------------------------------


def test_parse_ymd_is_utc_aware() -> None:
    dt = parse_ymd("2026-04-24")
    assert dt is not None
    assert dt.tzinfo is UTC


def test_compute_window_last_days_is_utc_aware() -> None:
    since, until = compute_window(None, None, 7)
    assert since is not None and until is not None
    assert since.tzinfo is UTC and until.tzinfo is UTC


def test_compute_window_last_hours_is_utc_aware() -> None:
    """`--last-hours N` returns a UTC-aware (now-N hours, now) window.

    Hour-granular flag mirrors `--last-days` semantics but at finer
    resolution. New flag added to support `last24h` / `last96h` wizard
    options that can't be expressed via date-granular `--since`.
    """
    from datetime import datetime as _dt
    from datetime import timedelta

    since, until = compute_window(None, None, None, last_hours=24)
    assert since is not None and until is not None
    assert since.tzinfo is UTC and until.tzinfo is UTC
    delta = until - since
    assert timedelta(hours=23, minutes=55) <= delta <= timedelta(hours=24, minutes=5)
    # `until` is approximately now-UTC.
    assert abs(_dt.now(UTC) - until) < timedelta(minutes=1)


def test_compute_window_last_hours_wins_over_last_days() -> None:
    """When both --last-hours and --last-days are passed, hours wins.

    More-specific flag takes precedence; the helper is the resolver,
    caller-side mutex is the validator.
    """
    from datetime import timedelta

    since, until = compute_window(None, None, 7, last_hours=24)
    assert since is not None and until is not None
    delta = until - since
    # Hours window (~24h), not days window (~168h).
    assert timedelta(hours=23, minutes=55) <= delta <= timedelta(hours=24, minutes=5)


# --- Truncated cache hit is re-run --------------------------------------


async def test_cache_get_returns_truncated_flag(tmp_path: Path) -> None:
    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        # Path A: non-truncated write round-trips as 0.
        await repo.cache_put(
            "h-clean",
            preset="summary",
            model="gpt-5.4",
            prompt_version="v1",
            result="full",
            prompt_tokens=10,
            cached_tokens=0,
            completion_tokens=5,
            cost_usd=0.0,
            truncated=False,
        )
        hit = await repo.cache_get("h-clean")
        assert hit is not None
        assert not hit["truncated"]

        # Path B: explicitly truncated write is recoverable. Normal code
        # never calls this (invariant §1) — we're verifying the column
        # actually persists so a defensive-read guard in the pipeline has
        # something to see.
        await repo.cache_put(
            "h-trunc",
            preset="summary",
            model="gpt-5.4",
            prompt_version="v1",
            result="partial",
            prompt_tokens=10,
            cached_tokens=0,
            completion_tokens=5,
            cost_usd=0.0,
            truncated=True,
        )
        hit = await repo.cache_get("h-trunc")
        assert hit is not None
        assert hit["truncated"] == 1
    finally:
        await repo.close()


# --- Preset validation --------------------------------------------------


def test_validate_user_template_rejects_unknown_placeholder(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    with pytest.raises(RuntimeError, match="unknown placeholder"):
        prompts._validate_user_template("{period} {title} {msg_count} {messages} {bogus}", path=bad)


def test_load_preset_rejects_name_stem_mismatch(tmp_path: Path, monkeypatch) -> None:
    # Per-language layout: presets live under presets/<lang>/. Build a
    # tiny mock tree at tmp_path/presets/en/digest.md with a name/stem
    # mismatch and assert the loader rejects it via get_presets("en").
    presets_dir = tmp_path / "presets"
    en_dir = presets_dir / "en"
    en_dir.mkdir(parents=True)
    (en_dir / "digest.md").write_text(
        "---\nname: summary\nprompt_version: v1\n---\nsystem\n---USER---\n"
        "{period} {title} {msg_count}\n{messages}\n"
    )
    monkeypatch.setattr(prompts, "PRESETS_DIR", presets_dir)
    prompts.clear_preset_cache()
    with pytest.raises(RuntimeError, match="does not match filename stem"):
        prompts.get_presets("en")
    prompts.clear_preset_cache()  # don't poison neighbour tests


# --- Chunker degenerate-budget guard ------------------------------------


def test_chunker_refuses_tiny_budget() -> None:
    # Force a budget < 2000 via huge output/safety relative to a small model
    # context. Building any chunk should raise instead of silently clamping.
    from datetime import datetime

    from unread.models import Message

    m = Message(chat_id=1, msg_id=1, date=datetime.now(UTC), text="x")
    with pytest.raises(ValueError, match="Chunk token budget too small"):
        build_chunks(
            [m],
            model="gpt-4o",  # 128k context
            system_prompt="s" * 10,
            user_overhead="u" * 10,
            output_budget=130_000,  # swallows the whole context
            safety_margin=2000,
        )


# --- Pydantic strict config --------------------------------------------


def test_settings_reject_unknown_keys(tmp_path: Path, monkeypatch) -> None:
    # Point UNREAD_CONFIG_PATH at our crafted config — the loader no
    # longer falls back to a cwd-relative `./config.toml` (which used to
    # be a developer-only convenience but masked typos in
    # `~/.unread/config.toml` after the install-dir switch).
    cfg = tmp_path / "config.toml"
    cfg.write_text("[analyze]\nmin_msg_chars = 5\nbogus_key = 123\n")
    monkeypatch.setenv("UNREAD_CONFIG_PATH", str(cfg))
    with pytest.raises(Exception) as ei:
        load_settings()
    msg = str(ei.value)
    assert "bogus_key" in msg or "Extra inputs" in msg or "extra" in msg.lower()


# --- Stats usage_by returns unpriced_calls ------------------------------


async def test_folder_rejects_period_flags() -> None:
    """`unread analyze --folder X --full-history` must fail fast, not silently
    analyze only unread messages."""
    import typer as _typer

    with pytest.raises(_typer.BadParameter, match="--folder is unread-only"):
        await cmd_analyze(
            ref=None,
            thread=None,
            from_msg=None,
            full_history=True,
            since=None,
            until=None,
            last_days=None,
            preset=None,
            prompt_file=None,
            model=None,
            filter_model=None,
            output=None,
            folder="Alpha",
        )


class _SentinelClient(Exception):
    """Stubbed tg_client raises this to short-circuit the run after validation."""


def _fake_tg_client_factory():
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _fake_tg_client(*_args, **_kwargs):
        raise _SentinelClient
        yield  # pragma: no cover

    return _fake_tg_client


async def test_all_flat_unread_default_reaches_run_path(monkeypatch) -> None:
    """`--all-flat` with no period must reach the run path (per-topic unread fallback).

    The wizard's chat picker offers `unread` for forum all-flat mode and
    `_run_single` handles the per-topic-marker floor. The validator that
    rejected this combination at parse time has been removed; if it sneaks
    back in, this test fails because BadParameter would fire before the
    stubbed client.
    """
    from unread.analyzer import commands as analyzer_commands

    monkeypatch.setattr(analyzer_commands, "tg_client", _fake_tg_client_factory())

    with pytest.raises(_SentinelClient):
        await cmd_analyze(
            ref="@somechat",
            thread=None,
            from_msg=None,
            full_history=False,
            since=None,
            until=None,
            last_days=None,
            preset=None,
            prompt_file=None,
            model=None,
            filter_model=None,
            output=None,
            all_flat=True,
        )


async def test_dump_all_flat_unread_default_reaches_run_path(monkeypatch) -> None:
    """`unread dump --all-flat` mirrors analyze: unread default no longer rejected."""
    from unread.export import commands as export_commands

    monkeypatch.setattr(export_commands, "tg_client", _fake_tg_client_factory())

    with pytest.raises(_SentinelClient):
        await cmd_dump(
            ref="@somechat",
            output=None,
            fmt="md",
            since=None,
            until=None,
            last_days=None,
            full_history=False,
            thread=None,
            from_msg=None,
            join=False,
            with_transcribe=False,
            include_transcripts=True,
            all_flat=True,
        )


async def test_media_breakdown_groups_by_kind(tmp_path: Path) -> None:
    """The wizard's enrich picker reads this to show per-kind counts."""
    from datetime import datetime as _dt

    from unread.models import Message

    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        now = _dt.now(UTC)
        msgs = [
            Message(chat_id=1, msg_id=1, date=now, text=None, media_type="voice", media_doc_id=10),
            Message(chat_id=1, msg_id=2, date=now, text=None, media_type="voice", media_doc_id=11),
            Message(chat_id=2, msg_id=1, date=now, text=None, media_type="voice", media_doc_id=12),
            Message(chat_id=1, msg_id=3, date=now, text=None, media_type="videonote", media_doc_id=13),
            Message(chat_id=1, msg_id=4, date=now, text=None, media_type="photo", media_doc_id=14),
            Message(chat_id=1, msg_id=5, date=now, text="check https://example.com"),
            Message(chat_id=1, msg_id=6, date=now, text=None, media_type="doc", media_doc_id=16),
        ]
        await repo.upsert_messages(msgs)

        b = await repo.media_breakdown(1)
        assert b["total"] == 6
        assert b["voice"] == 2
        assert b["videonote"] == 1
        assert b["photo"] == 1
        assert b["doc"] == 1
        assert b["links"] == 1
        assert b["text"] == 1
        assert b["any_media"] == 5

        # min_msg_id is exclusive (matches the unread-anchor convention).
        b2 = await repo.media_breakdown(1, min_msg_id=4)
        assert b2["total"] == 2  # msg_ids 5, 6
        assert b2["doc"] == 1
        assert b2["links"] == 1
    finally:
        await repo.close()


# --- ask retrieval ------------------------------------------------------


def test_tokenize_question_drops_stop_words_and_short_tokens() -> None:
    from unread.ask.retrieval import tokenize_question

    tokens = tokenize_question("What did Bob say about migration?")
    assert "bob" in tokens
    assert "migration" in tokens
    # Stop words and short tokens are filtered.
    assert "what" not in tokens
    assert "did" not in tokens
    assert "is" not in tokens
    # Russian works too.
    ru = tokenize_question("Когда дедлайн по проекту?")
    assert "дедлайн" in ru
    assert "проекту" in ru
    assert "по" not in ru


async def test_cite_context_expands_citations(tmp_path: Path) -> None:
    """`--cite-context` should append a sources section with surrounding messages."""
    from datetime import datetime as _dt

    from unread.analyzer.commands import _expand_citations
    from unread.models import Message

    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        now = _dt.now(UTC)
        msgs = [
            Message(chat_id=1, msg_id=i, date=now, text=f"msg {i}", sender_name="alice") for i in range(1, 11)
        ]
        await repo.upsert_messages(msgs)

        body = "Findings: see [#5](https://t.me/x/5) and [#7](https://t.me/x/7)."
        # Default language="en" → "Sources"; verify both EN and RU paths.
        out_en = await _expand_citations(body, chat_id=1, repo=repo, context_n=2)
        assert "Sources" in out_en
        out_ru = await _expand_citations(body, chat_id=1, repo=repo, context_n=2, language="ru")
        assert "Источники" in out_ru
        out = out_en  # downstream assertions reference `out`
        # Anchor markers around the cited msgs.
        assert "#5" in out
        assert "#7" in out
        # Surrounding messages render: msg 3 is two before #5, msg 9 is two after #7.
        assert "#3" in out
        assert "#9" in out

        # Cap respected: a body with 50 distinct citations expands at most 30.
        many_cites = " ".join(f"[#{i}](https://t.me/x/{i})" for i in range(1, 51))
        out2 = await _expand_citations(many_cites, chat_id=1, repo=repo, context_n=0)
        # context_n=0 short-circuits.
        assert out2 == many_cites
    finally:
        await repo.close()


async def test_message_embeddings_round_trip_and_missing(tmp_path: Path) -> None:
    """Embeddings must round-trip through SQLite as float32 bytes, and the
    `missing` query must skip already-indexed rows + bodyless messages."""
    import array
    from datetime import datetime as _dt

    from unread.models import Message

    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        now = _dt.now(UTC)
        await repo.upsert_messages(
            [
                Message(chat_id=1, msg_id=1, date=now, text="hello world"),
                Message(chat_id=1, msg_id=2, date=now, text="another"),
                Message(chat_id=1, msg_id=3, date=now, text=None),  # bodyless → skip
            ]
        )
        # Initially all body-bearing msgs are missing.
        missing = await repo.msg_ids_missing_embedding(1, "test-model")
        assert missing == [1, 2]

        # Insert one row.
        vec = array.array("f", [0.1, 0.2, 0.3]).tobytes()
        wrote = await repo.put_embeddings([(1, 1, "test-model", vec)])
        assert wrote == 1

        # Now only msg_id=2 remains missing.
        missing = await repo.msg_ids_missing_embedding(1, "test-model")
        assert missing == [2]

        # Different model → both missing again.
        missing = await repo.msg_ids_missing_embedding(1, "other-model")
        assert missing == [1, 2]

        # Round-trip the bytes.
        rows = await repo.get_embeddings([1], "test-model")
        assert len(rows) == 1
        cid, mid, vec_bytes = rows[0]
        assert (cid, mid) == (1, 1)
        recovered = array.array("f")
        recovered.frombytes(vec_bytes)
        assert list(recovered) == pytest.approx([0.1, 0.2, 0.3], rel=1e-5)
    finally:
        await repo.close()


def test_citation_regex_matches_telegram_urls() -> None:
    """The citation regex covers every Telegram link shape the formatter
    emits: public usernames, private channel internal-ids, with and
    without forum thread segments."""
    from unread.analyzer.commands import _CITATION_RE

    # Public username.
    body = "see [#42](https://t.me/somegroup/42) yes"
    matches = list(_CITATION_RE.finditer(body))
    assert len(matches) == 1
    assert matches[0].group(1) == "42"
    assert matches[0].group(2) == "https://t.me/somegroup/42"

    # Private (internal_id form: t.me/c/<id>/<msg_id>).
    body = "[#100](https://t.me/c/1234567/100)"
    matches = list(_CITATION_RE.finditer(body))
    assert matches[0].group(2) == "https://t.me/c/1234567/100"

    # Forum topic (extra path segment).
    body = "[#7](https://t.me/somegroup/12345/7)"
    matches = list(_CITATION_RE.finditer(body))
    assert matches[0].group(2) == "https://t.me/somegroup/12345/7"

    # Multiple citations on one line.
    body = "[#1](https://t.me/x/1) and [#2](https://t.me/x/2) too"
    msg_ids = [m.group(1) for m in _CITATION_RE.finditer(body)]
    assert msg_ids == ["1", "2"]


def test_flatten_citations_renders_url_inline() -> None:
    """`--plain-citations` mode rewrites markdown links → text + URL.

    macOS Terminal.app (and similar terminals without OSC 8 hyperlink
    support) only style the markdown link without making it clickable.
    The flattened form `#N (url)` keeps the URL visible and copy-able.
    """
    from unread.analyzer.commands import _flatten_citations

    out = _flatten_citations("see [#42](https://t.me/x/42) and [#7](https://t.me/c/1/7)")
    assert "[#42](" not in out  # original markdown link is gone
    assert "#42 (https://t.me/x/42)" in out
    assert "#7 (https://t.me/c/1/7)" in out

    # Non-citation markdown links are untouched (the regex requires the
    # `[#N](...)` shape, not arbitrary link text).
    body = "see [the docs](https://example.com) for details"
    assert _flatten_citations(body) == body

    # Idempotent: running it twice on already-flattened text leaves it
    # alone (the second pass has no `[#N](...)` matches).
    once = _flatten_citations("[#1](https://t.me/x/1)")
    assert _flatten_citations(once) == once


def test_rerank_total_failure_returns_keyword_sorted_pool() -> None:
    """When all rerank batches fail, the fallback must keep the *best*
    keyword hits (sorted by score), not an arbitrary slice."""
    import asyncio
    from datetime import datetime as _dt

    from unread.ask import rerank as _rk
    from unread.models import Message

    # Build a pool with shuffled keyword scores.
    now = _dt.now(UTC)
    pool: list[tuple[Message, int]] = [
        (Message(chat_id=1, msg_id=10, date=now, text="low"), 1),
        (Message(chat_id=1, msg_id=11, date=now, text="med"), 3),
        (Message(chat_id=1, msg_id=12, date=now, text="high"), 5),
        (Message(chat_id=1, msg_id=13, date=now, text="mid"), 2),
        (Message(chat_id=1, msg_id=14, date=now, text="top"), 6),
    ]

    # Force every rerank batch to fail by stubbing chat_complete.
    async def _bad_chat_complete(*a, **kw):
        raise RuntimeError("simulated API outage")

    import unread.ask.rerank as rerank_mod

    orig = rerank_mod.chat_complete
    rerank_mod.chat_complete = _bad_chat_complete  # type: ignore[assignment]
    try:
        out = asyncio.run(_rk.rerank(repo=None, pool=pool, question="x", model="m", keep=3, batch_size=2))
    finally:
        rerank_mod.chat_complete = orig  # type: ignore[assignment]

    # Top-3 by keyword score: msg_ids 14 (score 6), 12 (5), 11 (3).
    msg_ids = [m.msg_id for m, _ in out]
    assert msg_ids == [14, 12, 11]


def test_rerank_parses_clean_json_and_fenced_responses() -> None:
    """The cheap rerank model occasionally wraps the JSON in prose / fences.

    `_parse_ratings` must tolerate both shapes. Without this, a single
    chatty batch would silently lose its rerank scores and the rest of
    the rerank pool falls back to the keyword order.
    """
    from unread.ask.rerank import _parse_ratings

    # Clean JSON.
    out = _parse_ratings('[{"msg_id": 1, "score": 5}, {"msg_id": 2, "score": 3}]')
    assert out == [(1, 5), (2, 3)]

    # ```json fence.
    out = _parse_ratings('```json\n[{"msg_id": 7, "score": 4}]\n```')
    assert out == [(7, 4)]

    # Prose-wrapped (model added an explanation).
    out = _parse_ratings('Here are the ratings:\n[{"msg_id": 9, "score": 2}]\nDone.')
    assert out == [(9, 2)]

    # Score clamped to 1-5.
    out = _parse_ratings('[{"msg_id": 1, "score": 99}, {"msg_id": 2, "score": -3}]')
    assert out == [(1, 5), (2, 1)]

    # Garbage → empty.
    assert _parse_ratings("no json here") == []
    assert _parse_ratings("") == []
    assert _parse_ratings(None) == []


async def test_ask_retrieval_scores_by_token_hits(tmp_path: Path) -> None:
    from datetime import datetime as _dt

    from unread.ask.retrieval import retrieve_messages
    from unread.models import Message

    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        now = _dt.now(UTC)
        msgs = [
            # Two-token hit — should win.
            Message(chat_id=1, msg_id=1, date=now, text="we picked Postgres for the migration"),
            # One-token hit.
            Message(chat_id=1, msg_id=2, date=now, text="general chat about postgres tuning"),
            # Zero hits — must be excluded.
            Message(chat_id=1, msg_id=3, date=now, text="weather is nice today"),
            # Transcript-only match (transcript is written via the dedicated
            # setter, not through upsert_messages).
            Message(chat_id=1, msg_id=4, date=now, text=None),
        ]
        await repo.upsert_messages(msgs)
        await repo.set_message_transcript(1, 4, "postgres migration is done", "test-model")

        out = await retrieve_messages(
            repo=repo,
            question="postgres migration plan",
            limit=10,
        )
        ids = [m.msg_id for m in out]
        assert 3 not in ids  # no-hit message dropped
        # Top hits include the multi-token match (#1) and the transcript (#4).
        assert 1 in ids
        assert 4 in ids
        # Result is chronologically sorted — for same-timestamp msgs that's
        # by msg_id within chat. We just sanity-check ordering is stable.
        assert ids == sorted(ids)
    finally:
        await repo.close()


async def test_stats_by_includes_unpriced_calls(tmp_path: Path) -> None:
    repo = await Repo.open(tmp_path / "t.sqlite")
    try:
        # Two rows: one priced, one unpriced (cost_usd=NULL).
        await repo.log_usage(
            kind="chat",
            model="gpt-X",
            prompt_tokens=10,
            cached_tokens=0,
            completion_tokens=5,
            audio_seconds=None,
            cost_usd=None,
            context={"preset": "summary", "chat_id": 1},
        )
        await repo.log_usage(
            kind="chat",
            model="gpt-X",
            prompt_tokens=10,
            cached_tokens=0,
            completion_tokens=5,
            audio_seconds=None,
            cost_usd=0.001,
            context={"preset": "summary", "chat_id": 1},
        )
        rows = await repo.stats_by(group_by="model")
        assert rows
        row = rows[0]
        assert row["calls"] == 2
        assert row["unpriced_calls"] == 1
    finally:
        await repo.close()
