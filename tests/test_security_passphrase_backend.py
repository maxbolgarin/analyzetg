"""Phase 3 — passphrase backend end-to-end (no Telegram involved).

Drives the upgrade → read_secrets → downgrade cycle entirely through
local SQLite + `data.sqlite::secrets`. Telethon's `StringSession` is
stubbed where it would otherwise run; nothing here hits the network.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("UNREAD_PASSPHRASE", raising=False)
    # Drop any in-process passphrase / key cached by a previous test
    # — without this, an env-supplied passphrase from one test bleeds
    # into the next via the module-level `_PROCESS_PASSPHRASE`.
    import unread.secrets as _secrets
    from unread.config import reset_settings
    from unread.security.crypto import forget_cached_key, forget_process_keys

    _secrets._PROCESS_PASSPHRASE = None  # type: ignore[attr-defined]
    reset_settings()
    forget_process_keys()
    forget_cached_key()
    yield tmp_path
    _secrets._PROCESS_PASSPHRASE = None  # type: ignore[attr-defined]
    forget_process_keys()
    forget_cached_key()


def _seed(home: Path, secrets: dict[str, str]) -> Path:
    from unread.db.repo import Repo

    db = home / "storage" / "data.sqlite"

    async def _do():
        repo = await Repo.open(db)
        await repo.put_secrets(secrets)
        await repo.close()

    asyncio.run(_do())
    return db


def _set_app_setting(db: Path, key: str, value: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------- read_secrets passphrase path ---------------------------------


def test_read_secrets_decrypts_with_env_passphrase(isolated_home: Path, monkeypatch) -> None:
    """Setting `UNREAD_PASSPHRASE` lets read_secrets decrypt without a TTY prompt."""
    from unread.config import load_settings, reset_settings
    from unread.secrets_backend import BACKEND_PASSPHRASE
    from unread.security.crypto import (
        APP_SETTING_SALT,
        SALT_LEN,
        derive_key,
        encrypt_with_key,
    )
    from unread.security.passphrase import _b64encode

    db = _seed(isolated_home, {})  # creates schema
    salt = b"\x00" * SALT_LEN  # deterministic for reproducibility
    key = derive_key("hunter2", salt)
    blob = encrypt_with_key("sk-from-encryption", key, salt=salt)

    # Persist the install salt + an encrypted slot, set backend.
    _set_app_setting(db, APP_SETTING_SALT, _b64encode(salt))
    _set_app_setting(db, "secrets.backend", BACKEND_PASSPHRASE)

    # Manually insert ciphertext (bypass put_secrets allowlist read
    # path because we want to check load_settings end-to-end).
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO secrets(key, value, updated_at) VALUES(?, ?, ?)",
            ("openai.api_key", blob, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("UNREAD_PASSPHRASE", "hunter2")
    reset_settings()
    s = load_settings()
    assert s.openai.api_key == "sk-from-encryption"


def test_read_secrets_passphrase_wrong_passphrase_raises(isolated_home: Path, monkeypatch) -> None:
    from unread.config import load_settings, reset_settings
    from unread.secrets_backend import BACKEND_PASSPHRASE
    from unread.security.crypto import (
        APP_SETTING_SALT,
        SALT_LEN,
        derive_key,
        encrypt_with_key,
    )
    from unread.security.passphrase import _b64encode

    db = _seed(isolated_home, {})
    salt = b"\x01" * SALT_LEN
    blob = encrypt_with_key("plain", derive_key("real", salt), salt=salt)
    _set_app_setting(db, APP_SETTING_SALT, _b64encode(salt))
    _set_app_setting(db, "secrets.backend", BACKEND_PASSPHRASE)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO secrets(key, value, updated_at) VALUES(?, ?, ?)",
            ("openai.api_key", blob, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("UNREAD_PASSPHRASE", "WRONG")
    reset_settings()
    from unread.security.crypto import PassphraseError

    with pytest.raises(PassphraseError):
        load_settings()


def test_read_secrets_passphrase_no_tty_no_env_raises(isolated_home: Path, monkeypatch) -> None:
    """Non-interactive context with no UNREAD_PASSPHRASE must error, not deadlock."""
    from unread.config import load_settings, reset_settings
    from unread.secrets_backend import BACKEND_PASSPHRASE
    from unread.security.crypto import (
        APP_SETTING_SALT,
        SALT_LEN,
        derive_key,
        encrypt_with_key,
    )
    from unread.security.passphrase import _b64encode

    db = _seed(isolated_home, {})
    salt = b"\x02" * SALT_LEN
    blob = encrypt_with_key("plain", derive_key("real", salt), salt=salt)
    _set_app_setting(db, APP_SETTING_SALT, _b64encode(salt))
    _set_app_setting(db, "secrets.backend", BACKEND_PASSPHRASE)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO secrets(key, value, updated_at) VALUES(?, ?, ?)",
            ("openai.api_key", blob, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    # No UNREAD_PASSPHRASE; pytest stdin is not a TTY.
    monkeypatch.delenv("UNREAD_PASSPHRASE", raising=False)
    reset_settings()
    with pytest.raises(RuntimeError, match="passphrase"):
        load_settings()


# ---------- session string round-trip ------------------------------------


def test_session_string_read_write_round_trip(isolated_home: Path, monkeypatch) -> None:
    """write_session_string_async + read_session_string_sync round-trip an arbitrary blob."""
    from unread.secrets_backend import BACKEND_PASSPHRASE
    from unread.security.crypto import APP_SETTING_SALT, SALT_LEN, derive_key, remember_key_for_salt
    from unread.security.passphrase import (
        _b64encode,
        read_session_string_sync,
        write_session_string_async,
    )

    db = _seed(isolated_home, {})
    salt = b"\x03" * SALT_LEN
    key = derive_key("pw", salt)
    _set_app_setting(db, APP_SETTING_SALT, _b64encode(salt))
    _set_app_setting(db, "secrets.backend", BACKEND_PASSPHRASE)
    remember_key_for_salt(salt, key)

    fake_session = "1ApgAAA…fake-telethon-string…end"
    asyncio.run(write_session_string_async(db, fake_session))

    monkeypatch.setenv("UNREAD_PASSPHRASE", "pw")
    out = read_session_string_sync(db)
    assert out == fake_session


def test_session_string_empty_when_unset(isolated_home: Path) -> None:
    from unread.security.passphrase import read_session_string_sync

    db = _seed(isolated_home, {})
    assert read_session_string_sync(db) == ""


def test_read_secrets_uses_disk_cache_no_prompt(isolated_home: Path, monkeypatch) -> None:
    """`upgrade` / `unlock` populate the on-disk key cache; the next process
    must consult it BEFORE asking the user for a passphrase. Otherwise the
    UX promise of `unlock` is broken.
    """
    from unread.config import load_settings, reset_settings
    from unread.secrets_backend import BACKEND_PASSPHRASE
    from unread.security.crypto import (
        APP_SETTING_SALT,
        SALT_LEN,
        derive_key,
        encrypt_with_key,
        forget_process_keys,
        store_cached_key,
    )
    from unread.security.passphrase import _b64encode

    db = _seed(isolated_home, {})
    salt = b"\x07" * SALT_LEN
    key = derive_key("seekrit", salt)
    blob = encrypt_with_key("sk-from-disk-cache", key, salt=salt)
    _set_app_setting(db, APP_SETTING_SALT, _b64encode(salt))
    _set_app_setting(db, "secrets.backend", BACKEND_PASSPHRASE)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO secrets(key, value, updated_at) VALUES(?, ?, ?)",
            ("openai.api_key", blob, datetime.now(UTC).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    # Simulate `unread security upgrade` having stamped the cache file:
    # write the key to the runtime cache, then forget the in-process
    # state. A fresh process would only see the disk cache.
    store_cached_key(key, salt, ttl_seconds=None)
    forget_process_keys()

    # No UNREAD_PASSPHRASE, no TTY — proves the disk cache is being
    # consulted before the passphrase prompt path. If `read_secrets`
    # reaches `_ensure_passphrase` it'd raise RuntimeError instead.
    monkeypatch.delenv("UNREAD_PASSPHRASE", raising=False)
    reset_settings()

    s = load_settings()
    assert s.openai.api_key == "sk-from-disk-cache"
