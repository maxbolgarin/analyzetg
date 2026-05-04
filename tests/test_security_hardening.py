"""Pre-prod regressions on `unread/security/` and `unread/secrets.py`.

Covers:
  * `_b64decode` rejects non-base64 inputs (validate=True).
  * `runtime_key_cache_path` is a public stable name (used by killme).
  * `DEFAULT_KEY_CACHE_TTL_SEC` is a sane positive number.
  * `SCRYPT_N` is at the modern (2026) recommended cost.
  * `keychain_service` always namespaces by install home so two installs
    on the same OS user don't clobber each other's keychain entries
    AND no other Python process can `keyring.get_password("unread", ...)`
    out of the user's keychain.
  * `_read_db_secrets_passphrase` zeroizes `_PROCESS_PASSPHRASE` after
    deriving the key.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from unread.security.crypto import (
    DEFAULT_KEY_CACHE_TTL_SEC,
    PRODUCTION_SCRYPT_N,
    PassphraseError,
    _b64decode,
    runtime_key_cache_path,
)


def test_b64decode_rejects_non_base64():
    """A malformed envelope used to silently produce partial bytes that
    later tripped InvalidTag and wasted a Scrypt cycle. validate=True
    means malformed input fails at parse time."""
    with pytest.raises(PassphraseError, match="malformed base64"):
        _b64decode("!!!not-base64!!!")


def test_b64decode_accepts_unpadded_urlsafe():
    # Round-trip through the canonical encoding to make sure validate=True
    # doesn't reject the legitimate format.
    import base64

    payload = b"\x01\x02\x03"
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    assert _b64decode(encoded) == payload


def test_runtime_key_cache_path_is_public():
    """Public alias used by killme to clean up the cached key. A rename
    of the private helper must not silently break the cleanup path."""
    p = runtime_key_cache_path()
    assert isinstance(p, Path)
    assert p.name == "key"


def test_default_key_cache_ttl_is_sane():
    """At least 1 minute, at most 24 hours. The pre-prod review flagged
    `None` (== until-lock) as too long for a default; 30 minutes is the
    chosen middle ground."""
    assert 60 <= DEFAULT_KEY_CACHE_TTL_SEC <= 24 * 3600


def test_scrypt_n_is_modern():
    """2026 recommendation is N >= 2^18 for high-value targets.

    We assert the production constant, not the runtime `SCRYPT_N` —
    the test suite drops the runtime value via `UNREAD_SCRYPT_N` to
    keep crypto round-trip tests fast. The production default is what
    ships to users.
    """
    assert PRODUCTION_SCRYPT_N >= 2**18


def test_keychain_service_default_install_is_namespaced(monkeypatch, tmp_path):
    """Default install (`UNREAD_HOME` resolves under `~/.unread`) is
    ALSO namespaced — every install gets `unread:<install_id>` so a
    rogue Python process can't fetch credentials by guessing the bare
    `"unread"` service name."""
    user_home = tmp_path / "home"
    (user_home / ".unread").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    monkeypatch.setenv("UNREAD_HOME", str(user_home / ".unread"))
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(user_home) if p == "~" else p)
    from unread.config import reset_settings

    reset_settings()
    from unread.secrets_backend import _reset_keychain_service_cache, keychain_service

    _reset_keychain_service_cache()
    name = keychain_service()
    assert name.startswith("unread:")
    assert len(name.split(":")[1]) == 12  # 12-char sha256 prefix


def test_keychain_service_custom_install_differs_from_default(monkeypatch, tmp_path):
    """Two installs at different paths get DIFFERENT service names so
    dev + prod (or two cwd-bound installs) coexist on the same OS user
    without clobbering each other's credentials."""
    user_home = tmp_path / "home"
    user_home.mkdir()
    default_path = user_home / ".unread"
    default_path.mkdir()
    custom = tmp_path / "dev_install"
    custom.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(user_home) if p == "~" else p)
    from unread.config import reset_settings
    from unread.secrets_backend import _reset_keychain_service_cache, keychain_service

    monkeypatch.setenv("UNREAD_HOME", str(default_path))
    reset_settings()
    _reset_keychain_service_cache()
    default_name = keychain_service()

    monkeypatch.setenv("UNREAD_HOME", str(custom))
    reset_settings()
    _reset_keychain_service_cache()
    custom_name = keychain_service()

    assert default_name != custom_name
    assert default_name.startswith("unread:")
    assert custom_name.startswith("unread:")


def test_passphrase_zeroized_after_decrypt(monkeypatch, tmp_path):
    """After `_read_db_secrets_passphrase` returns, the cached
    passphrase string must be cleared. Subsequent decrypts work via
    the cached salt-derived key."""
    import sqlite3

    import unread.secrets as secrets_mod
    from unread.security.crypto import APP_SETTING_SALT

    # Build a tiny sqlite that looks like data.sqlite — install salt in
    # app_settings + one encrypted row in secrets. Use the real encrypt
    # path (via derive_key + encrypt_with_key below) so the resulting
    # envelope round-trips through the real decrypt path.
    db = tmp_path / "data.sqlite"
    salt = os.urandom(16)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    conn.execute("CREATE TABLE secrets (key TEXT PRIMARY KEY, value TEXT)")
    import base64

    salt_b64 = base64.urlsafe_b64encode(salt).rstrip(b"=").decode("ascii")
    conn.execute(
        "INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,?)",
        (APP_SETTING_SALT, salt_b64, "2026-01-01"),
    )
    # Re-encrypt with the install salt so the install-salt fast path
    # kicks in (matches the real upgrade flow).
    from unread.security.crypto import derive_key, encrypt_with_key

    install_key = derive_key("the-passphrase", salt)
    install_ct = encrypt_with_key("sk-real", install_key, salt=salt)
    conn.execute("INSERT INTO secrets(key,value) VALUES(?,?)", ("openai.api_key", install_ct))
    conn.commit()
    conn.close()

    # Pre-load the in-process passphrase as if the user just typed it.
    secrets_mod._PROCESS_PASSPHRASE = "the-passphrase"
    out = secrets_mod._read_db_secrets_passphrase(db)
    assert out["openai.api_key"] == "sk-real"
    # Critical assertion: the passphrase must have been wiped.
    assert secrets_mod._PROCESS_PASSPHRASE is None
    # Cleanup
    from unread.security.crypto import forget_process_keys

    forget_process_keys()
