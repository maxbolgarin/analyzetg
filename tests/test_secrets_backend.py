"""Phase 2 — pluggable secrets-storage backend.

Covers:

- `read_active_backend_sync` defaults to ``db`` and reads
  `app_settings::secrets.backend` when present, ignoring junk.
- `read_secrets` consults the keychain backend when active, falling
  through to the DB when the keychain is empty.
- `cmd_migrate` round-trips DB → keychain → DB without losing values
  and flips the active flag.
- The wizard's keychain-step helper is a no-op when no slots are
  populated.

Uses an in-memory fake `keyring` backend installed via
``keyring.set_keyring`` so the test doesn't touch the developer's real
macOS Keychain / Linux Secret Service.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch):
    """Clean `UNREAD_HOME` with no env-supplied creds — same shape as
    `tests/test_secrets_persistence.py`'s fixture."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_API_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_API_HASH", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from unread.config import reset_settings

    reset_settings()
    return tmp_path


# ---------- fake keyring backend -----------------------------------------


def _build_fake_keyring():
    """Return a lazily-built ``KeyringBackend`` subclass with an in-memory store.

    Defined inside a factory so importing this test module doesn't
    eagerly resolve `keyring.backend.KeyringBackend` (which probes the
    OS for available providers — we don't want that side effect at
    collection time).
    """
    from keyring.backend import KeyringBackend
    from keyring.errors import PasswordDeleteError

    class _FakeKeyring(KeyringBackend):
        priority = 1.0  # required by keyring's set_keyring contract

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

    return _FakeKeyring()


@pytest.fixture
def fake_keyring(monkeypatch):
    """Install the in-memory backend for the test's duration."""
    import keyring

    fake = _build_fake_keyring()
    original = keyring.get_keyring()
    keyring.set_keyring(fake)
    try:
        yield fake
    finally:
        keyring.set_keyring(original)


# ---------- helpers ------------------------------------------------------


def _seed_db_secrets(home: Path, secrets: dict[str, str]) -> Path:
    """Synchronously create a data DB and insert allowlisted secrets."""
    from unread.db.repo import Repo

    db_path = home / "storage" / "data.sqlite"

    async def _seed():
        repo = await Repo.open(db_path)
        await repo.put_secrets(secrets)
        await repo.close()

    asyncio.run(_seed())
    return db_path


def _set_app_setting(db: Path, key: str, value: str) -> None:
    """Direct sqlite write — bypasses the async Repo for setup brevity."""
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


# ---------- read_active_backend_sync -------------------------------------


def test_active_backend_defaults_to_db(isolated_home: Path) -> None:
    from unread.secrets_backend import BACKEND_DB, read_active_backend_sync

    db = _seed_db_secrets(isolated_home, {"openai.api_key": "sk-x"})
    assert read_active_backend_sync(db) == BACKEND_DB


def test_active_backend_reads_app_settings(isolated_home: Path) -> None:
    from unread.secrets_backend import BACKEND_KEYCHAIN, read_active_backend_sync

    db = _seed_db_secrets(isolated_home, {"openai.api_key": "sk-x"})
    _set_app_setting(db, "secrets.backend", "keychain")
    assert read_active_backend_sync(db) == BACKEND_KEYCHAIN


def test_active_backend_ignores_unknown_value(isolated_home: Path) -> None:
    from unread.secrets_backend import BACKEND_DB, read_active_backend_sync

    db = _seed_db_secrets(isolated_home, {"openai.api_key": "sk-x"})
    _set_app_setting(db, "secrets.backend", "garbage")
    assert read_active_backend_sync(db) == BACKEND_DB


def test_active_backend_handles_missing_db(tmp_path: Path) -> None:
    from unread.secrets_backend import BACKEND_DB, read_active_backend_sync

    assert read_active_backend_sync(tmp_path / "nonexistent.sqlite") == BACKEND_DB


# ---------- keychain backend round-trip ----------------------------------


def test_read_secrets_uses_keychain_when_active(isolated_home: Path, fake_keyring) -> None:
    """When backend = keychain, read_secrets returns keychain values."""
    from unread.config import load_settings
    from unread.secrets_backend import KEYCHAIN_SERVICE

    db = _seed_db_secrets(isolated_home, {"openai.api_key": "sk-from-db"})
    _set_app_setting(db, "secrets.backend", "keychain")
    fake_keyring.set_password(KEYCHAIN_SERVICE, "openai.api_key", "sk-from-kc")

    s = load_settings()
    assert s.openai.api_key == "sk-from-kc"


