"""Persisted credentials read at settings-load time.

Authoritative storage is the `secrets` table in `~/.unread/storage/data.sqlite`
— populated by `unread tg init` (the interactive wizard) or programmatically
via `Repo.put_secrets`. Persisting these means a user can blow away
`~/.unread/.env` after a successful first-run setup and the CLI keeps
working.

Reader precedence (high → low):
  1. `data.sqlite::secrets`   — written by the current wizard.
  2. `session.sqlite::unread_secrets` — legacy, written by the previous
     release that put secrets alongside the Telethon session. Kept for
     one release so existing installs don't suddenly demand re-init;
     scheduled for removal in the release after this lands.

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

import sqlite3
from pathlib import Path

from unread.db._keys import SECRET_KEYS as _SECRET_KEYS_SET

# `_keys.SECRET_KEYS` is a frozenset (best for membership checks) but
# this module needs an ordered, parameterizable form to splat into a
# `WHERE key IN (?, ?, …)` query. Tuple-ize once at module load.
_SECRET_KEYS: tuple[str, ...] = tuple(sorted(_SECRET_KEYS_SET))

# Telethon historically wrote `<name>` and now writes `<name>.session`.
# We accept either when locating the legacy on-disk file.
_TELETHON_SUFFIX = ".session"


def _resolve_session_db(session_path: Path) -> Path | None:
    """Return the on-disk Telethon session DB file, or None if missing.

    Telethon may store at `<path>` or `<path>.session` depending on
    version; we check both and prefer the exact path the caller
    configured.
    """
    candidates = [Path(session_path)]
    if not str(session_path).endswith(_TELETHON_SUFFIX):
        candidates.append(Path(str(session_path) + _TELETHON_SUFFIX))
    for c in candidates:
        if c.exists():
            return c
    return None


def _read_legacy_session_secrets(session_path: Path) -> dict[str, str]:
    """One-release fallback reader for the previous storage layout.

    The prior release wrote secrets into the Telethon session DB under
    a custom `unread_secrets` table. Returning rows from there lets a
    user upgrade without losing saved creds. New installs never see
    this path because the wizard writes to `data.sqlite::secrets`.
    """
    db = _resolve_session_db(session_path)
    if db is None:
        return {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    except sqlite3.OperationalError:
        return {}
    try:
        placeholders = ",".join("?" * len(_SECRET_KEYS))
        cur = conn.execute(
            f"SELECT key, value FROM unread_secrets WHERE key IN ({placeholders})",
            _SECRET_KEYS,
        )
        return {k: v for k, v in cur.fetchall() if v}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


# Module-level once-flag so the deprecation warning fires at most once
# per process even though `read_secrets` runs every time settings are
# constructed (CLI bootstrap + per-test fixture reset).
_LEGACY_FALLBACK_WARNED = False


def read_secrets(settings) -> dict[str, str]:  # type: ignore[no-untyped-def]
    """Return persisted secrets, looking in the data DB then the legacy session DB.

    Returns an empty dict when nothing is persisted anywhere. Caller
    decides which fields to overlay onto in-memory settings — typically
    only the ones that the higher-precedence layers left empty.

    When the legacy ``session.sqlite::unread_secrets`` table is the only
    source, log a once-per-process deprecation warning so users know to
    re-run ``unread tg init`` (which writes to the new location) before
    the next release removes the fallback.
    """
    # Late import: this module is read at `config.load_settings` time,
    # which itself runs at `unread.cli` module-import. Going through
    # `unread.db.repo` keeps the schema-allowlist and read shape in
    # exactly one place.
    from unread.db.repo import read_data_db_secrets_sync

    primary = read_data_db_secrets_sync(settings.storage.data_path)
    if primary:
        return primary
    legacy = _read_legacy_session_secrets(settings.telegram.session_path)
    if legacy:
        global _LEGACY_FALLBACK_WARNED
        if not _LEGACY_FALLBACK_WARNED:
            _LEGACY_FALLBACK_WARNED = True
            # Use the project's structured logger lazily to avoid a hard
            # dep at import time (this module loads early via
            # `config.load_settings`).
            try:
                from unread.util.logging import get_logger

                get_logger(__name__).warning(
                    "secrets.legacy_fallback",
                    hint=(
                        "Credentials are being read from the legacy "
                        "session.sqlite::unread_secrets table. Re-run "
                        "`unread tg init` to migrate them into "
                        "data.sqlite::secrets — the legacy fallback will "
                        "be removed in the next release."
                    ),
                )
            except Exception:
                # Never let a logging hiccup block startup.
                pass
    return legacy
