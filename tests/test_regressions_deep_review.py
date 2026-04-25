"""Regression tests for the deep-code-review fixes.

Each test locks in a bug found during the April 2026 audit so the same
regression can't quietly sneak back in.

Covered:
- `schema.sql` is idempotent across repeated `Repo.open()` calls
- `--folder` batch rejects period flags (analyzer.commands cmd_analyze)
- `--all-flat` requires explicit period
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

from analyzetg.analyzer import prompts
from analyzetg.analyzer.chunker import build_chunks
from analyzetg.analyzer.commands import cmd_analyze
from analyzetg.config import _load_dotenv, _read_toml, load_settings
from analyzetg.core.paths import compute_window, parse_ymd
from analyzetg.db.repo import Repo
from analyzetg.export.commands import cmd_dump

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
    env_path.write_bytes(b"\xef\xbb\xbfANALYZETG_REGRESSION_KEY=ok\n")
    monkeypatch.delenv("ANALYZETG_REGRESSION_KEY", raising=False)
    _load_dotenv(env_path)
    import os

    assert os.environ.get("ANALYZETG_REGRESSION_KEY") == "ok"


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
    presets_dir = tmp_path / "presets"
    presets_dir.mkdir()
    (presets_dir / "digest.md").write_text(
        "---\nname: summary\nprompt_version: v1\n---\nsystem\n---USER---\n"
        "{period} {title} {msg_count}\n{messages}\n"
    )
    monkeypatch.setattr(prompts, "PRESETS_DIR", presets_dir)
    with pytest.raises(RuntimeError, match="does not match filename stem"):
        prompts._load_all_presets()


# --- Chunker degenerate-budget guard ------------------------------------


def test_chunker_refuses_tiny_budget() -> None:
    # Force a budget < 2000 via huge output/safety relative to a small model
    # context. Building any chunk should raise instead of silently clamping.
    from datetime import datetime

    from analyzetg.models import Message

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
    # Drop into an isolated cwd so load_settings doesn't pick up the real
    # config.toml from the repo root.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text("[analyze]\nmin_msg_chars = 5\nbogus_key = 123\n")
    # Clear any leaked ANALYZETG_CONFIG_PATH so the relative path wins.
    monkeypatch.delenv("ANALYZETG_CONFIG_PATH", raising=False)
    with pytest.raises(Exception) as ei:
        load_settings()
    msg = str(ei.value)
    assert "bogus_key" in msg or "Extra inputs" in msg or "extra" in msg.lower()


# --- Stats usage_by returns unpriced_calls ------------------------------


async def test_folder_rejects_period_flags() -> None:
    """`atg analyze --folder X --full-history` must fail fast, not silently
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


async def test_all_flat_requires_period() -> None:
    """`--all-flat` alone (no period flag) must raise, not fall back to unread."""
    import typer as _typer

    with pytest.raises(_typer.BadParameter, match="--all-flat requires"):
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


async def test_dump_all_flat_requires_period() -> None:
    """`atg dump --all-flat` must match analyze: explicit period required."""
    import typer as _typer

    with pytest.raises(_typer.BadParameter, match="--all-flat requires"):
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

    from analyzetg.models import Message

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
    from analyzetg.ask.retrieval import tokenize_question

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


async def test_ask_retrieval_scores_by_token_hits(tmp_path: Path) -> None:
    from datetime import datetime as _dt

    from analyzetg.ask.retrieval import retrieve_messages
    from analyzetg.models import Message

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
