"""End-to-end persistence of wizard-saved secrets.

The wizard writes to `data.sqlite::secrets`; `load_settings` overlays
those values onto the in-memory singleton at next reload. A user can
delete `~/.unread/.env` after init and the CLI keeps working.
"""

from __future__ import annotations

import asyncio
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
