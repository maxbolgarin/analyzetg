"""Pluggable storage backend for the allowlisted credentials.

The default backend is the on-disk ``data.sqlite::secrets`` table —
plaintext, protected only by ``~/.unread/`` permissions. The keychain
backend stores each value in the per-user OS credential store
(macOS Keychain / Linux Secret Service / Windows Credential Manager)
which is encrypted at rest with a key bound to the user's login
session.

`unread.secrets.read_secrets` consults whichever backend is active at
startup. The choice itself lives in ``app_settings::secrets.backend``
(NOT in ``secrets`` — the choice isn't sensitive, only its targets
are). `unread security migrate` flips the active backend and moves
existing values across; users never edit it by hand.

Every keyring call is wrapped in a defensive ``try/except``: a missing
DBus session, an out-of-process keychain lockout, or a corrupted
backend store all degrade to "no overlay" rather than failing the
calling command. The active-backend read is cached per-process to
avoid hitting SQLite on every secret lookup during a single CLI run.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from unread.db._keys import SECRET_KEYS as _SECRET_KEYS
from unread.util.logging import get_logger

log = get_logger(__name__)

# Identifiers used when calling into `keyring`. The service name groups
# all of unread's slots under a single Keychain item / Secret-Service
# collection entry, making it easy for the user to inspect or revoke
# them ("unread" appears once in Keychain Access, with one row per
# allowlisted key).
KEYCHAIN_SERVICE = "unread"

# Backend identifiers persisted in `app_settings::secrets.backend`.
# Phase 3 will add ``BACKEND_PASSPHRASE``; the constant is reserved
# here so we don't drift on the spelling later.
BACKEND_DB = "db"
BACKEND_KEYCHAIN = "keychain"
BACKEND_PASSPHRASE = "passphrase"

_VALID_BACKENDS: frozenset[str] = frozenset({BACKEND_DB, BACKEND_KEYCHAIN, BACKEND_PASSPHRASE})


def is_valid_backend(name: str) -> bool:
    return name in _VALID_BACKENDS


def keychain_available() -> bool:
    """True iff `keyring` resolved to a real OS-backed implementation.

    The pure-Python ``keyring.backends.fail.Keyring`` and
    ``keyring.backends.null.Keyring`` are sentinels meaning "no native
    store on this host" — we treat both as unavailable so the wizard
    doesn't offer a choice that will silently fail at write time.
    """
    try:
        import keyring
        from keyring.backends import fail, null

        active = keyring.get_keyring()
    except Exception as e:
        log.debug("secrets_backend.keychain_probe_failed", err=str(e)[:200])
        return False
    return not isinstance(active, fail.Keyring | null.Keyring)


def keychain_describe() -> str:
    """Human-readable name of the currently active keyring backend.

    Returned by `unread security status` so the user sees which native
    store their secrets are heading into ("macOS Keychain",
    "Linux Secret Service", "Windows Credential Manager", etc.)
    rather than guessing from the platform.
    """
    try:
        import keyring

        active = keyring.get_keyring()
    except Exception as e:
        return f"unavailable ({str(e)[:80]})"
    return type(active).__module__ + "." + type(active).__name__


def keychain_read(key: str) -> str | None:
    """Return the value stored under ``key`` in the OS keychain, or None.

    Only allowlisted keys are accepted — silently returning None for
    anything else stops a typo in a Python repl from spelunking
    arbitrary credentials out of the user's keychain.
    """
    if key not in _SECRET_KEYS:
        return None
    try:
        import keyring

        return keyring.get_password(KEYCHAIN_SERVICE, key)
    except Exception as e:
        log.warning("secrets_backend.keychain_read_failed", key=key, err=str(e)[:200])
        return None


def keychain_write(key: str, value: str) -> bool:
    """Persist ``value`` under ``key``. False if the keychain refused.

    Empty values are written through (lets ``unread security migrate``
    blank a slot rather than leaving stale ciphertext behind).
    Allowlist-enforced — same as the DB backend.
    """
    if key not in _SECRET_KEYS:
        raise ValueError(f"unknown secret key: {key!r}; allowed: {sorted(_SECRET_KEYS)}")
    try:
        import keyring

        keyring.set_password(KEYCHAIN_SERVICE, key, value)
        return True
    except Exception as e:
        log.warning("secrets_backend.keychain_write_failed", key=key, err=str(e)[:200])
        return False


def keychain_delete(key: str) -> bool:
    """Remove ``key`` from the keychain. False if it wasn't there or the call failed."""
    if key not in _SECRET_KEYS:
        return False
    try:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(KEYCHAIN_SERVICE, key)
        except PasswordDeleteError:
            return False
        return True
    except Exception as e:
        log.warning("secrets_backend.keychain_delete_failed", key=key, err=str(e)[:200])
        return False


def read_active_backend_sync(db_path: Path | str) -> str:
    """Read ``app_settings::secrets.backend`` without an event loop.

    Used at `load_settings` time to decide which store
    `read_secrets` should consult. Defensive on every error: missing
    file / locked DB / missing table / unrecognised value all fall
    through to ``BACKEND_DB`` (the historic default) so a half-set-up
    install never crashes at import time.
    """
    target = Path(db_path)
    if not target.is_file():
        return BACKEND_DB
    try:
        absolute = target.resolve()
        conn = sqlite3.connect(f"file:{absolute}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return BACKEND_DB
    try:
        cur = conn.execute("SELECT value FROM app_settings WHERE key = ?", ("secrets.backend",))
        row = cur.fetchone()
    except sqlite3.Error:
        conn.close()
        return BACKEND_DB
    conn.close()
    if not row:
        return BACKEND_DB
    value = (row[0] or "").strip().lower()
    return value if value in _VALID_BACKENDS else BACKEND_DB
