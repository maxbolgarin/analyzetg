"""Tests for AEAD envelope migration on read.

Three envelope generations exist on disk:

  - ``$u1$`` — original. AEAD AAD = ``None``. Slot swap and framing
    tamper both go undetected until AEAD verify fires.
  - ``$u2$`` — slot-bound. AEAD AAD = ``"unread:v2:" + slot_name``. Slot
    swaps fail ``InvalidTag``; framing bytes are still un-bound.
  - ``$u3$`` — slot + framing-bound. AEAD AAD = ``"unread:v3:" +
    slot_name + salt + nonce``. Both slot swap and framing tamper fail.

New writes always emit ``$u3$``. Auto-migration on read upgrades v1 and
v2 rows to v3 in a single rewrite.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from unread.security.crypto import (
    ENCRYPTED_PREFIX,
    ENCRYPTED_PREFIX_V2,
    ENCRYPTED_PREFIX_V3,
    PassphraseError,
    decrypt_with_key,
    derive_key,
    encrypt_with_key,
    envelope_version,
    migrate_to_v3_with_key,
    migrate_v1_to_v2_with_key,
    parse_envelope,
)


def test_migrate_v1_to_v2_with_key_round_trip():
    salt = b"\x02" * 16
    key = derive_key("pp", salt)
    v1_blob = encrypt_with_key("the-secret", key, salt=salt)
    assert v1_blob.startswith(ENCRYPTED_PREFIX)
    assert envelope_version(v1_blob) == 1

    v2_blob = migrate_v1_to_v2_with_key(v1_blob, key, slot_name="openai.api_key")
    assert v2_blob.startswith(ENCRYPTED_PREFIX_V2)
    assert envelope_version(v2_blob) == 2
    # Salt is preserved so the cached-key-by-salt machinery keeps working.
    assert parse_envelope(v2_blob).salt == salt
    assert decrypt_with_key(v2_blob, key, slot_name="openai.api_key") == "the-secret"


def test_migrate_v1_to_v2_rejects_already_v2():
    salt = b"\x03" * 16
    key = derive_key("pp", salt)
    # encrypt_with_key(slot_name=...) emits v3 by default; force v2 via
    # the explicit v1→v2 helper to seed a truly-v2 row.
    v1 = encrypt_with_key("v", key, salt=salt)
    v2_blob = migrate_v1_to_v2_with_key(v1, key, slot_name="openai.api_key")
    with pytest.raises(ValueError, match="already v2 or newer"):
        migrate_v1_to_v2_with_key(v2_blob, key, slot_name="openai.api_key")


def test_migrate_v1_to_v2_rejects_already_v3():
    salt = b"\x06" * 16
    key = derive_key("pp", salt)
    v3_blob = encrypt_with_key("v", key, salt=salt, slot_name="openai.api_key")
    with pytest.raises(ValueError, match="already v2 or newer"):
        migrate_v1_to_v2_with_key(v3_blob, key, slot_name="openai.api_key")


def test_migrate_v1_to_v2_rejects_plaintext():
    salt = b"\x04" * 16
    key = derive_key("pp", salt)
    with pytest.raises(ValueError, match="not encrypted"):
        migrate_v1_to_v2_with_key("plain-text-not-encrypted", key, slot_name="openai.api_key")


def test_migrated_v2_blocks_slot_swap():
    """v2: once migrated, a slot swap fails AEAD verify."""
    salt = b"\x05" * 16
    key = derive_key("pp", salt)
    v1_blob = encrypt_with_key("openai-key-value", key, salt=salt)
    v2_blob = migrate_v1_to_v2_with_key(v1_blob, key, slot_name="openai.api_key")
    with pytest.raises(PassphraseError):
        decrypt_with_key(v2_blob, key, slot_name="telegram.api_hash")


# ---- v3 -----------------------------------------------------------------


def test_encrypt_with_slot_name_emits_v3():
    salt = b"\x07" * 16
    key = derive_key("pp", salt)
    blob = encrypt_with_key("v", key, salt=salt, slot_name="openai.api_key")
    assert blob.startswith(ENCRYPTED_PREFIX_V3)
    assert envelope_version(blob) == 3


def test_v3_round_trip():
    salt = b"\x08" * 16
    key = derive_key("pp", salt)
    blob = encrypt_with_key("the-secret-v3", key, salt=salt, slot_name="openai.api_key")
    assert decrypt_with_key(blob, key, slot_name="openai.api_key") == "the-secret-v3"


def test_v3_blocks_slot_swap():
    salt = b"\x09" * 16
    key = derive_key("pp", salt)
    blob = encrypt_with_key("v", key, salt=salt, slot_name="openai.api_key")
    with pytest.raises(PassphraseError):
        decrypt_with_key(blob, key, slot_name="telegram.api_hash")


def test_v3_blocks_salt_tamper():
    """Flipping a salt byte trips InvalidTag — v2 was implicit-only."""
    import base64

    salt = b"\x0a" * 16
    key = derive_key("pp", salt)
    blob = encrypt_with_key("v", key, salt=salt, slot_name="openai.api_key")
    body = base64.urlsafe_b64decode(blob[len(ENCRYPTED_PREFIX_V3) :] + "==")
    tampered_body = bytes([body[0] ^ 0x01]) + body[1:]
    tampered = ENCRYPTED_PREFIX_V3 + base64.urlsafe_b64encode(tampered_body).rstrip(b"=").decode()
    with pytest.raises(PassphraseError):
        decrypt_with_key(tampered, key, slot_name="openai.api_key")


def test_v3_blocks_nonce_tamper():
    import base64

    salt = b"\x0b" * 16
    key = derive_key("pp", salt)
    blob = encrypt_with_key("v", key, salt=salt, slot_name="openai.api_key")
    body = bytearray(base64.urlsafe_b64decode(blob[len(ENCRYPTED_PREFIX_V3) :] + "=="))
    body[16] ^= 0x01  # nonce starts at offset 16 (after the 16-byte salt)
    tampered = ENCRYPTED_PREFIX_V3 + base64.urlsafe_b64encode(bytes(body)).rstrip(b"=").decode()
    with pytest.raises(PassphraseError):
        decrypt_with_key(tampered, key, slot_name="openai.api_key")


def test_migrate_v1_to_v3():
    salt = b"\x0c" * 16
    key = derive_key("pp", salt)
    v1 = encrypt_with_key("from-v1", key, salt=salt)
    v3 = migrate_to_v3_with_key(v1, key, slot_name="openai.api_key")
    assert v3.startswith(ENCRYPTED_PREFIX_V3)
    assert parse_envelope(v3).salt == salt  # salt preserved
    assert decrypt_with_key(v3, key, slot_name="openai.api_key") == "from-v1"


def test_migrate_v2_to_v3():
    salt = b"\x0d" * 16
    key = derive_key("pp", salt)
    v1 = encrypt_with_key("from-v2", key, salt=salt)
    v2 = migrate_v1_to_v2_with_key(v1, key, slot_name="openai.api_key")
    v3 = migrate_to_v3_with_key(v2, key, slot_name="openai.api_key")
    assert v3.startswith(ENCRYPTED_PREFIX_V3)
    assert parse_envelope(v3).salt == salt
    assert decrypt_with_key(v3, key, slot_name="openai.api_key") == "from-v2"


def test_migrate_to_v3_rejects_already_v3():
    salt = b"\x0e" * 16
    key = derive_key("pp", salt)
    v3 = encrypt_with_key("v", key, salt=salt, slot_name="openai.api_key")
    with pytest.raises(ValueError, match="already v3"):
        migrate_to_v3_with_key(v3, key, slot_name="openai.api_key")


def test_migrate_to_v3_rejects_plaintext():
    salt = b"\x0f" * 16
    key = derive_key("pp", salt)
    with pytest.raises(ValueError, match="not encrypted"):
        migrate_to_v3_with_key("plain", key, slot_name="openai.api_key")


# ---- end-to-end through `read_secrets` ------------------------------------


def _seed_passphrase_install(
    install_home: Path,
    *,
    passphrase: str,
    plaintext_secrets: dict[str, str],
    seed_version: int = 1,
) -> Path:
    """Build a minimal data.sqlite that looks like a passphrase-backend install.

    ``seed_version`` controls the on-disk envelope: 1 → ``$u1$``,
    2 → ``$u2$``, 3 → ``$u3$``. Pre-v3 installs are what the
    auto-migration path on read is supposed to upgrade.
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
            if seed_version == 1:
                blob = encrypt_with_key(value, key, salt=salt)
            elif seed_version == 2:
                v1 = encrypt_with_key(value, key, salt=salt)
                blob = migrate_v1_to_v2_with_key(v1, key, slot_name=slot)
            elif seed_version == 3:
                blob = encrypt_with_key(value, key, salt=salt, slot_name=slot)
            else:  # pragma: no cover - test misuse
                raise ValueError(f"unknown seed_version {seed_version}")
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


