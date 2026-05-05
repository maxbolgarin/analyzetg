"""`unread dump <youtube-url>` transcript / audio / video modes."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
import typer

from unread.youtube.dump import cmd_dump_youtube
from unread.youtube.metadata import YoutubeMetadata
from unread.youtube.transcript import TranscriptResult


def _meta(video_id: str = "abcdefghijk") -> YoutubeMetadata:
    return YoutubeMetadata(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        title="Hello World",
        channel_id="UC123",
        channel_title="Examples",
        channel_url="https://youtube.com/c/examples",
        description="A test video.",
        upload_date="20250101",
        duration_sec=120,
        view_count=1234,
        like_count=42,
    )


def _trans_with_cues() -> TranscriptResult:
    return TranscriptResult(
        text="hello world",
        source="captions",
        language="en",
        duration_sec=120,
        cost_usd=0.0,
        timed_cues=[(0, "hello"), (5, "world")],
    )


def _trans_no_cues() -> TranscriptResult:
    return TranscriptResult(
        text="whisper text only",
        source="audio",
        language=None,
        duration_sec=120,
        cost_usd=0.0123,
        timed_cues=None,
    )


async def test_transcript_mode_writes_metadata_and_transcript(tmp_path) -> None:
    meta = _meta("vid-1cues000")
    out = tmp_path / "out"
    with (
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch(
            "unread.youtube.dump.get_transcript",
            new=AsyncMock(return_value=_trans_with_cues()),
        ),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="transcript",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
        )
    assert (out / "metadata.json").exists()
    assert (out / "transcript.md").exists()
    assert (out / "transcript_timed.json").exists()
    md = (out / "transcript.md").read_text(encoding="utf-8")
    assert "Hello World" in md
    assert "[00:00]" in md and "hello" in md
    assert "[00:05]" in md and "world" in md
    meta_data = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert meta_data["video_id"] == meta.video_id
    assert meta_data["title"] == "Hello World"
    assert "subtitles" not in meta_data and "automatic_captions" not in meta_data
    # No audio/video files in transcript mode.
    assert not (out / "audio.mp3").exists()
    assert not list(out.glob("video.*"))


async def test_transcript_mode_no_cues_falls_back_to_plain_text(tmp_path) -> None:
    meta = _meta("vid-2nocues00")
    out = tmp_path / "out"
    with (
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch(
            "unread.youtube.dump.get_transcript",
            new=AsyncMock(return_value=_trans_no_cues()),
        ),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="transcript",
            youtube_source="audio",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
        )
    md = (out / "transcript.md").read_text(encoding="utf-8")
    assert "whisper text only" in md
    assert not (out / "transcript_timed.json").exists()


async def test_audio_mode_calls_download_audio(tmp_path) -> None:
    meta = _meta("vid-3audio000")
    out = tmp_path / "out"

    async def _fake_download_audio(metadata, dest_dir):
        local = dest_dir / f"{metadata.video_id}.mp3"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"ID3fake-mp3")
        return local

    with (
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch("unread.youtube.dump.download_audio", new=_fake_download_audio),
        patch("unread.youtube.dump.require_ffmpeg"),
        patch(
            "unread.youtube.dump.get_transcript",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        ),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="audio",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
        )
    assert (out / "audio.mp3").exists()
    assert (out / "metadata.json").exists()
    assert not (out / "transcript.md").exists()


async def test_video_mode_calls_download_video(tmp_path) -> None:
    meta = _meta("vid-4video000")
    out = tmp_path / "out"

    async def _fake_download_video(metadata, dest_dir):
        local = dest_dir / f"{metadata.video_id}.mp4"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        return local

    with (
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch("unread.youtube.dump.download_video", new=_fake_download_video),
        patch("unread.youtube.dump.require_ffmpeg"),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="video",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
        )
    assert (out / "video.mp4").exists()
    assert (out / "metadata.json").exists()


async def test_audio_mode_preflight_requires_ffmpeg(tmp_path) -> None:
    meta = _meta("vid-5ffaudio0")
    out = tmp_path / "out"

    def _missing(_reason: str):
        import typer

        raise typer.Exit(1)

    with (
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch(
            "unread.youtube.dump.download_audio",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        ),
        patch("unread.youtube.dump.require_ffmpeg", side_effect=_missing),
        pytest.raises((SystemExit, typer.Exit)),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="audio",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
        )


async def test_video_mode_preflight_requires_ffmpeg(tmp_path) -> None:
    meta = _meta("vid-6ffvideo0")
    out = tmp_path / "out"

    def _missing(_reason: str):
        import typer

        raise typer.Exit(1)

    with (
        patch("unread.youtube.dump.fetch_metadata", new=AsyncMock(return_value=meta)),
        patch(
            "unread.youtube.dump.download_video",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        ),
        patch("unread.youtube.dump.require_ffmpeg", side_effect=_missing),
        pytest.raises((SystemExit, typer.Exit)),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="video",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
        )


async def test_transcript_mode_uses_repo_cache(tmp_path) -> None:
    """A populated `youtube_videos` row must skip get_transcript entirely."""
    out = tmp_path / "out"
    meta = _meta("vid-7cache000")

    from unread.config import get_settings
    from unread.db.repo import open_repo

    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
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
            language="en",
            transcript="cached transcript text",
            transcript_source="captions",
            transcript_model=None,
            transcript_cost_usd=0.0,
            transcript_timed=[(0, "cached"), (3, "transcript text")],
        )

    with (
        patch(
            "unread.youtube.dump.get_transcript",
            new=AsyncMock(side_effect=AssertionError("cache must hit")),
        ),
        patch(
            "unread.youtube.dump.fetch_metadata",
            new=AsyncMock(side_effect=AssertionError("cache must hit")),
        ),
    ):
        await cmd_dump_youtube(
            url=meta.url,
            mode="transcript",
            youtube_source="auto",
            output=out,
            console_out=False,
            language="en",
            report_language="en",
            source_language="",
            yes=True,
        )

    md = (out / "transcript.md").read_text(encoding="utf-8")
    assert "cached" in md and "transcript text" in md
    assert (out / "transcript_timed.json").exists()
