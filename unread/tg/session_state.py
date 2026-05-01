"""Sync, no-network, no-decrypt check for Telegram session authorization.

``client.is_user_authorized()`` is the authoritative answer but it
needs telethon imported, a client built, and a connect call. This
module provides the same boolean cheaply for status panels and other
non-command surfaces that can't pay that cost.

Two backends to consider:

* file (``db`` / ``keychain``) → on-disk SQLiteSession at
  ``session_path``. Telethon writes the file on first
  ``client.connect()`` (DC info, server addresses, port — well before
  the user completes login), so file existence alone is NOT proof of
  authorization. The authoritative signal is a non-NULL ``auth_key``
  in the ``sessions`` table.
* ``passphrase`` → encrypted StringSession lives only in
  ``data.sqlite::secrets[telegram.session_string]``. The slot is
  populated only post-authorization (``tg_client``'s persist branch
  and ``cmd_upgrade``'s migration both gate on a successful auth), so
  a non-empty value is a reliable proxy. Avoids the passphrase prompt
  that decrypting would trigger.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from unread.config import Settings


def _file_session_authorized(session_path: Path) -> bool:
    """True iff the on-disk Telethon session has a non-NULL ``auth_key``."""
    candidates = [session_path, session_path.with_name(session_path.name + ".session")]
    target = next((c for c in candidates if c.exists()), None)
    if target is None:
        return False
    try:
        conn = sqlite3.connect(f"file:{target.resolve()}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return False
    try:
        cur = conn.execute(
            "SELECT 1 FROM sessions WHERE auth_key IS NOT NULL AND length(auth_key) > 0 LIMIT 1"
        )
        return cur.fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def _passphrase_session_present(data_path: Path) -> bool:
    """True iff the encrypted session_string slot is populated."""
    if not data_path.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{data_path.resolve()}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return False
    try:
        cur = conn.execute(
            "SELECT length(value) FROM secrets WHERE key = ?",
            ("telegram.session_string",),
        )
        row = cur.fetchone()
        return bool(row and row[0])
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def is_session_authorized_sync(settings: Settings) -> bool:
    """Sync proxy for ``client.is_user_authorized()`` across both backends."""
    from unread.secrets_backend import BACKEND_PASSPHRASE, read_active_backend_sync

    if read_active_backend_sync(settings.storage.data_path) == BACKEND_PASSPHRASE:
        return _passphrase_session_present(settings.storage.data_path)
    return _file_session_authorized(settings.telegram.session_path)