def _drive_read(install_home: Path, monkeypatch, passphrase: str) -> dict[str, str]:
    monkeypatch.setenv("UNREAD_PASSPHRASE", passphrase)
    monkeypatch.setenv("UNREAD_HOME", str(install_home))
    from unread.config import get_settings, reset_settings

    reset_settings()
    import unread.secrets as _secrets
    from unread.secrets import read_secrets

    _secrets._PROCESS_PASSPHRASE = None
    return read_secrets(get_settings())


def test_read_passphrase_secrets_auto_migrates_v1_to_v3(tmp_path, monkeypatch):
    """End-to-end: a v1 install transparently upgrades to v3 on first read."""
    install_home = tmp_path / "home"
    db_path = _seed_passphrase_install(
        install_home,
        passphrase="pp",
        plaintext_secrets={
            "openai.api_key": "sk-openai-real-value",
            "anthropic.api_key": "sk-ant-real-value",
            "telegram.api_hash": "telegram-hash-value",
        },
        seed_version=1,
    )

    pre = _read_back_secrets(db_path)
    assert all(envelope_version(v) == 1 for v in pre.values())

    out = _drive_read(install_home, monkeypatch, "pp")
    assert out["openai.api_key"] == "sk-openai-real-value"
    assert out["anthropic.api_key"] == "sk-ant-real-value"
    assert out["telegram.api_hash"] == "telegram-hash-value"

    post = _read_back_secrets(db_path)
    assert all(v.startswith(ENCRYPTED_PREFIX_V3) for v in post.values()), post
    assert all(envelope_version(v) == 3 for v in post.values())

    # The post-v3 row refuses both slot swap and framing tamper.
    salt = parse_envelope(post["openai.api_key"]).salt
    key = derive_key("pp", salt)
    with pytest.raises(PassphraseError):
        decrypt_with_key(post["openai.api_key"], key, slot_name="telegram.api_hash")


