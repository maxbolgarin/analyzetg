"""Report path helpers for YouTube analyses.

Layout: `reports/youtube/<channel-slug>/<video-slug>-<stamp>.md`. Mirrors
the `reports/<chat-slug>/analyze/<preset>-<stamp>.md` shape for Telegram
chats so a user scanning `reports/` sees one folder per source. Slug rules
come from `core.paths.slugify` — single source of truth.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from unread.core.paths import slugify


def _channel_slug(channel_title: str | None, channel_id: str | None) -> str:
    if channel_title and (s := slugify(channel_title)):
        return s
    if channel_id and (s := slugify(channel_id)):
        return s
    return "unknown-channel"


def _video_slug(title: str | None, video_id: str) -> str:
    """Slug + last-6-of-id suffix to disambiguate same-title videos.

    Example: "Never Gonna Give You Up" + dQw4w9WgXcQ → `never-gonna-give-you-up-9wgxcq`.
    """
    base = slugify(title) if title else ""
    suffix = video_id[-6:].lower()
    if base:
        return f"{base[:34]}-{suffix}"
    return f"video-{suffix}"


def youtube_report_path(
    *,
    video_id: str,
    title: str | None,
    channel_title: str | None,
    channel_id: str | None,
    preset: str,
    stamp: datetime | None = None,
) -> Path:
    """Default disk path for a YouTube analysis report."""
    when = stamp or datetime.now()
    ts = when.strftime("%Y-%m-%d_%H%M%S")
    return Path(
        "reports",
        "youtube",
        _channel_slug(channel_title, channel_id),
        f"{_video_slug(title, video_id)}-{preset}-{ts}.md",
    )
