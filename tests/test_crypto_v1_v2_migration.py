"""Tests for the auto-migration of v1 (`$u1$`) AEAD ciphertexts to v2 (`$u2$`).

Pre-prod blocker #2 (partial fix): new writes use the v2 slot-bound
envelope, but existing installs that ran `unread security upgrade`
before that change still have v1 rows in `data.sqlite::secrets`. A v1
row can be silently swapped between slots without detection.

The fix is a transparent rewrite: any time the user's passphrase
successfully decrypts a v1 row, the row is re-encrypted as v2 and
written back. After one read pass on a passphrase-backed install, every
secrets row carries slot-bound AAD.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from unread.security.crypto import (
    ENCRYPTED_PREFIX,
    ENCRYPTED_PREFIX_V2,
    PassphraseError,
    decrypt_with_key,
    derive_key,
    encrypt_with_key,
    envelope_version,
    migrate_v1_to_v2_with_key,
    parse_envelope,
)


def test_migrate_v1_to_v2_with_key_round_trip():
    salt = b"\x02" * 16
    key = derive_key("pp", salt)
    # v1 envelope: encrypt without slot_name.
    v1_blob = encrypt_with_key("the-secret", key, salt=salt)
    assert v1_blob.startswith(ENCRYPTED_PREFIX)
    assert envelope_version(v1_blob) == 1

    v2_blob = migrate_v1_to_v2_with_key(v1_blob, key, slot_name="openai.api_key")
    assert v2_blob.startswith(ENCRYPTED_PREFIX_V2)
    assert envelope_version(v2_blob) == 2
    # Salt is preserved so the cached-key-by-salt machinery keeps working.
    assert parse_envelope(v2_blob).salt == salt
    # Decrypts cleanly under the matching slot name.
    assert decrypt_with_key(v2_blob, key, slot_name="openai.api_key") == "the-secret"


def test_migrate_v1_to_v2_rejects_already_v2():
    salt = b"\x03" * 16
    key = derive_key("pp", salt)
    v2_blob = encrypt_with_key("v", key, salt=salt, slot_name="openai.api_key")
    with pytest.raises(ValueError, match="already v2"):
        migrate_v1_to_v2_with_key(v2_blob, key, slot_name="openai.api_key")


def test_migrate_v1_to_v2_rejects_plaintext():
    salt = b"\x04" * 16
    key = derive_key("pp", salt)
    with pytest.raises(ValueError, match="not encrypted"):
        migrate_v1_to_v2_with_key("plain-text-not-encrypted", key, slot_name="openai.api_key")


def test_migrated_v2_blocks_slot_swap():
    """The whole point — once migrated, a slot swap fails AEAD verify."""
    salt = b"\x05" * 16
    key = derive_key("pp", salt)
    v1_blob = encrypt_with_key("openai-key-value", key, salt=salt)
    v2_blob = migrate_v1_to_v2_with_key(v1_blob, key, slot_name="openai.api_key")
    with pytest.raises(PassphraseError):
        decrypt_with_key(v2_blob, key, slot_name="telegram.api_hash")


def _seed_passphrase_install(
    install_home: Path,
    *,
    passphrase: str,
    plaintext_secrets: dict[str, str],
    legacy_v1: bool = True,
) -> Path:
    """Build a minimal data.sqlite that looks like a passphrase-backend install.

    Used by the migration smoke tests so we don't have to drive the
    full `cmd_upgrade` path (which prompts on the TTY). When
    `legacy_v1=True`, encrypts each slot with the v1 envelope (no
    slot_name) — emulating an install upgraded before the v2 ship.
    """
    import base64
    from datetime import UTC, datetime

    from unread.db._keys import OVERRIDE_KEYS, SECRET_KEYS

    storage = install_home / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    db_path = storage / "data.sqlite"

    salt = b"\x10" * 16
    key = derive_key(passphrase, salt)
    salt_b64 = base64.urlsafe_b64encode(salt).rstrip(b"=").decode("ascii")
    now = datetime.now(UTC).isoformat()

    # Verify allowlists at test time so a typo in this fixture flags
    # against the real schema instead of seeding an unreadable row.
    assert "security.kdf_salt" in OVERRIDE_KEYS
    assert "secrets.backend" in OVERRIDE_KEYS
    for slot in plaintext_secrets:
        assert slot in SECRET_KEYS, slot

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_settings(
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS secrets(
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?)",
            ("security.kdf_salt", salt_b64, now),
        )
        conn.execute(
            "INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?)",
            ("secrets.backend", "passphrase", now),
        )
        for slot, value in plaintext_secrets.items():
            if legacy_v1:
                blob = encrypt_with_key(value, key, salt=salt)  # no slot_name → v1
            else:
                blob = encrypt_with_key(value, key, salt=salt, slot_name=slot)
            conn.execute(
                "INSERT INTO secrets(key, value, updated_at) VALUES(?, ?, ?)",
                (slot, blob, now),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _read_back_secrets(db_path: Path) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT key, value FROM secrets ORDER BY key").fetchall()
    finally:
        conn.close()
    return dict(rows)


def test_read_passphrase_secrets_auto_migrates_v1_to_v2(tmp_path, monkeypatch):
    """End-to-end: reading a passphrase install with v1 rows transparently
    upgrades every row to v2. The plaintext returned to the caller is
    unchanged; the on-disk envelopes change shape."""
    install_home = tmp_path / "home"
    db_path = _seed_passphrase_install(
        install_home,
        passphrase="pp",
        plaintext_secrets={
            "openai.api_key": "sk-openai-real-value",
            "anthropic.api_key": "sk-ant-real-value",
            "telegram.api_hash": "telegram-hash-value",
        },
        legacy_v1=True,
    )

    # Sanity: pre-migration shape is v1 across the board.
    pre = _read_back_secrets(db_path)
    assert all(v.startswith(ENCRYPTED_PREFIX) for v in pre.values())
    assert all(envelope_version(v) == 1 for v in pre.values())

    # Drive the read path. Pre-supply the passphrase so the test doesn't
    # block on a TTY prompt.
    monkeypatch.setenv("UNREAD_PASSPHRASE", "pp")
    # `read_secrets` reads through `settings.storage.data_path` — point
    # the singleton at our test install.
    monkeypatch.setenv("UNREAD_HOME", str(install_home))
    from unread.config import reset_settings

    reset_settings()
    import unread.secrets as _secrets
    from unread.config import get_settings
    from unread.secrets import read_secrets

    _secrets._PROCESS_PASSPHRASE = None  # ensure the env var is what's read
    settings = get_settings()
    out = read_secrets(settings)
    assert out["openai.api_key"] == "sk-openai-real-value"
    assert out["anthropic.api_key"] == "sk-ant-real-value"
    assert out["telegram.api_hash"] == "telegram-hash-value"

    # Post-migration: every row is now v2.
    post = _read_back_secrets(db_path)
    assert all(v.startswith(ENCRYPTED_PREFIX_V2) for v in post.values()), post
    assert all(envelope_version(v) == 2 for v in post.values())

    # And the v2 rows refuse a slot swap (the whole point of v2).
    salt = parse_envelope(post["openai.api_key"]).salt
    key = derive_key("pp", salt)
    with pytest.raises(PassphraseError):
        decrypt_with_key(post["openai.api_key"], key, slot_name="telegram.api_hash")


def test_read_passphrase_secrets_skips_when_already_v2(tmp_path, monkeypatch):
    """Idempotent: a fully-v2 install does no rewrites on read."""
    install_home = tmp_path / "home"
    db_path = _seed_passphrase_install(
        install_home,
        passphrase="pp",
        plaintext_secrets={"openai.api_key": "sk-test-already-v2"},
        legacy_v1=False,  # already v2
    )
    pre = _read_back_secrets(db_path)
    pre_value = pre["openai.api_key"]
    assert pre_value.startswith(ENCRYPTED_PREFIX_V2)

    monkeypatch.setenv("UNREAD_PASSPHRASE", "pp")
    monkeypatch.setenv("UNREAD_HOME", str(install_home))
    from unread.config import reset_settings

    reset_settings()
    import unread.secrets as _secrets

    _secrets._PROCESS_PASSPHRASE = None
    from unread.config import get_settings
    from unread.secrets import read_secrets

    settings = get_settings()
    assert read_secrets(settings)["openai.api_key"] == "sk-test-already-v2"

    # The on-disk row is byte-for-byte unchanged.
    post = _read_back_secrets(db_path)
    assert post["openai.api_key"] == pre_value
