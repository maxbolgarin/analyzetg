"""yt-dlp download helpers shared between transcript Whisper fallback
and `unread dump <youtube-url>`.

Why this lives next to `transcript.py`: the audio downloader was originally
a private helper inside `transcript.py` because Whisper was its only
caller. `unread dump --mode=audio|video` adds two more callers, so the
helper moves here and keeps `transcript.py` focused on captions /
Whisper. ``transcript._download_audio`` re-exports :func:`download_audio`
for backwards compatibility — old call sites keep working.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from unread.util.logging import get_logger
from unread.youtube.metadata import YoutubeMetadata, _import_ytdlp

log = get_logger(__name__)


class YoutubeDownloadError(RuntimeError):
    """yt-dlp couldn't download the requested artifact."""


async def download_audio(metadata: YoutubeMetadata, dest_dir: Path) -> Path:
    """Download bestaudio as mp3 into ``<dest_dir>/<video_id>.mp3``.

    Requires ffmpeg (yt-dlp's FFmpegExtractAudio postprocessor). Callers
    must run :func:`unread.util.preflight.require_ffmpeg` upfront.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(dest_dir / "%(id)s.%(ext)s")

    def _run() -> Path:
        yt_dlp = _import_ytdlp()
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestaudio/best",
            "outtmpl": out_template,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "64",
                }
            ],
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([metadata.url])
        except yt_dlp.utils.DownloadError as e:
            raise YoutubeDownloadError(str(e)) from e
        for f in dest_dir.iterdir():
            if f.suffix == ".mp3":
                return f
        raise YoutubeDownloadError(f"yt-dlp produced no mp3 in {dest_dir}")

    return await asyncio.to_thread(_run)


async def download_video(metadata: YoutubeMetadata, dest_dir: Path) -> Path:
    """Download bestvideo+bestaudio as mp4 into ``<dest_dir>/<video_id>.mp4``.

    yt-dlp picks the best video + audio streams and merges them.
    Merging requires ffmpeg. yt-dlp may fall back to mkv on rare
    streams that can't be muxed cleanly into mp4 — we accept either
    and log if mkv was produced.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    out_template = str(dest_dir / "%(id)s.%(ext)s")

    def _run() -> Path:
        yt_dlp = _import_ytdlp()
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": out_template,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([metadata.url])
        except yt_dlp.utils.DownloadError as e:
            raise YoutubeDownloadError(str(e)) from e
        for ext in (".mp4", ".mkv", ".webm"):
            for f in dest_dir.iterdir():
                if f.suffix == ext:
                    if ext != ".mp4":
                        log.info(
                            "youtube.dump.video_fallback_format",
                            video_id=metadata.video_id,
                            format=ext,
                        )
                    return f
        raise YoutubeDownloadError(f"yt-dlp produced no video file in {dest_dir}")

    return await asyncio.to_thread(_run)
