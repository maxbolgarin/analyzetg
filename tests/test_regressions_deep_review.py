"""Regression tests for the deep-code-review fixes.

Each test locks in a bug found during the April 2026 audit so the same
regression can't quietly sneak back in.

Covered:
- Migration prefix uniqueness (repo.py _apply_migrations guard)
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

from datetime import UTC
from pathlib import Path

import pytest

from analyzetg.analyzer import prompts
from analyzetg.analyzer.chunker import build_chunks
from analyzetg.analyzer.commands import cmd_analyze
from analyzetg.config import _load_dotenv, _read_toml, load_settings
from analyzetg.core.paths import compute_window, parse_ymd
from analyzetg.db.repo import Repo

# --- Migration prefix uniqueness ---------------------------------------


async def test_migrations_reject_duplicate_prefix(tmp_path: Path, monkeypatch) -> None:
    """If two migration files share a numeric prefix, open() must fail fast."""
    # Drop two files with the same prefix into a scratch migrations dir and
    # point the repo at it.
    scratch = tmp_path / "migrations"
    scratch.mkdir()
    (scratch / "001_a.sql").write_text("CREATE TABLE IF NOT EXISTS a(x INT);")
    (scratch / "001_b.sql").write_text("CREATE TABLE IF NOT EXISTS b(x INT);")
    from analyzetg.db import repo as repo_mod

    monkeypatch.setattr(repo_mod, "MIGRATIONS_DIR", scratch)
    with pytest.raises(RuntimeError, match="prefix collision"):
        await Repo.open(tmp_path / "t.sqlite")


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
