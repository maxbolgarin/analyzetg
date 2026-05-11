"""Migration of legacy `ai.provider` row → four `ai.<slot>_provider` rows.

The umbrella `ai.provider` knob was deprecated in 2026-05 in favour of
per-slot routing. `_migrate_legacy_ai_provider_sync` is the one-shot
rewrite that runs at every bootstrap; once it succeeds, the legacy row
is gone and subsequent runs are a no-op.

Tests assert the rewrite is correct AND idempotent.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def _seed_legacy_row(db_path: Path, value: str) -> None:
    """Create a minimal `app_settings` table and insert one legacy row."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)",
            ("ai.provider", value, "2026-05-07T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _read_provider_rows(db_path: Path) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT key, value FROM app_settings WHERE key LIKE 'ai.%_provider' OR key = 'ai.provider'"
        )
        return dict(cur.fetchall())
    finally:
        conn.close()


def test_anthropic_migration_seeds_chat_filter_vision_and_snaps_audio(tmp_path: Path):
    """Picking anthropic for the chat umbrella → all three chat-class
    slots get anthropic; audio snaps to openai (no Whisper-shape API)."""
    from unread.db.repo import _migrate_legacy_ai_provider_sync

    db = tmp_path / "data.sqlite"
    _seed_legacy_row(db, "anthropic")

    _migrate_legacy_ai_provider_sync(db)

    rows = _read_provider_rows(db)
    assert "ai.provider" not in rows  # legacy row deleted
    assert rows.get("ai.chat_provider") == "anthropic"
    assert rows.get("ai.filter_provider") == "anthropic"
    assert rows.get("ai.audio_provider") == "openai"
    assert rows.get("ai.vision_provider") == "anthropic"


def test_openai_migration_propagates_to_every_slot(tmp_path: Path):
    from unread.db.repo import _migrate_legacy_ai_provider_sync

    db = tmp_path / "data.sqlite"
    _seed_legacy_row(db, "openai")
    _migrate_legacy_ai_provider_sync(db)

    rows = _read_provider_rows(db)
    for slot in ("chat", "filter", "audio", "vision"):
        assert rows.get(f"ai.{slot}_provider") == "openai", slot


def test_local_migration_propagates_to_every_slot(tmp_path: Path):
    """Local is in the audio capability set, so it propagates verbatim."""
    from unread.db.repo import _migrate_legacy_ai_provider_sync

    db = tmp_path / "data.sqlite"
    _seed_legacy_row(db, "local")
    _migrate_legacy_ai_provider_sync(db)

    rows = _read_provider_rows(db)
    for slot in ("chat", "filter", "audio", "vision"):
        assert rows.get(f"ai.{slot}_provider") == "local", slot


def test_existing_slot_value_is_preserved(tmp_path: Path):
    """A user who pre-set `ai.chat_provider=google` then has a stray
    `ai.provider=anthropic` should keep the explicit slot value."""
    from unread.db.repo import _migrate_legacy_ai_provider_sync

    db = tmp_path / "data.sqlite"
    _seed_legacy_row(db, "anthropic")
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)",
            ("ai.chat_provider", "google", "2026-05-06T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    _migrate_legacy_ai_provider_sync(db)

    rows = _read_provider_rows(db)
    assert "ai.provider" not in rows
    assert rows.get("ai.chat_provider") == "google"  # preserved
    assert rows.get("ai.filter_provider") == "anthropic"  # filled


def test_migration_is_idempotent(tmp_path: Path):
    """Running twice produces the same rows; the second call short-circuits."""
    from unread.db.repo import _migrate_legacy_ai_provider_sync

    db = tmp_path / "data.sqlite"
    _seed_legacy_row(db, "google")

    _migrate_legacy_ai_provider_sync(db)
    rows_after_first = _read_provider_rows(db)

    _migrate_legacy_ai_provider_sync(db)
    rows_after_second = _read_provider_rows(db)

    assert rows_after_first == rows_after_second


def test_missing_db_is_noop(tmp_path: Path):
    """A path that doesn't exist degrades silently — bootstrap path
    runs this before the DB is necessarily on disk."""
    from unread.db.repo import _migrate_legacy_ai_provider_sync

    # Should not raise.
    _migrate_legacy_ai_provider_sync(tmp_path / "does-not-exist.sqlite")


def test_no_legacy_row_is_noop(tmp_path: Path):
    """An install with no `ai.provider` row stays untouched."""
    from unread.db.repo import _migrate_legacy_ai_provider_sync

    db = tmp_path / "data.sqlite"
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE app_settings(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
        conn.commit()
    finally:
        conn.close()

    _migrate_legacy_ai_provider_sync(db)
    assert _read_provider_rows(db) == {}


@pytest.mark.parametrize("provider", ("anthropic", "google"))
def test_audio_snap_per_provider(tmp_path: Path, provider: str):
    """anthropic / google → audio slot gets openai; rest get the picked value."""
    from unread.db.repo import _migrate_legacy_ai_provider_sync

    db = tmp_path / "data.sqlite"
    _seed_legacy_row(db, provider)
    _migrate_legacy_ai_provider_sync(db)
    rows = _read_provider_rows(db)
    assert rows["ai.audio_provider"] == "openai"
    assert rows["ai.chat_provider"] == provider
    assert rows["ai.filter_provider"] == provider
    assert rows["ai.vision_provider"] == provider
