"""Phase 3 — passphrase crypto primitives + key cache.

Covers the building blocks that the higher-level passphrase backend
(`unread.security.passphrase`, `unread.secrets`) relies on:

- encrypt → decrypt round-trip with the same passphrase succeeds.
- decrypt with the wrong passphrase raises `PassphraseError`.
- ciphertext format always starts with the `$u1$` prefix.
- Each call generates a fresh salt+nonce (no deterministic re-encrypt).
- `derive_key` rejects a salt of the wrong length.
- The cross-invocation key cache writes / reads / honours TTL / wipes
  expired entries on read.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_runtime(monkeypatch, tmp_path):
    """Pin XDG_RUNTIME_DIR + UNREAD_HOME so the cross-invocation cache
    doesn't trample the developer's real ~/.unread/.runtime/ during the
    test run."""
    monkeypatch.setenv("UNREAD_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / "runtime").mkdir(parents=True, exist_ok=True)
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    from unread.config import reset_settings
    from unread.security.crypto import forget_cached_key, forget_process_keys

    reset_settings()
    forget_process_keys()
    forget_cached_key()
    yield
    forget_process_keys()
    forget_cached_key()


def test_encrypt_decrypt_round_trip() -> None:
    from unread.security.crypto import decrypt, encrypt

    blob = encrypt("sk-secret-value", "correct horse battery staple")
    assert blob.startswith("$u1$")
    assert decrypt(blob, "correct horse battery staple") == "sk-secret-value"


def test_wrong_passphrase_raises() -> None:
    from unread.security.crypto import PassphraseError, decrypt, encrypt

    blob = encrypt("payload", "right")
    with pytest.raises(PassphraseError):
        decrypt(blob, "wrong")


def test_each_encrypt_uses_fresh_salt_nonce() -> None:
    from unread.security.crypto import encrypt

    a = encrypt("same-text", "pw")
    b = encrypt("same-text", "pw")
    assert a != b  # randomised salt / nonce → different ciphertexts


def test_derive_key_rejects_wrong_salt_length() -> None:
    from unread.security.crypto import SALT_LEN, derive_key

    with pytest.raises(ValueError):
        derive_key("pw", os.urandom(SALT_LEN - 1))
    # Correct length succeeds.
    out = derive_key("pw", os.urandom(SALT_LEN))
    assert len(out) == 32


def test_decrypt_with_key_round_trip() -> None:
    from unread.security.crypto import (
        SALT_LEN,
        decrypt_with_key,
        derive_key,
        encrypt_with_key,
    )

    salt = os.urandom(SALT_LEN)
    key = derive_key("pw", salt)
    blob = encrypt_with_key("plain", key, salt=salt)
    assert decrypt_with_key(blob, key) == "plain"


def test_decrypt_with_key_wrong_key_raises() -> None:
    from unread.security.crypto import (
        SALT_LEN,
        PassphraseError,
        decrypt_with_key,
        derive_key,
        encrypt_with_key,
    )

    salt = os.urandom(SALT_LEN)
    blob = encrypt_with_key("plain", derive_key("right", salt), salt=salt)
    wrong = derive_key("wrong", salt)
    with pytest.raises(PassphraseError):
        decrypt_with_key(blob, wrong)


def test_cache_round_trip(tmp_path) -> None:
    from unread.security.crypto import KEY_LEN, SALT_LEN, load_cached_key, store_cached_key

    salt = os.urandom(SALT_LEN)
    key = os.urandom(KEY_LEN)
    path = store_cached_key(key, salt, ttl_seconds=None)
    assert Path(path).is_file()
    # Mode is 0o600.
    assert (path.stat().st_mode & 0o777) == 0o600

    loaded = load_cached_key()
    assert loaded is not None
    salt_loaded, key_loaded = loaded
    assert salt_loaded == salt
    assert key_loaded == key


def test_cache_expiry_wipes_on_read() -> None:
    from unread.security.crypto import (
        KEY_LEN,
        SALT_LEN,
        _cache_path,
        load_cached_key,
        store_cached_key,
    )

    salt = os.urandom(SALT_LEN)
    key = os.urandom(KEY_LEN)
    store_cached_key(key, salt, ttl_seconds=1)
    # Sleep past the expiry. The cache file is on local disk so a
    # real sleep is fine here — well under a second of test cost.
    time.sleep(1.1)
    assert load_cached_key() is None
    # Stale file gets wiped on the read.
    assert not _cache_path().is_file()


def test_forget_cached_key_returns_true_only_when_file_existed() -> None:
    from unread.security.crypto import (
        KEY_LEN,
        SALT_LEN,
        forget_cached_key,
        store_cached_key,
    )

    assert forget_cached_key() is False
    store_cached_key(os.urandom(KEY_LEN), os.urandom(SALT_LEN), ttl_seconds=None)
    assert forget_cached_key() is True
    assert forget_cached_key() is False


def test_is_encrypted_distinguishes_format() -> None:
    from unread.security.crypto import encrypt, is_encrypted

    assert is_encrypted(encrypt("x", "pw")) is True
    assert is_encrypted("plain") is False
    assert is_encrypted("") is False
    assert is_encrypted(None) is False
