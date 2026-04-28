"""Shared path / slug utilities used by analyze / dump / download-media.

Previously duplicated across analyzer/commands.py, export/commands.py,
and media/commands.py. Consolidated here so a future slug rule change
(e.g. adding a new fallback shape) is one-liner instead of three.

Also owns the `unread_home()` family of helpers — the single source of
truth for `~/.unread/...` path defaults. Every storage / reports / config
/ session path in the codebase derives from `unread_home()`. Override
the root with `UNREAD_HOME=/abs/path` for tests, dev installs, or
multi-profile setups.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path


def install_pointer_path() -> Path:
    """Canonical location of the `install.toml` pointer file.

    Always at `~/.unread/install.toml`, even when the actual data lives
    elsewhere (custom path / current folder picked during the wizard).
    The pointer has to be findable BEFORE `unread_home()` resolves —
    that's the whole reason it can't itself live under `unread_home()`.
    """
    return Path.home() / ".unread" / "install.toml"


def unread_home() -> Path:
    """Resolve the per-user `~/.unread/` install directory.

    Resolution order (high → low):
      1. `UNREAD_HOME` env var (tests, dev, multi-profile installs).
      2. `~/.unread/install.toml` pointer file with a `home = "..."`
         entry — written by `unread tg init` when the user picks
         "current folder" or "custom path". Empty / absent value falls
         through to the default.
      3. Default: `~/.unread/`.

    Defensive about pointer-file errors: a missing/corrupt TOML simply
    falls through to the default. We never want a bad pointer to
    surface as an exception — the user can always recover by deleting
    the file and re-running `unread tg init`.
    """
    override = os.environ.get("UNREAD_HOME")
    if override:
        return Path(override).expanduser()
    pointer = install_pointer_path()
    if pointer.is_file():
        try:
            import tomllib

            data = tomllib.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # `tomllib.TOMLDecodeError` is a subclass of `ValueError`;
            # broader catch keeps this defensive against any read snag.
            data = {}
        home = data.get("home")
        if home:
            return Path(home).expanduser()
    return Path.home() / ".unread"


def write_install_pointer(home: Path | None) -> None:
    """Persist the install-folder choice to `~/.unread/install.toml`.

    Pass `None` (or the default `~/.unread/` itself) to record the
    "default" choice as `home = ""` — its presence is the canonical
    "setup has been done" marker that keeps subsequent `unread tg init`
    runs from re-prompting for folder selection.
    """
    pointer = install_pointer_path()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    # Default choice → empty value, so a future move of the pointer
    # subtree never accidentally redirects somewhere weird.
    target = "" if home is None else str(Path(home).expanduser().resolve())
    body = f'# Written by `unread tg init`. Delete to re-pick the install folder.\nhome = "{target}"\n'
    pointer.write_text(body, encoding="utf-8")


def storage_dir() -> Path:
    return unread_home() / "storage"


def reports_dir() -> Path:
    return unread_home() / "reports"


def default_session_path() -> Path:
    return storage_dir() / "session.sqlite"


def default_data_path() -> Path:
    return storage_dir() / "data.sqlite"


def default_media_dir() -> Path:
    return storage_dir() / "media"


def default_backups_dir() -> Path:
    return storage_dir() / "backups"


def default_config_path() -> Path:
    return unread_home() / "config.toml"


def default_env_path() -> Path:
    return unread_home() / ".env"


def ensure_unread_home() -> Path:
    """Create `~/.unread/` (mode 0700) if missing. Idempotent.

    Mode 0700 matches the README's old `chmod 700 storage` advice — the
    SQLite DBs and the `.env` file aren't encrypted, so file-system
    permissions are the only confidentiality boundary.
    """
    import contextlib

    p = unread_home()
    p.mkdir(parents=True, exist_ok=True)
    # Best-effort: Windows / network mounts may not support chmod.
    with contextlib.suppress(OSError):
        p.chmod(0o700)
    return p


# Permissive regex: keep Unicode letters/digits/underscore/hyphen, collapse
# everything else. Empty → empty string (callers supply a fallback).
_SLUG_RE = re.compile(r"[^\w\-]+", re.UNICODE)


def slugify(text: str) -> str:
    """Lowercase, punctuation-stripped, 40-char-capped directory slug.

    Preserves Unicode letters (Cyrillic, CJK, Arabic, …). Empty or
    all-punctuation input returns `""` — callers must provide a
    fallback (see `chat_slug` / `topic_slug`).
    """
    slug = _SLUG_RE.sub("-", text).strip("-").lower()
    return slug[:40]


def chat_slug(title: str | None, chat_id: int) -> str:
    """Directory-safe identifier for a chat.

    Falls back to `chat-<abs chat_id>` when the title is empty or
    slugs down to nothing (e.g. emoji-only Telegram titles).
    """
    if title and (s := slugify(title)):
        return s
    return f"chat-{abs(chat_id)}"


def topic_slug(title: str | None, thread_id: int) -> str:
    """Directory-safe identifier for a forum topic.

    Falls back to `topic-<id>` when the title isn't known at write
    time — keeps the directory structure deterministic even when the
    caller only has the numeric id.
    """
    if title and (s := slugify(title)):
        return s
    return f"topic-{thread_id}"


def unique_path(base: Path) -> Path:
    """Return `base`, or the first numbered sibling that doesn't exist yet.

    Appends `-2`, `-3`, ... until we find a free slot. Caps at 100 to
    surface pathological cases (infinite loops in a calling script).
    """
    if not base.exists():
        return base
    stem = base.stem
    suffix = base.suffix
    parent = base.parent
    for i in range(2, 100):
        cand = parent / f"{stem}-{i}{suffix}"
        if not cand.exists():
            return cand
    raise RuntimeError(f"100 collisions at {base} — check for a runaway loop")


def derive_internal_id(chat_id: int) -> int | None:
    """Strip Telethon's `-100` channel/supergroup prefix.

    Returns None for regular users / small groups where the id isn't
    suitable for a t.me/c/ link.
    """
    if chat_id >= 0:
        return None
    abs_id = abs(chat_id)
    if abs_id > 1_000_000_000_000:
        return abs_id - 1_000_000_000_000
    return None


def parse_ymd(s: str | None) -> datetime | None:
    """Parse a YYYY-MM-DD string as UTC midnight.

    Returning UTC-aware datetimes keeps comparisons consistent with
    stored message timestamps, which are ISO-formatted UTC strings.
    A naive datetime here would sort wrong against those stored values.
    """
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)


def compute_window(
    since: str | None,
    until: str | None,
    last_days: int | None,
    last_hours: int | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Return a UTC-aware (since, until) window.

    `--last-hours N` → (now-UTC - N hours, now-UTC); `--last-days N` →
    (now-UTC - N days, now-UTC); `--since/--until` are parsed as
    UTC-midnight by `parse_ymd`. Telethon's `offset_date` and SQLite
    `messages.date` column are both UTC, so staying UTC end-to-end
    avoids off-by-timezone window edges.

    Precedence within this helper: `last_hours` > `last_days` >
    `since/until`. The hour-granular flag is the more specific one,
    so when both are passed it wins. Caller-side flag mutex still
    holds — this helper is the *resolver*, not the validator.
    """
    if last_hours:
        until_dt = datetime.now(UTC)
        return until_dt - timedelta(hours=last_hours), until_dt
    if last_days:
        until_dt = datetime.now(UTC)
        return until_dt - timedelta(days=last_days), until_dt
    return parse_ymd(since), parse_ymd(until)


def has_explicit_period(
    since_dt: datetime | None,
    until_dt: datetime | None,
    from_msg_id: int | None,
    full_history: bool,
) -> bool:
    return bool(since_dt or until_dt or from_msg_id is not None or full_history)