def test_read_secrets_falls_back_when_keychain_empty(isolated_home: Path, fake_keyring) -> None:
    """If backend=keychain but the keychain is empty, the DB is the fallback."""
    from unread.config import load_settings

    db = _seed_db_secrets(isolated_home, {"openai.api_key": "sk-from-db"})
    _set_app_setting(db, "secrets.backend", "keychain")
    # fake_keyring has nothing set.

    s = load_settings()
    assert s.openai.api_key == "sk-from-db"


# ---------- migrate command ---------------------------------------------


def test_migrate_db_to_keychain_round_trip(isolated_home: Path, fake_keyring) -> None:
    """db → keychain moves values, blanks DB, flips backend flag."""
    from unread.config import reset_settings
    from unread.db.repo import read_data_db_secrets_sync
    from unread.secrets_backend import (
        BACKEND_KEYCHAIN,
        KEYCHAIN_SERVICE,
        read_active_backend_sync,
    )
    from unread.security.commands import cmd_migrate

    db = _seed_db_secrets(
        isolated_home,
        {
            "openai.api_key": "sk-openai",
            "telegram.api_id": "12345",
            "telegram.api_hash": "abchash",
        },
    )
    reset_settings()  # refresh singleton so cmd_migrate sees the new DB

    cmd_migrate(BACKEND_KEYCHAIN)

    assert read_active_backend_sync(db) == BACKEND_KEYCHAIN
    assert fake_keyring.get_password(KEYCHAIN_SERVICE, "openai.api_key") == "sk-openai"
    assert fake_keyring.get_password(KEYCHAIN_SERVICE, "telegram.api_id") == "12345"
    assert fake_keyring.get_password(KEYCHAIN_SERVICE, "telegram.api_hash") == "abchash"
    # DB rows are blanked.
    assert read_data_db_secrets_sync(db) == {}


def test_migrate_keychain_to_db_round_trip(isolated_home: Path, fake_keyring) -> None:
    from unread.config import reset_settings
    from unread.db.repo import read_data_db_secrets_sync
    from unread.secrets_backend import (
        BACKEND_DB,
        KEYCHAIN_SERVICE,
        read_active_backend_sync,
    )
    from unread.security.commands import cmd_migrate

    # Start with creds on the keychain, backend flag set accordingly.
    db = _seed_db_secrets(isolated_home, {})  # empty DB; just creates schema
    _set_app_setting(db, "secrets.backend", "keychain")
    fake_keyring.set_password(KEYCHAIN_SERVICE, "openai.api_key", "sk-kc")
    fake_keyring.set_password(KEYCHAIN_SERVICE, "anthropic.api_key", "sk-ant-kc")
    reset_settings()

    cmd_migrate(BACKEND_DB)

    assert read_active_backend_sync(db) == BACKEND_DB
    rows = read_data_db_secrets_sync(db)
    assert rows.get("openai.api_key") == "sk-kc"
    assert rows.get("anthropic.api_key") == "sk-ant-kc"
    assert fake_keyring.get_password(KEYCHAIN_SERVICE, "openai.api_key") is None


def test_migrate_idempotent(isolated_home: Path, fake_keyring) -> None:
    """Running --to keychain twice is a no-op the second time."""
    from unread.config import reset_settings
    from unread.secrets_backend import BACKEND_KEYCHAIN, read_active_backend_sync
    from unread.security.commands import cmd_migrate

    db = _seed_db_secrets(isolated_home, {"openai.api_key": "sk-x"})
    reset_settings()

    cmd_migrate(BACKEND_KEYCHAIN)
    # Second run: nothing left in the DB to move; should still succeed.
    cmd_migrate(BACKEND_KEYCHAIN)

    assert read_active_backend_sync(db) == BACKEND_KEYCHAIN


# ---------- wizard step --------------------------------------------------


def test_wizard_keychain_step_noop_when_no_secrets(isolated_home: Path, fake_keyring) -> None:
    """Wizard's `_run_keychain_step` is a no-op on a fresh install with no secrets."""
    from unread.config import reset_settings
    from unread.secrets_backend import BACKEND_DB, read_active_backend_sync
    from unread.tg.commands import _run_keychain_step

    db = _seed_db_secrets(isolated_home, {})  # empty
    reset_settings()

    # No prompt should fire; backend stays db.
    _run_keychain_step()

    assert read_active_backend_sync(db) == BACKEND_DB


