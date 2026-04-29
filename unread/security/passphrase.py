"""High-level glue for the passphrase backend.

The primitives in :mod:`unread.security.crypto` know nothing about the
DB or the Telethon session — they just take/return bytes/strings.
This module knits those primitives together with `data.sqlite::secrets`
and the in-process passphrase cache from :mod:`unread.secrets` so the
rest of the codebase can call one-liners:

* :func:`read_session_string` returns the decrypted Telethon session
  payload, or empty string if not yet stored.
* :func:`write_session_string_async` encrypts and persists a new
  session string after Telethon rotates auth keys.
* :func:`ensure_install_key` returns the cached install-wide
  derived key, prompting for the passphrase exactly once per process.
"""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path

from unread.security.crypto import (
    APP_SETTING_SALT,
    SALT_LEN,
    derive_key,
    encrypt_with_key,
    is_encrypted,
    lookup_key_for_salt,
    parse_envelope,
    remember_key_for_salt,
)
from unread.util.logging import get_logger

log = get_logger(__name__)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def read_install_salt(db_path: Path) -> bytes | None:
    """Sync read of `app_settings::security.kdf_salt`. Returns None when unset."""
    if not db_path.is_file():
        return None
    try:
        absolute = db_path.resolve()
        conn = sqlite3.connect(f"file:{absolute}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return None
    try:
        cur = conn.execute("SELECT value FROM app_settings WHERE key = ?", (APP_SETTING_SALT,))
        row = cur.fetchone()
    except sqlite3.Error:
        conn.close()
        return None
    conn.close()
    if not row or not row[0]:
        return None
    try:
        out = _b64decode(row[0])
    except ValueError:
        return None
    return out if len(out) == SALT_LEN else None


def ensure_install_key(db_path: Path) -> bytes:
    """Return a 32-byte key derived from the install salt + user passphrase.

    Source order:
      1. In-process cache (``crypto._PROCESS_KEYS``).
      2. Cross-invocation cache (``$XDG_RUNTIME_DIR/unread/key`` or
         ``~/.unread/.runtime/key``) — populated by
         ``unread security upgrade`` / ``unlock``. Skipped if the
         cached salt doesn't match the install salt (catches a
         post-rotation stale cache).
      3. ``getpass.getpass()`` prompt via
         :func:`unread.secrets._ensure_passphrase`.

    Same prompt-once behavior whether the call site is the secrets
    reader or the Telethon session glue.
    """
    salt = read_install_salt(db_path)
    if salt is None:
        raise RuntimeError(
            "passphrase backend is active but the install salt is missing — "
            "rerun `unread security upgrade --passphrase` to repair"
        )
    cached = lookup_key_for_salt(salt)
    if cached is not None:
        return cached
    # On-disk cache populated by `upgrade` / `unlock`. Cheap read; if
    # the cached salt matches our install salt we skip Scrypt + the
    # passphrase prompt entirely.
    from unread.security.crypto import load_cached_key

    disk_cached = load_cached_key()
    if disk_cached is not None:
        disk_salt, disk_key = disk_cached
        if disk_salt == salt:
            remember_key_for_salt(salt, disk_key)
            return disk_key

    # Late import: avoid the import cycle with `unread.secrets` at
    # module load time.
    from unread.secrets import _ensure_passphrase

    pw = _ensure_passphrase()
    key = derive_key(pw, salt)
    remember_key_for_salt(salt, key)
    return key


def read_session_string_sync(db_path: Path) -> str:
    """Return the decrypted Telethon session string, or empty if unset.

    Returns "" rather than None so the caller can pass the result
    straight into ``StringSession(...)`` without conditional logic
    — Telethon treats an empty string as "no session yet, please
    authenticate".

    Plaintext rows (during a partial migration) are passed through
    unchanged. Decrypts only when the row carries the ``$u1$`` prefix.
    """
    from unread.security.crypto import decrypt_with_key

    if not db_path.is_file():
        return ""
    try:
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return ""
    try:
        cur = conn.execute("SELECT value FROM secrets WHERE key = ?", ("telegram.session_string",))
        row = cur.fetchone()
    except sqlite3.Error:
        conn.close()
        return ""
    conn.close()
    if not row or not row[0]:
        return ""
    value = row[0]
    if not is_encrypted(value):
        return value
    env = parse_envelope(value)
    cached = lookup_key_for_salt(env.salt)
    if cached is not None:
        return decrypt_with_key(value, cached)
    # Need to derive — fetch the install key (this prompts once).
    key = ensure_install_key(db_path)
    # If the row was written before the salt was rotated, the cached
    # key won't match. Re-derive against the row's specific salt.
    try:
        return decrypt_with_key(value, key)
    except Exception:
        from unread.secrets import _ensure_passphrase
        from unread.security.crypto import decrypt

        return decrypt(value, _ensure_passphrase())


async def write_session_string_async(db_path: Path, session_string: str) -> None:
    """Encrypt ``session_string`` and store it under `telegram.session_string`.

    Uses the install salt + cached key, so the cost is one
    ChaCha20Poly1305 encrypt (microseconds). Routes through
    :class:`unread.db.repo.Repo` so the allowlist enforcement and
    schema apply.
    """
    if not session_string:
        # Caller is responsible for distinguishing "no save needed"
        # from "wipe the slot" — we always treat empty as no-op rather
        # than risk losing a valid session by writing "".
        return
    salt = read_install_salt(db_path)
    if salt is None:
        raise RuntimeError("install salt missing — run `unread security upgrade --passphrase`")
    cached = lookup_key_for_salt(salt)
    if cached is None:
        cached = ensure_install_key(db_path)
    blob = encrypt_with_key(session_string, cached, salt=salt)
    from unread.db.repo import open_repo

    async with open_repo(db_path) as repo:
        await repo.put_secrets({"telegram.session_string": blob})


__all__ = [
    "ensure_install_key",
    "read_install_salt",
    "read_session_string_sync",
    "write_session_string_async",
]
