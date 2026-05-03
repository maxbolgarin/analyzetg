"""Persisted credentials read at settings-load time.

Authoritative storage is the `secrets` table in `~/.unread/storage/data.sqlite`
— populated by `unread init` (the interactive wizard) or programmatically
via `Repo.put_secrets`. Persisting these means a user can blow away
`~/.unread/.env` after a successful first-run setup and the CLI keeps
working.

Backend selection (DB / OS keychain / passphrase-encrypted) is read
from ``app_settings::secrets.backend`` at every call: this lets
``unread security migrate`` flip the source without requiring a
process restart.

Within `load_settings`, the values fill ONLY fields the higher
precedence layers (env / `.env` / `config.toml`) left empty — so a
populated `.env` always wins on rotation.

All reads are defensive: missing files, locked DBs, missing tables, or
schema-mismatch rows degrade to "no overlay" silently. Credentials
disappearing inside a corrupt DB is bad UX, but a noisy crash here
(at import time, before Typer constructs the app) is worse — `.env` is
always available as a manual override.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from unread.db._keys import SECRET_KEYS as _SECRET_KEYS_SET

# `_keys.SECRET_KEYS` is a frozenset (best for membership checks) but
# this module needs an ordered, parameterizable form to splat into a
# `WHERE key IN (?, ?, …)` query. Tuple-ize once at module load.
_SECRET_KEYS: tuple[str, ...] = tuple(sorted(_SECRET_KEYS_SET))


# Per-process passphrase cache. Holds the user-typed passphrase between
# the first prompt and any subsequent `read_secrets` call within the
# same process — so a single command that triggers multiple settings
# reloads doesn't ask for the passphrase repeatedly. NEVER persisted.
_PROCESS_PASSPHRASE: str | None = None


def _read_install_salt(db_path: Path) -> bytes | None:
    """Fetch the install's KDF salt from `app_settings::security.kdf_salt`.

    Returns the raw bytes (decoded from base64) or None if the salt
    isn't recorded — which means either the install hasn't been
    upgraded to the passphrase backend yet, or the row was wiped on
    `downgrade`.
    """
    import base64

    from unread.security.crypto import APP_SETTING_SALT

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
        pad = "=" * (-len(row[0]) % 4)
        return base64.urlsafe_b64decode(row[0] + pad)
    except ValueError:
        return None


def _ensure_passphrase() -> str:
    """Return the user's passphrase, prompting interactively if not cached.

    Source order:
      1. `_PROCESS_PASSPHRASE` (in-memory, set by a previous prompt
         in the same process).
      2. ``UNREAD_PASSPHRASE`` env var (lets scripts / cron pre-supply
         a passphrase without an interactive shell).
      3. ``getpass.getpass()`` prompt — only when stdin/stdout are a TTY.

    Raises ``RuntimeError`` when no source produces a passphrase
    (non-interactive context with no env var). This is preferable to
    deadlocking on stdin or returning an empty string that would then
    fail with a misleading "passphrase didn't decrypt" error.
    """
    global _PROCESS_PASSPHRASE
    if _PROCESS_PASSPHRASE:
        return _PROCESS_PASSPHRASE
    env_value = (os.environ.get("UNREAD_PASSPHRASE") or "").strip()
    if env_value:
        _PROCESS_PASSPHRASE = env_value
        return env_value
    import sys

    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise RuntimeError(
            "passphrase backend is active but no passphrase available — "
            "set UNREAD_PASSPHRASE or run `unread security unlock` from a TTY"
        )
    import getpass

    pw = getpass.getpass("unread passphrase: ").strip()
    if not pw:
        raise RuntimeError("empty passphrase supplied")
    _PROCESS_PASSPHRASE = pw
    return pw


def _read_db_secrets_passphrase(db_path: Path) -> dict[str, str]:
    """Decrypt every encrypted slot in ``data.sqlite::secrets``.

    Plaintext slots (e.g. left over from a partial migration) are
    passed through unchanged so a half-broken install still surfaces
    something rather than mass-failing. Raises whatever
    ``_ensure_passphrase`` raises on non-TTY contexts; lets a
    ``PassphraseError`` propagate so the caller can show a friendly
    "wrong passphrase" line and re-prompt.

    On successful return, the per-process passphrase cache is cleared:
    every distinct salt's derived key now lives in `_PROCESS_KEYS`,
    which is sufficient for any further decrypts in this process.
    Keeping the passphrase string around longer is a needless exposure
    surface (Rich tracebacks, swap, core dumps). CLI commands that
    need the raw passphrase (`rotate-passphrase`, `recover`) re-prompt
    explicitly anyway.
    """
    from unread.db.repo import read_data_db_secrets_sync
    from unread.security.crypto import (
        PassphraseError,
        decrypt,
        decrypt_with_key,
        is_encrypted,
        load_cached_key,
        lookup_key_for_salt,
        parse_envelope,
        remember_key_for_salt,
    )

    rows = read_data_db_secrets_sync(db_path)
    if not rows:
        return {}

    install_salt = _read_install_salt(db_path)
    out: dict[str, str] = {}
    passphrase: str | None = None

    # Bring the cross-invocation cache into the in-process map up front
    # so every per-row lookup below sees it. Without this the disk
    # cache populated by `upgrade` / `unlock` is invisible to a fresh
    # process, and we'd prompt for the passphrase even though a valid
    # key is sitting one open() away. The disk cache is keyed by salt
    # — only useful when the row's salt matches.
    disk_cached = load_cached_key()
    if disk_cached is not None:
        disk_salt, disk_key = disk_cached
        remember_key_for_salt(disk_salt, disk_key)

    for key, value in rows.items():
        if not value:
            continue
        if not is_encrypted(value):
            # Plaintext rows during migration / downgrade — pass through.
            out[key] = value
            continue
        env = parse_envelope(value)
        # `slot_name=key` is the AAD binding for v2 envelopes. Passing
        # it for v1 reads is harmless (decrypt ignores it when the
        # prefix is `$u1$`) and means we don't need a per-row branch.
        cached_key = lookup_key_for_salt(env.salt)
        if cached_key is not None:
            try:
                out[key] = decrypt_with_key(value, cached_key, slot_name=key)
                continue
            except PassphraseError:
                # Unlikely (cached key already validated against this
                # salt), but recoverable: drop and re-derive.
                pass
        # Need the passphrase. Prompt at most once per process; we
        # then derive a key per distinct salt we encounter.
        if passphrase is None:
            passphrase = _ensure_passphrase()
        # Common case: row salt matches the install salt. Derive once
        # and reuse for every subsequent row sharing that salt.
        if install_salt is not None and env.salt == install_salt:
            from unread.security.crypto import derive_key

            install_key = derive_key(passphrase, install_salt)
            remember_key_for_salt(install_salt, install_key)
            out[key] = decrypt_with_key(value, install_key, slot_name=key)
        else:
            out[key] = decrypt(value, passphrase, slot_name=key)

    # Zeroize the per-process passphrase once the keys are cached. A
    # later read that doesn't need the passphrase (cached_key hits) is
    # free; one that does will re-prompt or pull from the disk cache.
    # See docstring for the security rationale.
    global _PROCESS_PASSPHRASE
    if passphrase is not None:
        _PROCESS_PASSPHRASE = None
    return out


def read_secrets(settings) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """Return persisted secrets from the active backend (DB / keychain).

    Returns an empty dict when nothing is persisted anywhere. Caller
    decides which fields to overlay onto in-memory settings — typically
    only the ones that the higher-precedence layers left empty.

    Backend selection is read from ``app_settings::secrets.backend`` at
    every call: this lets ``unread security migrate`` flip the source
    without requiring a process restart for tests, and keeps the
    default behavior (no app_settings row → DB backend) byte-for-byte
    identical to the pre-Phase-2 implementation.
    """
    # Late import: this module is read at `config.load_settings` time,
    # which itself runs at `unread.cli` module-import. Going through
    # `unread.db.repo` keeps the schema-allowlist and read shape in
    # exactly one place.
    from unread.db.repo import read_data_db_secrets_sync
    from unread.secrets_backend import (
        BACKEND_KEYCHAIN,
        BACKEND_PASSPHRASE,
        keychain_read,
        read_active_backend_sync,
    )

    backend = read_active_backend_sync(settings.storage.data_path)
    if backend == BACKEND_KEYCHAIN:
        # Pull each allowlisted slot out of the OS keychain. Missing /
        # never-stored slots come back as empty entries that the
        # caller's fill-only-if-empty overlay treats as "no value".
        primary = {key: val for key in _SECRET_KEYS if (val := keychain_read(key))}
        if primary:
            return primary
        # If the keychain is empty (fresh migration aborted? user
        # cleared their keychain?) fall through to the DB path so a
        # half-broken state still surfaces saved secrets rather than
        # silently demanding re-init.

    if backend == BACKEND_PASSPHRASE:
        # Read encrypted blobs and decrypt with a passphrase-derived
        # key. Errors here propagate (unlike the silent-fallback
        # paths above) because a wrong / missing passphrase isn't
        # something the legacy reader can paper over.
        return _read_db_secrets_passphrase(settings.storage.data_path)

    return read_data_db_secrets_sync(settings.storage.data_path)
