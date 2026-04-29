"""yt-dlp failures surface as `YoutubeFetchError`, not raw tracebacks.

Covers the two extraction paths:
  - `metadata._extract_sync` → audio download metadata
  - `transcript._download_audio._run` → mp3 download

In both, `yt_dlp.utils.DownloadError` is the common failure mode
(deleted / private / region-locked video, network drop, format-change
drift). The wrappers turn it into our typed `YoutubeFetchError` which
the command boundary maps to a friendly banner.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from unread.youtube.transcript import YoutubeFetchError


def _make_yt_dlp_module_with_download_failure(err_msg: str) -> MagicMock:
    """Build a fake `yt_dlp` module whose YoutubeDL.download() raises DownloadError."""

    class _DownloadError(Exception):
        pass

    fake_ydl = MagicMock()
    fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
    fake_ydl.__exit__ = MagicMock(return_value=False)
    fake_ydl.download = MagicMock(side_effect=_DownloadError(err_msg))
    fake_ydl.extract_info = MagicMock(side_effect=_DownloadError(err_msg))

    fake_module = MagicMock()
    fake_module.YoutubeDL = MagicMock(return_value=fake_ydl)
    fake_module.utils = MagicMock()
    fake_module.utils.DownloadError = _DownloadError
    return fake_module


def test_metadata_fetch_wraps_download_error() -> None:
    """`_extract_sync` raising DownloadError should surface as YoutubeFetchError."""
    fake_module = _make_yt_dlp_module_with_download_failure("Video is unavailable")
    with patch("unread.youtube.metadata._import_ytdlp", return_value=fake_module):
        from unread.youtube.metadata import _extract_sync

        with pytest.raises(YoutubeFetchError, match="Video is unavailable"):
            _extract_sync("https://youtu.be/whatever")


@pytest.mark.asyncio
async def test_audio_download_wraps_download_error(tmp_path: Path) -> None:
    """`_download_audio` raising DownloadError should surface as YoutubeFetchError."""
    fake_module = _make_yt_dlp_module_with_download_failure("Sign in to confirm your age")
    with patch("unread.youtube.transcript._import_ytdlp", return_value=fake_module):
        from unread.youtube.metadata import YoutubeMetadata
        from unread.youtube.transcript import _download_audio

        meta = YoutubeMetadata(video_id="abc123", url="https://youtu.be/abc123")
        with pytest.raises(YoutubeFetchError):
            await _download_audio(meta, tmp_path)


def test_youtube_fetch_error_is_runtime_error_subclass() -> None:
    """Existing `except RuntimeError` paths should still catch us."""
    assert issubclass(YoutubeFetchError, RuntimeError)
