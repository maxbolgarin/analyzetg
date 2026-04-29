"""Round-trip every key in the unified `SECRET_KEYS` allowlist.

Pins the contract that `unread.db._keys.SECRET_KEYS` is the single
source of truth: both `Repo.put_secrets` (write side) and
`secrets.read_secrets` (read side) accept exactly the same set, with
no drift.

Catches the regression where adding a new chat-provider key to one
allowlist but forgetting the other would silently lose the value on
read.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from unread.db._keys import SECRET_KEYS


def test_secret_keys_include_every_chat_provider() -> None:
    """Every provider whose key the wizard prompts for must be in the allowlist.

    `telegram.session_string` joined the allowlist in Phase 3 — it's
    written only when the passphrase backend is active, but the
    schema-allowlist check still runs through `put_secrets` and would
    reject the row without the entry.
    """
    expected = {
        "telegram.api_id",
        "telegram.api_hash",
        "openai.api_key",
        "openrouter.api_key",
        "anthropic.api_key",
        "google.api_key",
        "telegram.session_string",
    }
    assert expected == SECRET_KEYS


def test_repo_put_and_read_round_trip_every_key(tmp_path: Path) -> None:
    """Put one row per key, read them back, expect everything to come through."""
    from unread.db.repo import open_repo, read_data_db_secrets_sync

    db = tmp_path / "data.sqlite"
    payload = {key: f"value-for-{key}" for key in SECRET_KEYS}

    async def _write_then_close() -> None:
        async with open_repo(db) as repo:
            await repo.put_secrets(payload)

    asyncio.run(_write_then_close())

    # Sync reader (used at CLI bootstrap) sees everything.
    sync_read = read_data_db_secrets_sync(db)
    assert sync_read == payload

    # Async reader sees the same.
    async def _async_read() -> dict[str, str]:
        async with open_repo(db) as repo:
            return await repo.get_secrets()

    async_read = asyncio.run(_async_read())
    assert async_read == payload


def test_repo_put_secrets_rejects_unknown_key(tmp_path: Path) -> None:
    """Defensive: a key outside the allowlist must raise on write."""
    from unread.db.repo import open_repo

    db = tmp_path / "data.sqlite"

    async def _attempt() -> None:
        async with open_repo(db) as repo:
            await repo.put_secrets({"unknown.bogus_key": "x"})

    with pytest.raises(ValueError, match="unknown secret key"):
        asyncio.run(_attempt())
