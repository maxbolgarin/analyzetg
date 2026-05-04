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

import hashlib
import sqlite3
from pathlib import Path

from unread.db._keys import SECRET_KEYS as _SECRET_KEYS
from unread.util.logging import get_logger

log = get_logger(__name__)

# Identifiers used when calling into `keyring`. Two installs on the
# same OS user otherwise share a flat `unread` namespace and silently
# clobber each other's keychain entries — and any other Python process
# could `keyring.get_password("unread", ...)` to fish them out. The
# service name is now ALWAYS namespaced as `unread:<install_id>` where
# `install_id` is the first 12 hex chars of `sha256(install_home)`.
# Existing legacy entries under the bare `"unread"` service are
# migrated forward on first read (see `keychain_read`).
_KEYCHAIN_BASE = "unread"
_LEGACY_KEYCHAIN_SERVICE = _KEYCHAIN_BASE
_INSTALL_ID_LEN = 12

# Cached resolution of the per-install service name. Keyed on the
# resolved install-home path so a mid-process `UNREAD_HOME` flip
# (tests, dev shell switching) is honored without any explicit cache
# reset. The path resolve + sha256 take ~100 µs anyway — caching is a
# politeness, not a hot-path optimization.
_KEYCHAIN_SERVICE_CACHE: dict[str, str] = {}


def _compute_install_id(home_str: str) -> str:
    return hashlib.sha256(home_str.encode("utf-8")).hexdigest()[:_INSTALL_ID_LEN]


def keychain_service() -> str:
    """Return the namespaced keychain service name for this install.

    Format: ``unread:<install_id>`` where ``install_id`` is the first
    12 hex chars of ``sha256(install_home)``. The path → service-name
    mapping is cached per-process; flipping ``UNREAD_HOME`` to a
    different install transparently picks up a different name.

    Replaces the historical bare `"unread"` constant. Reads of slots
    not present under the namespaced service fall through to the legacy
    `"unread"` name once and copy the value forward — see
    :func:`keychain_read`.
    """
    try:
        from unread.core.paths import unread_home

        home = str(unread_home().resolve())
    except Exception:
        # Defensive: keep a stable shape so the migration shim always
        # has a target to compare the legacy service name against.
        home = "default"
    cached = _KEYCHAIN_SERVICE_CACHE.get(home)
    if cached is not None:
        return cached
    name = f"{_KEYCHAIN_BASE}:{_compute_install_id(home)}"
    _KEYCHAIN_SERVICE_CACHE[home] = name
    return name


def _reset_keychain_service_cache() -> None:
    """Clear the per-install cache. For tests and dev shell switching."""
    _KEYCHAIN_SERVICE_CACHE.clear()


def __getattr__(name: str) -> str:
    """Lazy compatibility shim for `from unread.secrets_backend import KEYCHAIN_SERVICE`.

    The constant was the public name in the prior release. Existing
    callers (security commands, killme, and tests) keep importing it;
    we resolve to the live `keychain_service()` value here so they
    pick up the per-install namespacing automatically. New code should
    call `keychain_service()` directly so the cache reset works for
    them too.
    """
    if name == "KEYCHAIN_SERVICE":
        return keychain_service()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
        log.debug("secrets_backend.keychain_probe_failed", err_type=type(e).__name__)
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


def _migrate_legacy_slot(key: str) -> str | None:
    """One-time forward-port from the bare `"unread"` service to namespaced.

    Older installs wrote every credential under the flat `"unread"`
    keychain service. The new release namespaces by install path.
    On the first read after upgrade, look up the legacy entry, copy it
    into the namespaced slot, and delete the legacy row. Subsequent
    reads short-circuit to the namespaced slot. Failures here are
    swallowed — the worst case is the user re-enters the credential.
    """
    service = keychain_service()
    if service == _LEGACY_KEYCHAIN_SERVICE:
        # Defensive: if the namespaced name happens to collide with the
        # legacy one, there's nothing to migrate.
        return None
    try:
        import keyring
        from keyring.errors import PasswordDeleteError

        legacy_value = keyring.get_password(_LEGACY_KEYCHAIN_SERVICE, key)
        if legacy_value is None:
            return None
        try:
            keyring.set_password(service, key, legacy_value)
        except Exception as e:
            log.debug("secrets_backend.keychain_legacy_copy_failed", key=key, err_type=type(e).__name__)
            return legacy_value
        try:
            keyring.delete_password(_LEGACY_KEYCHAIN_SERVICE, key)
        except PasswordDeleteError:
            pass
        except Exception as e:
            log.debug("secrets_backend.keychain_legacy_delete_failed", key=key, err_type=type(e).__name__)
        log.debug(
            "secrets_backend.keychain_legacy_migrated",
            key=key,
            legacy_service=_LEGACY_KEYCHAIN_SERVICE,
            new_service=service,
        )
        return legacy_value
    except Exception as e:
        log.debug("secrets_backend.keychain_legacy_lookup_failed", key=key, err_type=type(e).__name__)
        return None


def keychain_read(key: str) -> str | None:
    """Return the value stored under ``key`` in the OS keychain, or None.

    Only allowlisted keys are accepted — silently returning None for
    anything else stops a typo in a Python repl from spelunking
    arbitrary credentials out of the user's keychain.

    On a miss against the namespaced service, falls back ONCE to the
    legacy bare `"unread"` service (and forward-ports the value if
    found) so installs that pre-date the per-install namespacing
    upgrade transparently. See :func:`_migrate_legacy_slot`.
    """
    if key not in _SECRET_KEYS:
        return None
    try:
        import keyring

        value = keyring.get_password(keychain_service(), key)
    except Exception as e:
        log.warning("secrets_backend.keychain_read_failed", key=key, err_type=type(e).__name__)
        return None
    if value is not None:
        return value
    # Migration shim — runs at most once per slot per install.
    return _migrate_legacy_slot(key)


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

        keyring.set_password(keychain_service(), key, value)
        return True
    except Exception as e:
        log.warning("secrets_backend.keychain_write_failed", key=key, err_type=type(e).__name__)
        return False


def keychain_delete(key: str) -> bool:
    """Remove ``key`` from the keychain. False if it wasn't there or the call failed."""
    if key not in _SECRET_KEYS:
        return False
    try:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(keychain_service(), key)
        except PasswordDeleteError:
            return False
        return True
    except Exception as e:
        log.warning("secrets_backend.keychain_delete_failed", key=key, err_type=type(e).__name__)
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