def test_wizard_keychain_step_auto_migrates_without_prompt(
    isolated_home: Path, fake_keyring, monkeypatch
) -> None:
    """On a TTY with secrets present, the wizard auto-migrates to keystore.

    Keystore is the default — no `confirm()` should fire, no user
    consent required. The opt-out path is `unread security set plain`.
    """
    from unread.config import reset_settings
    from unread.secrets_backend import (
        BACKEND_KEYCHAIN,
        KEYCHAIN_SERVICE,
        read_active_backend_sync,
    )
    from unread.tg.commands import _run_keychain_step

    db = _seed_db_secrets(isolated_home, {"openai.api_key": "sk-default-keystore"})
    reset_settings()

    # Force the TTY guard True so the auto-migrate path runs in the test.
    monkeypatch.setattr("unread.tg.commands._can_interact", lambda: True, raising=False)
    # Belt-and-braces: also patch the import site `unread.util.prompt._can_interact`
    # since `_run_keychain_step` imports it locally.
    monkeypatch.setattr("unread.util.prompt._can_interact", lambda: True)

    # Trip an assertion if any prompt is reached — auto-migrate must NOT confirm.
    def _no_prompt(*a, **kw):
        raise AssertionError("auto-migrate path must not call confirm()")

    monkeypatch.setattr("unread.util.prompt.confirm", _no_prompt)

    _run_keychain_step()

    assert read_active_backend_sync(db) == BACKEND_KEYCHAIN
    assert fake_keyring.get_password(KEYCHAIN_SERVICE, "openai.api_key") == "sk-default-keystore"


# ---------- defensive paths ---------------------------------------------


def test_keychain_read_rejects_unknown_keys(fake_keyring) -> None:
    """Unknown slot names silently return None (defense against repl spelunking)."""
    from unread.secrets_backend import KEYCHAIN_SERVICE, keychain_read

    fake_keyring.set_password(KEYCHAIN_SERVICE, "evil_key", "stolen")
    assert keychain_read("evil_key") is None
    assert keychain_read("openai.api_key") is None  # not set


def test_keychain_write_rejects_unknown_keys(fake_keyring) -> None:
    from unread.secrets_backend import keychain_write

    with pytest.raises(ValueError):
        keychain_write("evil_key", "x")


# ---------- unified `unread security set` -------------------------------


def test_set_plain_to_keystore_round_trip(isolated_home: Path, fake_keyring) -> None:
    """`set keystore` from a fresh DB-backed install behaves exactly like migrate."""
    from unread.config import reset_settings
    from unread.secrets_backend import (
        BACKEND_KEYCHAIN,
        KEYCHAIN_SERVICE,
        read_active_backend_sync,
    )
    from unread.security.commands import cmd_set

    db = _seed_db_secrets(isolated_home, {"openai.api_key": "sk-via-set"})
    reset_settings()

    cmd_set("keystore")
    assert read_active_backend_sync(db) == BACKEND_KEYCHAIN
    assert fake_keyring.get_password(KEYCHAIN_SERVICE, "openai.api_key") == "sk-via-set"


def test_set_alias_resolution(isolated_home: Path, fake_keyring) -> None:
    """All four alias names route to the same backend."""
    from unread.config import reset_settings
    from unread.secrets_backend import (
        BACKEND_DB,
        BACKEND_KEYCHAIN,
        read_active_backend_sync,
    )
    from unread.security.commands import cmd_set

    db = _seed_db_secrets(isolated_home, {"openai.api_key": "sk-x"})
    reset_settings()

    # plain → keystore via "keyring" alias
    cmd_set("keyring")
    assert read_active_backend_sync(db) == BACKEND_KEYCHAIN

    # back to plain via "plaintext"
    cmd_set("plaintext")
    assert read_active_backend_sync(db) == BACKEND_DB


def test_set_noop_when_already_on_target(isolated_home: Path) -> None:
    """`set plain` on an already-plain install is a no-op (no error, no migration)."""
    from unread.config import reset_settings
    from unread.secrets_backend import BACKEND_DB, read_active_backend_sync
    from unread.security.commands import cmd_set

    db = _seed_db_secrets(isolated_home, {"openai.api_key": "sk-stable"})
    reset_settings()

    cmd_set("plain")
    assert read_active_backend_sync(db) == BACKEND_DB


def test_set_rejects_unknown_alias(isolated_home: Path) -> None:
    import typer

    from unread.config import reset_settings
    from unread.security.commands import cmd_set

    _seed_db_secrets(isolated_home, {})
    reset_settings()

    with pytest.raises(typer.Exit):
        cmd_set("magic")


# ---------- ciphertext-migration guards ----------------------------------


