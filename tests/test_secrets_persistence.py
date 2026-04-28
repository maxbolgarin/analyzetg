"""End-to-end persistence of wizard-saved secrets.

The wizard writes to `data.sqlite::secrets`; `load_settings` overlays
those values onto the in-memory singleton at next reload. A user can
delete `~/.unread/.env` after init and the CLI keeps working.

Also pins the one-release backward-compat path: when the data DB has
no secrets row but the legacy session DB has an `unread_secrets` table
(from the previous release), `load_settings` reads from there.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch):
    """Run each test against a clean `UNREAD_HOME` and no env-supplied creds."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from unread.config import reset_settings

    reset_settings()
    return tmp_path


def test_data_db_secrets_overlay(isolated_home: Path) -> None:
    """`load_settings` fills missing creds from `data.sqlite::secrets`."""
    from unread.config import load_settings
    from unread.db.repo import Repo

    async def _seed():
        db = isolated_home / "storage" / "data.sqlite"
        repo = await Repo.open(db)
        await repo.put_secrets(
            {
                "telegram.api_id": "5555",
                "telegram.api_hash": "hashy",
                "openai.api_key": "sk-data",
            }
        )
        await repo.close()

    asyncio.run(_seed())

    s = load_settings()
    assert s.telegram.api_id == 5555
    assert s.telegram.api_hash == "hashy"
    assert s.openai.api_key == "sk-data"


def test_env_beats_data_db(isolated_home: Path, monkeypatch) -> None:
    """Env-set creds win — data DB only fills empty fields."""
    from unread.config import load_settings
    from unread.db.repo import Repo

    async def _seed():
        db = isolated_home / "storage" / "data.sqlite"
        repo = await Repo.open(db)
        await repo.put_secrets({"openai.api_key": "sk-from-db"})
        await repo.close()

    asyncio.run(_seed())

    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    from unread.config import reset_settings

    reset_settings()

    s = load_settings()
    assert s.openai.api_key == "sk-from-env"


def test_legacy_session_db_fallback(isolated_home: Path) -> None:
    """When data DB has no secrets row, fall back to the legacy session-DB table.

    Existing users on the prior release have rows in
    `session.sqlite::unread_secrets`. They should keep working without
    re-init.
    """
    from unread.config import load_settings

    storage = isolated_home / "storage"
    storage.mkdir(parents=True, exist_ok=True)

    # Mimic the legacy layout: only the session DB has the secrets table.
    session_db = storage / "session.sqlite"
    conn = sqlite3.connect(session_db)
    conn.execute(
        """
        CREATE TABLE unread_secrets (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP NOT NULL
        )
        """
    )
    now = datetime.now(UTC).isoformat()
    for k, v in [
        ("telegram.api_id", "777"),
        ("telegram.api_hash", "legacy-hash"),
        ("openai.api_key", "sk-legacy"),
    ]:
        conn.execute(
            "INSERT INTO unread_secrets VALUES (?, ?, ?)",
            (k, v, now),
        )
    conn.commit()
    conn.close()

    s = load_settings()
    assert s.telegram.api_id == 777
    assert s.telegram.api_hash == "legacy-hash"
    assert s.openai.api_key == "sk-legacy"


def test_data_db_takes_precedence_over_legacy(isolated_home: Path) -> None:
    """When both layouts have rows, the new `data.sqlite::secrets` wins."""
    from unread.config import load_settings
    from unread.db.repo import Repo

    async def _seed_data():
        db = isolated_home / "storage" / "data.sqlite"
        repo = await Repo.open(db)
        await repo.put_secrets({"openai.api_key": "sk-new"})
        await repo.close()

    asyncio.run(_seed_data())

    # Seed the legacy session-DB table too.
    session_db = isolated_home / "storage" / "session.sqlite"
    conn = sqlite3.connect(session_db)
    conn.execute(
        "CREATE TABLE unread_secrets (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TIMESTAMP NOT NULL)"
    )
    conn.execute(
        "INSERT INTO unread_secrets VALUES (?, ?, ?)",
        ("openai.api_key", "sk-legacy", datetime.now(UTC).isoformat()),
    )
    conn.commit()
    conn.close()

    s = load_settings()
    assert s.openai.api_key == "sk-new"
