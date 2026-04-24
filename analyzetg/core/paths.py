"""Shared path / slug utilities used by analyze / dump / download-media.

Previously duplicated across analyzer/commands.py, export/commands.py,
and media/commands.py. Consolidated here so a future slug rule change
(e.g. adding a new fallback shape) is one-liner instead of three.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

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
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d")


def compute_window(
    since: str | None, until: str | None, last_days: int | None
) -> tuple[datetime | None, datetime | None]:
    if last_days:
        until_dt = datetime.now()
        return until_dt - timedelta(days=last_days), until_dt
    return parse_ymd(since), parse_ymd(until)


def has_explicit_period(
    since_dt: datetime | None,
    until_dt: datetime | None,
    from_msg_id: int | None,
    full_history: bool,
) -> bool:
    return bool(since_dt or until_dt or from_msg_id is not None or full_history)
