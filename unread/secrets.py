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
    if not env_value:
        # Pre-prod blocker: `_load_dotenv` no longer pollutes
        # `os.environ`, so a passphrase set in `~/.unread/.env` would
        # otherwise be invisible here. Consult the cached overlay
        # explicitly so scripted / cron usage with a `.env`-supplied
        # passphrase keeps working.
        from unread.config import dotenv_value

        env_value = (dotenv_value("UNREAD_PASSPHRASE") or "").strip()
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
        envelope_version,
        is_encrypted,
        load_cached_key,
        lookup_key_for_salt,
        migrate_to_v3_with_key,
        parse_envelope,
        remember_key_for_salt,
    )

    rows = read_data_db_secrets_sync(db_path)
    if not rows:
        return {}

    install_salt = _read_install_salt(db_path)
    out: dict[str, str] = {}
    passphrase: str | None = None
    # (slot_name, new_v3_blob) pairs collected when we successfully
    # decrypt a pre-v3 row. Re-encryption reuses the same key/salt so
    # we can batch the rewrite at the end of the read.
    pending_v3_rewrites: dict[str, str] = {}

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
        # Anything below v3 needs a rewrite. v1 has no slot binding;
        # v2 binds slot but not framing — both are caught here and
        # upgraded once the decrypt succeeds.
        needs_v3_upgrade = envelope_version(value) < 3
        # `slot_name=key` is the AAD binding for v2 / v3 envelopes.
        # Passing it for v1 reads is harmless (decrypt ignores it when
        # the prefix is `$u1$`) and means we don't need a per-row branch.
        cached_key = lookup_key_for_salt(env.salt)
        used_key: bytes | None = None
        if cached_key is not None:
            try:
                out[key] = decrypt_with_key(value, cached_key, slot_name=key)
                used_key = cached_key
            except PassphraseError:
                # Unlikely (cached key already validated against this
                # salt), but recoverable: drop and re-derive.
                pass
        if used_key is None:
            # Need the passphrase. Prompt at most once per process; we
            # then derive a key per distinct salt we encounter.
            if passphrase is None:
                passphrase = _ensure_passphrase()
            # Common case: row salt matches the install salt. Derive
            # once and reuse for every subsequent row sharing that salt.
            if install_salt is not None and env.salt == install_salt:
                from unread.security.crypto import derive_key

                install_key = derive_key(passphrase, install_salt)
                remember_key_for_salt(install_salt, install_key)
                out[key] = decrypt_with_key(value, install_key, slot_name=key)
                used_key = install_key
            else:
                out[key] = decrypt(value, passphrase, slot_name=key)
                # Cache for the rewrite below — per-row salts are rare
                # but we still want one Scrypt per distinct salt.
                from unread.security.crypto import derive_key

                used_key = derive_key(passphrase, env.salt)
                remember_key_for_salt(env.salt, used_key)
        # Auto-migration: any successfully-decrypted v1 / v2 row gets
        # rewritten as v3 (slot + framing-bound AAD) at the end of the
        # read. Idempotent — once every row is v3 this branch never
        # runs again for the install. Failure to derive the new blob
        # is logged but doesn't fail the read; the in-memory plaintext
        # the caller asked for is unchanged.
        if needs_v3_upgrade and used_key is not None:
            try:
                pending_v3_rewrites[key] = migrate_to_v3_with_key(value, used_key, slot_name=key)
            except (PassphraseError, ValueError) as e:  # pragma: no cover - defensive
                from unread.util.logging import get_logger

                get_logger(__name__).warning("crypto.aead_v3_migrate_skip", slot=key, err=type(e).__name__)

    # Zeroize the per-process passphrase once the keys are cached. A
    # later read that doesn't need the passphrase (cached_key hits) is
    # free; one that does will re-prompt or pull from the disk cache.
    # See docstring for the security rationale.
    global _PROCESS_PASSPHRASE
    if passphrase is not None:
        _PROCESS_PASSPHRASE = None

    if pending_v3_rewrites:
        _persist_v3_rewrites_sync(db_path, pending_v3_rewrites)

    return out


def _persist_v3_rewrites_sync(db_path: Path, rewrites: dict[str, str]) -> None:
    """Write a batch of v1/v2→v3 envelope upgrades back to ``data.sqlite::secrets``.

    Runs at the end of a successful passphrase decrypt pass, never
    blocking the caller. A write error here is logged and swallowed —
    the in-memory plaintext the caller asked for is unaffected, and
    the next read attempt will pick up where this one left off.

    Uses sync sqlite3 because the read path that calls us is sync (it
    runs at `config.load_settings` time, which itself runs at
    `unread.cli` module-import). Single transaction so we never leave
    a half-migrated install behind.
    """
    from datetime import UTC, datetime

    from unread.db._keys import SECRET_KEYS as _ALLOWLIST

    # Allowlist guard mirrors the schema-side enforcement in
    # `Repo.put_secrets`. Defensive: a stray key here would be a real
    # bug upstream, but a row write that violates the allowlist must
    # never land on disk.
    for slot in rewrites:
        if slot not in _ALLOWLIST:
            from unread.util.logging import get_logger

            get_logger(__name__).warning("crypto.aead_v3_migrate_unknown_slot", slot=slot)
            return

    now_iso = datetime.now(UTC).isoformat()
    rows = [(value, now_iso, slot) for slot, value in rewrites.items()]
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
    except sqlite3.Error as e:
        from unread.util.logging import get_logger

        get_logger(__name__).warning("crypto.aead_v3_migrate_db_open", err=type(e).__name__)
        return
    try:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.executemany(
                "UPDATE secrets SET value=?, updated_at=? WHERE key=?",
                rows,
            )
            conn.commit()
        except sqlite3.Error as e:
            with __import__("contextlib").suppress(sqlite3.Error):
                conn.rollback()
            from unread.util.logging import get_logger

            get_logger(__name__).warning(
                "crypto.aead_v3_migrate_db_write", err=type(e).__name__, count=len(rows)
            )
            return
    finally:
        conn.close()

    from unread.util.logging import get_logger

    get_logger(__name__).info("crypto.aead_v3_migrated", count=len(rows))


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


