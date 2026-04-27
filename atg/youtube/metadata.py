"""YouTube metadata fetch via yt-dlp's Python API.

`yt_dlp` is a soft dependency: import is deferred to call-time so a user
who never analyzes YouTube doesn't need it installed (and `atg --help`
doesn't import it). When missing, raise a `YtdlpMissing` with a clear fix.

yt-dlp's `extract_info` is synchronous; we run it under `asyncio.to_thread`
to keep the rest of the event loop responsive (it can take a few seconds
on cold-cache videos due to YouTube's signature-decryption work).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from atg.util.logging import get_logger
from atg.youtube.urls import video_url

log = get_logger(__name__)


class YtdlpMissing(RuntimeError):
    """Raised when `yt_dlp` isn't installed."""


@dataclass(slots=True)
class YoutubeMetadata:
    video_id: str
    url: str
    title: str | None = None
    channel_id: str | None = None
    channel_title: str | None = None
    channel_url: str | None = None
    description: str | None = None
    upload_date: str | None = None  # YYYYMMDD as yt-dlp returns it
    duration_sec: int | None = None
    view_count: int | None = None
    like_count: int | None = None
    tags: list[str] | None = None
    language: str | None = None
    # Subtitles found on the YouTube page, keyed by language code. The
    # transcript stage uses this to decide whether to grab captions or
    # fall back to audio + Whisper. Each entry holds the list of subtitle
    # URLs / formats yt-dlp surfaced; we don't process them here.
    subtitles: dict[str, list[dict[str, Any]]] | None = None
    automatic_captions: dict[str, list[dict[str, Any]]] | None = None


def _ydl_options() -> dict[str, Any]:
    """Quiet, network-only yt-dlp options for metadata extraction."""
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        # Tell yt-dlp the page contains a video, not a playlist — even
        # when the URL has `&list=`. Pairs with our own URL extraction
        # which strips list/start_radio params before this is called.
        "extract_flat": False,
    }


def _import_ytdlp():
    try:
        import yt_dlp  # type: ignore[import-not-found]
    except ImportError as e:
        raise YtdlpMissing(
            "yt-dlp not installed. Run `uv sync` (or `pip install yt-dlp`) to enable YouTube analysis."
        ) from e
    return yt_dlp


def _extract_sync(url: str) -> dict[str, Any]:
    yt_dlp = _import_ytdlp()
    with yt_dlp.YoutubeDL(_ydl_options()) as ydl:
        return ydl.extract_info(url, download=False) or {}


async def fetch_metadata(video_id: str) -> YoutubeMetadata:
    """Resolve a video's metadata via yt-dlp. Single network call, no download."""
    url = video_url(video_id)
    log.info("youtube.metadata.fetch", video_id=video_id)
    info = await asyncio.to_thread(_extract_sync, url)
    return YoutubeMetadata(
        video_id=video_id,
        url=url,
        title=info.get("title") or None,
        channel_id=info.get("channel_id") or info.get("uploader_id") or None,
        channel_title=info.get("channel") or info.get("uploader") or None,
        channel_url=info.get("channel_url") or info.get("uploader_url") or None,
        description=info.get("description") or None,
        upload_date=info.get("upload_date") or None,
        duration_sec=int(info["duration"]) if info.get("duration") is not None else None,
        view_count=int(info["view_count"]) if info.get("view_count") is not None else None,
        like_count=int(info["like_count"]) if info.get("like_count") is not None else None,
        tags=list(info.get("tags") or []) or None,
        language=info.get("language") or None,
        subtitles=dict(info.get("subtitles") or {}) or None,
        automatic_captions=dict(info.get("automatic_captions") or {}) or None,
    )