def test_migrate_refuses_to_move_ciphertext_keychain(isolated_home: Path, fake_keyring, monkeypatch) -> None:
    """`migrate --to keychain` must refuse when the active backend is `passphrase`,
    even if the user (or a stale wizard) tries it. This is the exact bug that
    corrupted a real install.
    """
    import typer

    from unread.config import reset_settings
    from unread.secrets_backend import BACKEND_KEYCHAIN, BACKEND_PASSPHRASE
    from unread.security.commands import cmd_migrate
    from unread.security.crypto import (
        APP_SETTING_SALT,
        SALT_LEN,
        derive_key,
        encrypt_with_key,
    )
    from unread.security.passphrase import _b64encode

    salt = b"\x05" * SALT_LEN
    key = derive_key("pw", salt)
    real_ciphertext = encrypt_with_key("sk-x", key, salt=salt)

    db = _seed_db_secrets(isolated_home, {"openai.api_key": real_ciphertext})
    _set_app_setting(db, APP_SETTING_SALT, _b64encode(salt))
    _set_app_setting(db, "secrets.backend", BACKEND_PASSPHRASE)
    monkeypatch.setenv("UNREAD_PASSPHRASE", "pw")
    reset_settings()

    with pytest.raises(typer.Exit):
        cmd_migrate(BACKEND_KEYCHAIN)


def test_migrate_helper_rejects_encrypted_db_rows(isolated_home: Path, fake_keyring) -> None:
    """Even if the backend flag is wrong, the migrate helper itself blocks
    ciphertext from leaving the DB.
    """
    import typer

    from unread.config import reset_settings
    from unread.security.commands import _migrate_db_to_keychain

    # Backend flag stays at default (db); rows are ciphertext-shaped.
    db = _seed_db_secrets(isolated_home, {"openai.api_key": "$u1$pretend-ciphertext"})
    reset_settings()

    with pytest.raises(typer.Exit):
        _migrate_db_to_keychain(db)


def test_recover_decrypts_keychain_back_to_plaintext(isolated_home: Path, fake_keyring) -> None:
    """`security recover` reads ciphertext sitting in keychain, decrypts with
    the user's passphrase, and writes plaintext back."""
    from unread.config import reset_settings
    from unread.secrets_backend import (
        BACKEND_DB,
        BACKEND_KEYCHAIN,
        BACKEND_PASSPHRASE,
        KEYCHAIN_SERVICE,
        read_active_backend_sync,
    )
    from unread.security.commands import cmd_recover
    from unread.security.crypto import (
        APP_SETTING_SALT,
        SALT_LEN,
        derive_key,
        encrypt_with_key,
        forget_cached_key,
        forget_process_keys,
    )
    from unread.security.passphrase import _b64encode

    db = _seed_db_secrets(isolated_home, {})
    salt = b"\x09" * SALT_LEN
    key = derive_key("rescue", salt)

    # Simulate the broken state: ciphertext in keychain, backend
    # marked as passphrase, install salt persisted.
    fake_keyring.set_password(
        KEYCHAIN_SERVICE, "openai.api_key", encrypt_with_key("sk-saved", key, salt=salt)
    )
    fake_keyring.set_password(KEYCHAIN_SERVICE, "telegram.api_id", encrypt_with_key("12345", key, salt=salt))
    _set_app_setting(db, APP_SETTING_SALT, _b64encode(salt))
    _set_app_setting(db, "secrets.backend", BACKEND_PASSPHRASE)
    _set_app_setting(db, "_meta.schema_version", "1")  # keep schema check happy

    # Reset state and pre-supply the passphrase so cmd_recover doesn't
    # try to call getpass on a non-TTY pytest stdin.
    reset_settings()
    forget_process_keys()
    forget_cached_key()
    import unread.secrets as _s

    _s._PROCESS_PASSPHRASE = "rescue"  # type: ignore[attr-defined]

    cmd_recover()

    # Plaintext is now in the keychain.
    assert fake_keyring.get_password(KEYCHAIN_SERVICE, "openai.api_key") == "sk-saved"
    assert fake_keyring.get_password(KEYCHAIN_SERVICE, "telegram.api_id") == "12345"
    # Backend was demoted from passphrase to db (tests assert end state).
    assert read_active_backend_sync(db) == BACKEND_DB

    # Cleanup so neighbouring tests start fresh.
    _s._PROCESS_PASSPHRASE = None  # type: ignore[attr-defined]
    forget_process_keys()
    forget_cached_key()
    _ = BACKEND_KEYCHAIN  # silence unused-import lint
