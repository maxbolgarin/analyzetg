"""`unread.secrets.write_secrets` / `delete_secret` route via active backend.

Read-side routing was already exercised by `test_secrets_backend.py`. This
file covers the symmetric write half: post-init credential writes (wizard
re-runs, `unread settings`, `tg login` re-auth) must land in whichever
backend the user picked, not unconditionally in `data.sqlite::secrets`.

Without that guarantee, an install showing `keystore` in
`unread security status` would silently grow plaintext rows in the DB
every time the user rotated a key — exactly what "keystore by default"
is supposed to prevent.
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
    from unread.config import reset_settings

    reset_settings()
    return tmp_path


def _build_fake_keyring():
    from keyring.backend import KeyringBackend
    from keyring.errors import PasswordDeleteError

    class _Fake(KeyringBackend):
        priority = 1.0

        def __init__(self) -> None:
            super().__init__()
            self.store: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, username: str) -> str | None:
            return self.store.get((service, username))

        def set_password(self, service: str, username: str, password: str) -> None:
            self.store[(service, username)] = password

        def delete_password(self, service: str, username: str) -> None:
            if (service, username) not in self.store:
                raise PasswordDeleteError(f"no entry for {service}/{username}")
            del self.store[(service, username)]

    return _Fake()


@pytest.fixture
def fake_keyring(monkeypatch):
    import keyring

    fake = _build_fake_keyring()
    original = keyring.get_keyring()
    keyring.set_keyring(fake)
    try:
        yield fake
    finally:
        keyring.set_keyring(original)


def _ensure_db(home: Path) -> Path:
    """Create the data DB with the standard schema. Async one-shot."""
    from unread.db.repo import Repo

    db_path = home / "storage" / "data.sqlite"

    async def _open():
        repo = await Repo.open(db_path)
        await repo.close()

    asyncio.run(_open())
    return db_path


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


# ---------- write_secrets routing ---------------------------------------


def test_write_secrets_db_backend_writes_to_db(isolated_home: Path) -> None:
    from unread.config import load_settings
    from unread.db.repo import read_data_db_secrets_sync
    from unread.secrets import write_secrets

    db = _ensure_db(isolated_home)
    s = load_settings()

    asyncio.run(write_secrets(s, {"openai.api_key": "sk-db"}))

    assert read_data_db_secrets_sync(db) == {"openai.api_key": "sk-db"}


def test_write_secrets_keychain_backend_writes_to_keychain(isolated_home: Path, fake_keyring) -> None:
    """The whole point of this PR: with backend=keychain, the secret
    must land in the OS keychain — not in `data.sqlite::secrets`.
    """
    from unread.config import load_settings, reset_settings
    from unread.db.repo import read_data_db_secrets_sync
    from unread.secrets import write_secrets
    from unread.secrets_backend import KEYCHAIN_SERVICE

    db = _ensure_db(isolated_home)
    _set_app_setting(db, "secrets.backend", "keychain")
    reset_settings()
    s = load_settings()

    asyncio.run(write_secrets(s, {"openai.api_key": "sk-kc-route"}))

    assert fake_keyring.get_password(KEYCHAIN_SERVICE, "openai.api_key") == "sk-kc-route"
    # And nothing leaked into the DB.
    assert read_data_db_secrets_sync(db) == {}


def test_write_secrets_skips_empty_values_on_keychain(isolated_home: Path, fake_keyring) -> None:
    """Empty values are no-ops, mirroring `Repo.put_secrets`."""
    from unread.config import load_settings, reset_settings
    from unread.secrets import write_secrets
    from unread.secrets_backend import KEYCHAIN_SERVICE

    db = _ensure_db(isolated_home)
    _set_app_setting(db, "secrets.backend", "keychain")
    reset_settings()
    s = load_settings()

    asyncio.run(write_secrets(s, {"openai.api_key": "", "anthropic.api_key": "sk-ant"}))

    assert fake_keyring.get_password(KEYCHAIN_SERVICE, "openai.api_key") is None
    assert fake_keyring.get_password(KEYCHAIN_SERVICE, "anthropic.api_key") == "sk-ant"


def test_write_secrets_rejects_unknown_keys(isolated_home: Path) -> None:
    from unread.config import load_settings
    from unread.secrets import write_secrets

    _ensure_db(isolated_home)
    s = load_settings()

    with pytest.raises(ValueError):
        asyncio.run(write_secrets(s, {"evil.key": "x"}))


def test_write_secrets_passphrase_backend_writes_ciphertext_to_db(isolated_home: Path, monkeypatch) -> None:
    """Passphrase backend → encrypted blob lands in `secrets` table."""
    from unread.config import load_settings, reset_settings
    from unread.db.repo import read_data_db_secrets_sync
    from unread.secrets import write_secrets
    from unread.secrets_backend import BACKEND_PASSPHRASE
    from unread.security.crypto import (
        APP_SETTING_SALT,
        SALT_LEN,
        forget_cached_key,
        forget_process_keys,
        is_encrypted,
    )
    from unread.security.passphrase import _b64encode

    db = _ensure_db(isolated_home)
    salt = b"\x07" * SALT_LEN
    _set_app_setting(db, APP_SETTING_SALT, _b64encode(salt))
    _set_app_setting(db, "secrets.backend", BACKEND_PASSPHRASE)
    monkeypatch.setenv("UNREAD_PASSPHRASE", "test-passphrase")
    reset_settings()
    s = load_settings()

    try:
        asyncio.run(write_secrets(s, {"openai.api_key": "sk-secret-pp"}))

        rows = read_data_db_secrets_sync(db)
        assert "openai.api_key" in rows
        assert is_encrypted(rows["openai.api_key"])
        # Ciphertext is not the plaintext.
        assert rows["openai.api_key"] != "sk-secret-pp"
    finally:
        # Clear cross-test contamination: `_ensure_passphrase` caches
        # `UNREAD_PASSPHRASE` and `derive_key` results in process-wide
        # globals. Without cleanup, any later test that uses a
        # different passphrase or salt would silently reuse our key
        # and fail decryption with a confusing PassphraseError.
        import unread.secrets as _s

        _s._PROCESS_PASSPHRASE = None  # type: ignore[attr-defined]
        forget_process_keys()
        forget_cached_key()


# ---------- delete_secret routing ----------------------------------------


def test_delete_secret_db_backend(isolated_home: Path) -> None:
    from unread.config import load_settings
    from unread.db.repo import read_data_db_secrets_sync
    from unread.secrets import delete_secret, write_secrets

    _ensure_db(isolated_home)
    s = load_settings()
    asyncio.run(write_secrets(s, {"openai.api_key": "sk-del"}))

    removed = asyncio.run(delete_secret(s, "openai.api_key"))

    assert removed is True
    assert read_data_db_secrets_sync(s.storage.data_path) == {}


def test_delete_secret_keychain_backend(isolated_home: Path, fake_keyring) -> None:
    from unread.config import load_settings, reset_settings
    from unread.secrets import delete_secret, write_secrets
    from unread.secrets_backend import KEYCHAIN_SERVICE

    db = _ensure_db(isolated_home)
    _set_app_setting(db, "secrets.backend", "keychain")
    reset_settings()
    s = load_settings()

    asyncio.run(write_secrets(s, {"openai.api_key": "sk-temp"}))
    assert fake_keyring.get_password(KEYCHAIN_SERVICE, "openai.api_key") == "sk-temp"

    removed = asyncio.run(delete_secret(s, "openai.api_key"))

    assert removed is True
    assert fake_keyring.get_password(KEYCHAIN_SERVICE, "openai.api_key") is None


def test_delete_secret_rejects_unknown_keys(isolated_home: Path) -> None:
    from unread.config import load_settings
    from unread.secrets import delete_secret

    _ensure_db(isolated_home)
    s = load_settings()

    with pytest.raises(ValueError):
        asyncio.run(delete_secret(s, "bogus.slot"))


# ---------- end-to-end: status reflects user-visible promise -------------


def test_post_init_settings_write_lands_in_keychain(isolated_home: Path, fake_keyring, monkeypatch) -> None:
    """End-to-end: empty-install init flip + later write goes through.

    Reproduces the original user complaint: after `unread init` with no
    creds entered, the user's later write (via `unread settings` etc.)
    should land in the keychain — not silently in plaintext on disk.
    """
    from unread.config import load_settings, reset_settings
    from unread.db.repo import read_data_db_secrets_sync
    from unread.secrets import write_secrets
    from unread.secrets_backend import BACKEND_KEYCHAIN, KEYCHAIN_SERVICE, read_active_backend_sync
    from unread.tg.commands import _run_keychain_step

    db = _ensure_db(isolated_home)
    reset_settings()

    # Phase 1: wizard runs `_run_keychain_step` with empty slots.
    monkeypatch.setattr("unread.util.prompt._can_interact", lambda: True)
    _run_keychain_step()
    assert read_active_backend_sync(db) == BACKEND_KEYCHAIN

    # Phase 2: user later runs `unread settings` and adds a key.
    reset_settings()
    s = load_settings()
    asyncio.run(write_secrets(s, {"openai.api_key": "sk-after-init"}))

    # The promise: it landed in the keychain, not the DB.
    assert fake_keyring.get_password(KEYCHAIN_SERVICE, "openai.api_key") == "sk-after-init"
    assert read_data_db_secrets_sync(db) == {}