def test_read_passphrase_secrets_auto_migrates_v2_to_v3(tmp_path, monkeypatch):
    """A v2 install (slot-bound but framing-unbound) upgrades to v3."""
    install_home = tmp_path / "home"
    db_path = _seed_passphrase_install(
        install_home,
        passphrase="pp",
        plaintext_secrets={"openai.api_key": "sk-from-v2"},
        seed_version=2,
    )
    pre = _read_back_secrets(db_path)
    assert envelope_version(pre["openai.api_key"]) == 2

    out = _drive_read(install_home, monkeypatch, "pp")
    assert out["openai.api_key"] == "sk-from-v2"

    post = _read_back_secrets(db_path)
    assert envelope_version(post["openai.api_key"]) == 3


def test_read_passphrase_secrets_skips_when_already_v3(tmp_path, monkeypatch):
    """Idempotent: a fully-v3 install does no rewrites on read."""
    install_home = tmp_path / "home"
    db_path = _seed_passphrase_install(
        install_home,
        passphrase="pp",
        plaintext_secrets={"openai.api_key": "sk-test-already-v3"},
        seed_version=3,
    )
    pre = _read_back_secrets(db_path)
    pre_value = pre["openai.api_key"]
    assert pre_value.startswith(ENCRYPTED_PREFIX_V3)

    out = _drive_read(install_home, monkeypatch, "pp")
    assert out["openai.api_key"] == "sk-test-already-v3"

    post = _read_back_secrets(db_path)
    assert post["openai.api_key"] == pre_value
