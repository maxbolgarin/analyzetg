"""`unread dump <youtube-url>` — transcript / audio / video artifacts.

Mirrors :mod:`unread.youtube.commands` shape (fetch metadata, optional
transcript / download) but skips the LLM analysis. Three modes:

- ``transcript`` — ``metadata.json`` + ``transcript.md`` (plain text,
  no per-cue timestamps). Honors the existing ``--youtube-source`` flag.
  Per-cue timing is still cached in the DB for the analyze / ask paths,
  it just isn't emitted into the dump directory.
- ``audio`` — ``metadata.json`` + ``audio.mp3`` (yt-dlp + ffmpeg).
- ``video`` — ``metadata.json`` + ``video.mp4`` / ``.mkv`` /
  ``.webm`` (yt-dlp + ffmpeg merging).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Literal

import typer
from rich.console import Console

from unread.config import get_settings
from unread.db.repo import open_repo
from unread.i18n import t as _t
from unread.i18n import tf as _tf
from unread.util.logging import get_logger
from unread.util.preflight import require_ffmpeg
from unread.youtube.commands import _meta_header, _restore_metadata_from_row
from unread.youtube.download import (
    YoutubeDownloadError,
    download_audio,
    download_video,
)
from unread.youtube.metadata import YoutubeMetadata, fetch_metadata
from unread.youtube.paths import youtube_dump_dir
from unread.youtube.transcript import (
    NoTranscriptAvailable,
    TranscriptResult,
    TranscriptSource,
    YoutubeFetchError,
    get_transcript,
)
from unread.youtube.urls import extract_video_id

console = Console()
log = get_logger(__name__)

YoutubeDumpMode = Literal["transcript", "audio", "video"]


def _metadata_dict(meta: YoutubeMetadata) -> dict:
    """JSON-serializable view of the metadata.

    Drops the bulky ``subtitles`` / ``automatic_captions`` blobs — they
    pull thousands of internal yt-dlp URLs that aren't useful for users
    reading a dump report.
    """
    out = asdict(meta)
    out.pop("subtitles", None)
    out.pop("automatic_captions", None)
    return out


def _build_transcript_md(meta: YoutubeMetadata, tres: TranscriptResult) -> str:
    """Markdown body: meta header + plain transcript text (no per-cue timestamps).

    Per-cue timing is preserved in the DB (and used by the analyze / ask
    paths so the LLM can quote ``[HH:MM:SS]`` markers) but the dump
    artifact is plain reading copy.
    """
    header = _meta_header(meta)
    body = (tres.text or "").strip()
    parts = [header, "", "## Transcript", "", body]
    return "\n".join(parts).rstrip() + "\n"


def _resolve_dump_dir(
    output: Path | None,
    meta: YoutubeMetadata,
    mode: YoutubeDumpMode,
) -> Path:
    if output:
        return output
    return youtube_dump_dir(
        video_id=meta.video_id,
        title=meta.title,
        channel_title=meta.channel_title,
        channel_id=meta.channel_id,
        mode=mode,
        stamp=datetime.now(),
    )


def _write_metadata(meta: YoutubeMetadata, dest: Path) -> None:
    dest.write_text(
        json.dumps(_metadata_dict(meta), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


async def _resolve_metadata(repo, video_id: str) -> YoutubeMetadata:
    cached = await repo.get_youtube_video(video_id)
    if cached:
        return _restore_metadata_from_row(cached)
    try:
        return await fetch_metadata(video_id)
    except YoutubeFetchError as e:
        console.print(f"[red]{_t('youtube_fetch_failed').format(err=str(e)[:300])}[/]")
        console.print(f"[grey70]{_t('youtube_fetch_failed_hint')}[/]")
        raise typer.Exit(1) from e


async def _do_transcript_mode(
    *,
    repo,
    meta: YoutubeMetadata,
    youtube_source: TranscriptSource,
    dump_dir: Path,
    settings,
    cached_row: dict | None,
) -> None:
    if cached_row and cached_row.get("transcript"):
        tres = TranscriptResult(
            text=cached_row["transcript"] or "",
            source=cached_row.get("transcript_source") or "captions",  # type: ignore[arg-type]
            language=cached_row.get("language"),
            duration_sec=meta.duration_sec,
            cost_usd=float(cached_row.get("transcript_cost_usd") or 0.0),
            timed_cues=None,
        )
    else:
        try:
            tres = await get_transcript(
                meta,
                source=youtube_source,
                settings=settings,
                repo=repo,
            )
        except NoTranscriptAvailable as e:
            raise typer.BadParameter(str(e)) from e
        except YoutubeFetchError as e:
            console.print(f"[red]{_t('youtube_fetch_failed').format(err=str(e)[:300])}[/]")
            console.print(f"[grey70]{_t('youtube_fetch_failed_hint')}[/]")
            raise typer.Exit(1) from e

        await repo.put_youtube_video(
            video_id=meta.video_id,
            url=meta.url,
            title=meta.title,
            channel_id=meta.channel_id,
            channel_title=meta.channel_title,
            channel_url=meta.channel_url,
            description=meta.description,
            upload_date=meta.upload_date,
            duration_sec=meta.duration_sec,
            view_count=meta.view_count,
            like_count=meta.like_count,
            tags=meta.tags,
            language=tres.language,
            transcript=tres.text,
            transcript_source=tres.source,
            transcript_model=(settings.openai.audio_model_default if tres.source == "audio" else None),
            transcript_cost_usd=tres.cost_usd,
            transcript_timed=tres.timed_cues,
        )

    if not (tres.text or "").strip():
        console.print(f"[red]{_t('cli_error_prefix')}[/] {_t('err_files_empty_transcript')}")
        raise typer.Exit(2)

    _write_metadata(meta, dump_dir / "metadata.json")
    (dump_dir / "transcript.md").write_text(_build_transcript_md(meta, tres), encoding="utf-8")

    console.print(f"[green]{_tf('dump_youtube_transcript_done', path=dump_dir)}[/]")


async def _do_audio_mode(*, meta: YoutubeMetadata, dump_dir: Path) -> None:
    require_ffmpeg("download YouTube audio")
    _write_metadata(meta, dump_dir / "metadata.json")
    try:
        downloaded = await download_audio(meta, dump_dir)
    except YoutubeDownloadError as e:
        console.print(f"[red]{_t('youtube_fetch_failed').format(err=str(e)[:300])}[/]")
        console.print(f"[grey70]{_t('youtube_fetch_failed_hint')}[/]")
        raise typer.Exit(1) from e
    final = dump_dir / "audio.mp3"
    if downloaded != final:
        downloaded.rename(final)
    console.print(f"[green]{_tf('dump_youtube_audio_done', path=dump_dir)}[/]")


async def _do_video_mode(*, meta: YoutubeMetadata, dump_dir: Path) -> None:
    require_ffmpeg("download YouTube video")
    _write_metadata(meta, dump_dir / "metadata.json")
    try:
        downloaded = await download_video(meta, dump_dir)
    except YoutubeDownloadError as e:
        console.print(f"[red]{_t('youtube_fetch_failed').format(err=str(e)[:300])}[/]")
        console.print(f"[grey70]{_t('youtube_fetch_failed_hint')}[/]")
        raise typer.Exit(1) from e
    final = dump_dir / f"video{downloaded.suffix}"
    if downloaded != final:
        downloaded.rename(final)
    console.print(f"[green]{_tf('dump_youtube_video_done', path=dump_dir)}[/]")


async def cmd_dump_youtube(
    *,
    url: str,
    mode: YoutubeDumpMode,
    youtube_source: TranscriptSource,
    output: Path | None,
    console_out: bool,
    language: str,
    report_language: str,
    source_language: str,
    yes: bool,
) -> None:
    """Dump a YouTube video. Mode picks the artifact (transcript / audio / video)."""
    settings = get_settings()
    try:
        video_id = extract_video_id(url)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e

    async with open_repo(settings.storage.data_path) as repo:
        cached_row = await repo.get_youtube_video(video_id)
        if cached_row:
            meta = _restore_metadata_from_row(cached_row)
        else:
            try:
                meta = await fetch_metadata(video_id)
            except YoutubeFetchError as e:
                console.print(f"[red]{_t('youtube_fetch_failed').format(err=str(e)[:300])}[/]")
                console.print(f"[grey70]{_t('youtube_fetch_failed_hint')}[/]")
                raise typer.Exit(1) from e

        dump_dir = _resolve_dump_dir(output, meta, mode)
        dump_dir.mkdir(parents=True, exist_ok=True)

        if mode == "transcript":
            await _do_transcript_mode(
                repo=repo,
                meta=meta,
                youtube_source=youtube_source,
                dump_dir=dump_dir,
                settings=settings,
                cached_row=cached_row,
            )
        elif mode == "audio":
            await _do_audio_mode(meta=meta, dump_dir=dump_dir)
        elif mode == "video":
            await _do_video_mode(meta=meta, dump_dir=dump_dir)
        else:
            raise typer.BadParameter(f"Unknown dump mode {mode!r}")

    if console_out and mode == "transcript":
        console.print((dump_dir / "transcript.md").read_text(encoding="utf-8"))