async def write_secrets(settings, values: dict[str, str]) -> None:  # type: ignore[no-untyped-def]
    """Persist ``values`` through the currently active backend.

    The user-facing write counterpart to :func:`read_secrets` — every
    code path that takes a freshly-typed credential and saves it
    (``init`` wizard, ``settings`` menu, ``tg login`` re-auth) goes
    through here so the user's "keystore by default" choice is durable.
    Without this routing, post-init writes would land in
    ``data.sqlite::secrets`` regardless of the active backend, leaving
    a partial-plaintext install behind a "keystore" status flag.

    Migration plumbing (``security migrate`` / ``upgrade`` /
    ``downgrade``) deliberately bypasses this and writes directly via
    :meth:`Repo.put_secrets`: those paths are moving values BETWEEN
    backends and routing-through-active would feed them back into the
    backend they're trying to leave.

    Backend semantics:
      - ``db``      → plaintext row in ``secrets`` (current behaviour).
      - ``keychain``→ each value lands under the install-namespaced
                      keychain service. Empty values are skipped to
                      mirror :meth:`Repo.put_secrets`.
      - ``passphrase`` → encrypted with the install key, then written
                      to the ``secrets`` table as ciphertext.

    Allowlist enforcement happens here (and again at the schema layer
    when we route to DB), so a typo in `key` raises ``ValueError``
    regardless of which backend ends up servicing the write.
    """
    if not values:
        return
    from unread.db._keys import SECRET_KEYS as _ALLOWLIST

    for key in values:
        if key not in _ALLOWLIST:
            raise ValueError(f"unknown secret key: {key!r}; allowed: {sorted(_ALLOWLIST)}")

    from unread.secrets_backend import (
        BACKEND_KEYCHAIN,
        BACKEND_PASSPHRASE,
        keychain_write,
        read_active_backend_sync,
    )

    backend = read_active_backend_sync(settings.storage.data_path)

    if backend == BACKEND_KEYCHAIN:
        failures: list[str] = []
        for key, value in values.items():
            if not value:
                # Match Repo.put_secrets: empty values are no-ops
                # rather than wiping the existing entry.
                continue
            if not keychain_write(key, value):
                failures.append(key)
        if failures:
            raise RuntimeError(
                "keychain write failed for: "
                + ", ".join(sorted(failures))
                + " — run `unread security status` to inspect the active backend"
            )
        return

    if backend == BACKEND_PASSPHRASE:
        # Encrypt with the install-salt-derived key. Same envelope
        # shape as `write_session_string_async`, just iterated over
        # multiple slots.
        from unread.security.crypto import encrypt_with_key, lookup_key_for_salt
        from unread.security.passphrase import ensure_install_key, read_install_salt

        salt = read_install_salt(settings.storage.data_path)
        if salt is None:
            raise RuntimeError("install salt missing — run `unread security upgrade --passphrase`")
        key_bytes = lookup_key_for_salt(salt) or ensure_install_key(settings.storage.data_path)
        encrypted: dict[str, str] = {}
        for slot, value in values.items():
            if not value:
                continue
            encrypted[slot] = encrypt_with_key(value, key_bytes, salt=salt, slot_name=slot)
        if not encrypted:
            return
        from unread.db.repo import open_repo

        async with open_repo(settings.storage.data_path) as repo:
            await repo.put_secrets(encrypted)
        return

    # backend == BACKEND_DB (default).
    from unread.db.repo import open_repo

    async with open_repo(settings.storage.data_path) as repo:
        await repo.put_secrets(values)


async def delete_secret(settings, key: str) -> bool:  # type: ignore[no-untyped-def]
    """Remove ``key`` from the currently active backend.

    Returns True iff a row / entry actually existed. Allowlist-checked
    against `SECRET_KEYS` for the same reason as :func:`write_secrets`.
    Migration code paths (which know which backend they're targeting)
    keep using :meth:`Repo.delete_secret` directly.
    """
    from unread.db._keys import SECRET_KEYS as _ALLOWLIST

    if key not in _ALLOWLIST:
        raise ValueError(f"unknown secret key: {key!r}")

    from unread.secrets_backend import (
        BACKEND_KEYCHAIN,
        keychain_delete,
        read_active_backend_sync,
    )

    backend = read_active_backend_sync(settings.storage.data_path)
    if backend == BACKEND_KEYCHAIN:
        return keychain_delete(key)

    # `db` and `passphrase` both keep rows in `secrets`; delete is
    # backend-independent (the row is going away regardless of
    # whether its `value` was plaintext or ciphertext).
    from unread.db.repo import open_repo

    async with open_repo(settings.storage.data_path) as repo:
        return await repo.delete_secret(key)
