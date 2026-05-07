"""Subtitle language fallback + retry-on-rate-limit + Whisper progress logs.

The user-reported bug: a single 429 on the preferred subtitle language
sent the whole flow to Whisper without trying English next, and the
Whisper path was silent (no logs while a 2-hour video transcribed).
These tests pin the fixed behaviour.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from unread.youtube.metadata import YoutubeMetadata
from unread.youtube.transcript import (
    _RETRY_HTTP_PATTERNS,
    YoutubeFetchError,
    _subtitle_candidates,
    _try_single_caption_track,
    _yt_dlp_run,
)

# ---------- _subtitle_candidates: ordered fallback list ------------------


def _meta(
    *,
    subtitles: dict | None = None,
    automatic_captions: dict | None = None,
) -> YoutubeMetadata:
    return YoutubeMetadata(
        video_id="abc123",
        url="https://youtu.be/abc123",
        subtitles=subtitles,
        automatic_captions=automatic_captions,
    )


def test_subtitle_candidates_preferred_lang_manual_then_auto() -> None:
    """Preferred lang's manual track wins, then its auto track, then fallback."""
    meta = _meta(
        subtitles={"ru": [{}], "en": [{}]},
        automatic_captions={"ru": [{}], "en": [{}], "es": [{}]},
    )
    cands = _subtitle_candidates(meta, ["ru", "en"])
    # ru manual, ru auto, en manual, en auto, then any remaining (es auto)
    assert cands[:2] == [("ru", False), ("ru", True)]
    assert cands[2:4] == [("en", False), ("en", True)]
    assert ("es", True) in cands


def test_subtitle_candidates_falls_back_to_any_available() -> None:
    """When no preferred lang is present, return remaining tracks alphabetically."""
    meta = _meta(
        subtitles={"de": [{}]},
        automatic_captions={"fr": [{}]},
    )
    cands = _subtitle_candidates(meta, ["ru", "en"])
    # No ru/en at all; surface what's there
    assert cands == [("de", False), ("fr", True)]


def test_subtitle_candidates_auto_only_preferred() -> None:
    """Preferred-lang auto beats fallback-lang manual (preserved invariant)."""
    meta = _meta(
        subtitles={"en": [{}]},
        automatic_captions={"ru": [{}]},
    )
    cands = _subtitle_candidates(meta, ["ru", "en"])
    # ru auto comes before en manual
    assert cands[0] == ("ru", True)
    assert cands[1] == ("en", False)


def test_subtitle_candidates_empty_when_no_tracks() -> None:
    meta = _meta()
    assert _subtitle_candidates(meta, ["ru", "en"]) == []


def test_subtitle_candidates_dedups_repeated_preferred() -> None:
    """`preferred=['ru','ru']` shouldn't double-emit ru."""
    meta = _meta(subtitles={"ru": [{}]})
    cands = _subtitle_candidates(meta, ["ru", "ru"])
    assert cands == [("ru", False)]


# ---------- _yt_dlp_run: retry on 429/5xx --------------------------------


class _FakeDownloadError(Exception):
    pass


def _fake_yt_dlp_module(side_effects: list) -> MagicMock:
    """Fake yt_dlp module whose YoutubeDL().download() walks `side_effects`.

    Each entry is either an exception instance to raise or `None` to
    succeed (return None). On exhaustion we keep returning the last
    behaviour so misuse fails loudly, not silently looping.
    """
    calls = {"i": 0}

    def _download_side_effect(_urls):
        i = calls["i"]
        calls["i"] += 1
        eff = side_effects[min(i, len(side_effects) - 1)]
        if isinstance(eff, BaseException):
            raise eff

    fake_ydl = MagicMock()
    fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
    fake_ydl.__exit__ = MagicMock(return_value=False)
    fake_ydl.download = MagicMock(side_effect=_download_side_effect)

    fake_module = MagicMock()
    fake_module.YoutubeDL = MagicMock(return_value=fake_ydl)
    fake_module.utils = MagicMock()
    fake_module.utils.DownloadError = _FakeDownloadError
    fake_module._calls = calls  # for assertions
    return fake_module


def test_yt_dlp_run_retries_on_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 on first attempt → retry → success. No exception bubbles up."""
    monkeypatch.setattr("unread.youtube.transcript.time.sleep", lambda _s: None)
    fake = _fake_yt_dlp_module(
        [
            _FakeDownloadError("ERROR: Unable to download: HTTP Error 429: Too Many Requests"),
            None,  # second attempt succeeds
        ]
    )
    _yt_dlp_run(fake, {"quiet": True}, "https://youtu.be/x", what="audio download")
    assert fake._calls["i"] == 2


def test_yt_dlp_run_retries_on_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """5xx is treated as transient too."""
    monkeypatch.setattr("unread.youtube.transcript.time.sleep", lambda _s: None)
    fake = _fake_yt_dlp_module(
        [
            _FakeDownloadError("HTTP Error 503: Service Unavailable"),
            None,
        ]
    )
    _yt_dlp_run(fake, {}, "https://youtu.be/x", what="captions[en]")
    assert fake._calls["i"] == 2


def test_yt_dlp_run_does_not_retry_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 (or any other DownloadError without a retriable signature)
    propagates immediately so deleted/private videos surface fast."""
    monkeypatch.setattr("unread.youtube.transcript.time.sleep", lambda _s: None)
    fake = _fake_yt_dlp_module(
        [
            _FakeDownloadError("Video unavailable"),
            None,  # never reached if non-retriable
        ]
    )
    with pytest.raises(_FakeDownloadError, match="Video unavailable"):
        _yt_dlp_run(fake, {}, "https://youtu.be/x", what="audio download")
    assert fake._calls["i"] == 1


def test_yt_dlp_run_gives_up_after_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """After max_attempts of 429s we surface the last DownloadError."""
    monkeypatch.setattr("unread.youtube.transcript.time.sleep", lambda _s: None)
    fake = _fake_yt_dlp_module([_FakeDownloadError("HTTP Error 429: Too Many Requests")])
    with pytest.raises(_FakeDownloadError, match="429"):
        _yt_dlp_run(fake, {}, "https://youtu.be/x", what="captions[ru]", max_attempts=3)
    assert fake._calls["i"] == 3


def test_yt_dlp_run_retry_429_false_skips_retries_on_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`retry_429=False` makes 429 raise on first attempt — no backoff loop.

    Captions use this: a 429 reaching our outer loop almost always
    means YouTube doesn't have the auto-translation rather than real
    rate-limiting, and we have other candidate langs + Whisper as
    fallbacks. Spending 4× exponential backoff on a dead-end track
    delays the user for ~17s for nothing.
    """
    monkeypatch.setattr("unread.youtube.transcript.time.sleep", lambda _s: None)
    fake = _fake_yt_dlp_module([_FakeDownloadError("HTTP Error 429: Too Many Requests")])
    with pytest.raises(_FakeDownloadError, match="429"):
        _yt_dlp_run(fake, {}, "https://youtu.be/x", what="captions[ru]", retry_429=False)
    assert fake._calls["i"] == 1


def test_yt_dlp_run_retry_429_false_still_retries_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`retry_429=False` only short-circuits 429; 5xx is still transient."""
    monkeypatch.setattr("unread.youtube.transcript.time.sleep", lambda _s: None)
    fake = _fake_yt_dlp_module(
        [
            _FakeDownloadError("HTTP Error 503: Service Unavailable"),
            None,
        ]
    )
    _yt_dlp_run(fake, {}, "https://youtu.be/x", what="captions[ru]", retry_429=False)
    assert fake._calls["i"] == 2


def test_retry_pattern_matches_known_signatures() -> None:
    """Sanity check the regex on real yt-dlp error wording."""
    assert _RETRY_HTTP_PATTERNS.search("HTTP Error 429: Too Many Requests")
    assert _RETRY_HTTP_PATTERNS.search("HTTP Error 502: Bad Gateway")
    assert _RETRY_HTTP_PATTERNS.search("HTTP Error 504: Gateway Timeout")
    assert _RETRY_HTTP_PATTERNS.search("Read timed out")
    assert _RETRY_HTTP_PATTERNS.search("Connection reset by peer")
    # Non-retriable
    assert not _RETRY_HTTP_PATTERNS.search("Video is unavailable")
    assert not _RETRY_HTTP_PATTERNS.search("HTTP Error 404: Not Found")
    assert not _RETRY_HTTP_PATTERNS.search("Sign in to confirm your age")


# ---------- _fetch_captions: fallback through candidates -----------------


@pytest.mark.asyncio
async def test_fetch_captions_falls_through_to_next_lang_on_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ru fails with 429 → try en → succeeds."""
    monkeypatch.setattr("unread.youtube.transcript.time.sleep", lambda _s: None)

    # Track which langs were tried
    tried: list[tuple[str, bool]] = []

    async def _fake_try(metadata, *, lang, is_auto):
        tried.append((lang, is_auto))
        if lang == "ru":
            return None
        # en succeeds
        return [(0, f"text {lang}")], lang, is_auto

    monkeypatch.setattr("unread.youtube.transcript._try_single_caption_track", _fake_try)

    from unread.youtube.transcript import _fetch_captions

    meta = _meta(
        subtitles={"ru": [{}], "en": [{}]},
        automatic_captions={"ru": [{}]},
    )
    result = await _fetch_captions(meta, preferred_langs=["ru", "en"])
    assert result is not None
    cues, lang, is_auto = result
    assert lang == "en"
    assert is_auto is False
    assert cues == [(0, "text en")]
    # Ensure ru was tried before en (fallback order)
    assert tried[0] == ("ru", False)
    assert ("en", False) in tried


@pytest.mark.asyncio
async def test_fetch_captions_returns_none_when_all_candidates_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every candidate 429s → return None so caller can switch to Whisper."""

    async def _fake_try(metadata, *, lang, is_auto):
        return None

    monkeypatch.setattr("unread.youtube.transcript._try_single_caption_track", _fake_try)

    from unread.youtube.transcript import _fetch_captions

    meta = _meta(subtitles={"ru": [{}], "en": [{}]})
    result = await _fetch_captions(meta, preferred_langs=["ru", "en"])
    assert result is None


# ---------- _try_single_caption_track: surfaces 429s as None ------------


@pytest.mark.asyncio
async def test_try_single_caption_track_returns_none_on_persistent_429(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """yt-dlp 429 on captions → single attempt, caller sees None, not a raise.

    Captions are best-effort and have multiple fallbacks (other lang
    candidates, Whisper), so a 429 from yt-dlp's outer surface is
    treated as "this track isn't available" — we don't burn ~17s on
    exponential backoff before moving on.
    """
    monkeypatch.setattr("unread.youtube.transcript.time.sleep", lambda _s: None)
    fake = _fake_yt_dlp_module([_FakeDownloadError("HTTP Error 429: Too Many Requests")])
    monkeypatch.setattr("unread.youtube.transcript._import_ytdlp", lambda: fake)

    meta = _meta(subtitles={"ru": [{}]})
    result = await _try_single_caption_track(meta, lang="ru", is_auto=False)
    assert result is None
    assert fake._calls["i"] == 1


# ---------- _download_audio: retries 429 via the same wrapper -----------


@pytest.mark.asyncio
async def test_download_audio_retries_on_429(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A 429 during audio download should retry and ultimately produce the file."""
    monkeypatch.setattr("unread.youtube.transcript.time.sleep", lambda _s: None)

    # First call 429s, second succeeds and creates the mp3.
    side_effects: list = [
        _FakeDownloadError("HTTP Error 429: Too Many Requests"),
        None,
    ]
    calls = {"i": 0}

    def _download_side_effect(_urls):
        i = calls["i"]
        calls["i"] += 1
        eff = side_effects[min(i, len(side_effects) - 1)]
        if isinstance(eff, BaseException):
            raise eff
        # Simulate yt-dlp + the FFmpegExtractAudio postprocessor having
        # produced an mp3 in dest_dir.
        (tmp_path / "abc123.mp3").write_bytes(b"\xff\xfb" + b"\x00" * 1024)

    fake = MagicMock()
    fake_ydl = MagicMock()
    fake_ydl.__enter__ = MagicMock(return_value=fake_ydl)
    fake_ydl.__exit__ = MagicMock(return_value=False)
    fake_ydl.download = MagicMock(side_effect=_download_side_effect)
    fake.YoutubeDL = MagicMock(return_value=fake_ydl)
    fake.utils = MagicMock()
    fake.utils.DownloadError = _FakeDownloadError

    monkeypatch.setattr("unread.youtube.transcript._import_ytdlp", lambda: fake)

    from unread.youtube.transcript import _download_audio

    meta = YoutubeMetadata(video_id="abc123", url="https://youtu.be/abc123", duration_sec=120)
    out = await _download_audio(meta, tmp_path)
    assert out.exists()
    assert calls["i"] == 2


@pytest.mark.asyncio
async def test_download_audio_wraps_persistent_429_as_youtube_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """If retries are exhausted, surface the friendly typed error."""
    monkeypatch.setattr("unread.youtube.transcript.time.sleep", lambda _s: None)
    fake_module = _fake_yt_dlp_module([_FakeDownloadError("HTTP Error 429: Too Many Requests")])
    with patch("unread.youtube.transcript._import_ytdlp", return_value=fake_module):
        from unread.youtube.transcript import _download_audio

        meta = YoutubeMetadata(video_id="abc", url="https://youtu.be/abc")
        with pytest.raises(YoutubeFetchError, match="429"):
            await _download_audio(meta, tmp_path)


# ---------- Whisper progress logs ---------------------------------------


@pytest.mark.asyncio
async def test_transcribe_audio_emits_progress_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Whisper path emits download/transcode/transcribe-chunk events.

    Previously the user saw nothing during a 2-hour Whisper run. After
    the fix we emit info-level structured logs at each phase boundary
    plus per-chunk so progress is observable.
    """
    captured: list[tuple[str, dict]] = []

    class _FakeLog:
        def info(self, event: str, **kw) -> None:
            captured.append((event, kw))

        def warning(self, event: str, **kw) -> None:  # pragma: no cover
            captured.append((event, kw))

        def error(self, event: str, **kw) -> None:  # pragma: no cover
            captured.append((event, kw))

    monkeypatch.setattr("unread.youtube.transcript.log", _FakeLog())

    # Stub out yt-dlp download to drop a fake mp3 in the temp dir.
    def _fake_run(yt_dlp, opts, url, *, what, max_attempts=4):
        out_template = opts["outtmpl"]
        # outtmpl is `<td>/%(id)s.%(ext)s` — extract the dir.
        out_dir = Path(out_template).parent
        (out_dir / "abc123.mp3").write_bytes(b"\xff\xfb" + b"\x00" * 2048)

    monkeypatch.setattr("unread.youtube.transcript._yt_dlp_run", _fake_run)
    monkeypatch.setattr(
        "unread.youtube.transcript._import_ytdlp",
        lambda: MagicMock(utils=MagicMock(DownloadError=_FakeDownloadError)),
    )

    # Stub transcoding to return two pretend segment files.
    seg1 = tmp_path / "seg1.mp3"
    seg2 = tmp_path / "seg2.mp3"
    seg1.write_bytes(b"a" * 1024)
    seg2.write_bytes(b"b" * 2048)

    async def _fake_transcode(src, kind, td, **kw):
        return [seg1, seg2]

    monkeypatch.setattr("unread.youtube.transcript.transcode_for_openai", _fake_transcode)

    # Stub OpenAI transcription per chunk.
    async def _fake_transcribe(_oai, _path, _model, _lang):
        return f"text from {Path(_path).name}"

    monkeypatch.setattr("unread.youtube.transcript._transcribe_file", _fake_transcribe)

    # Minimal Settings + Repo doubles.
    class _Repo:
        async def log_usage(self, **kw):
            pass

    class _OpenAICfg:
        api_key = "sk-test"
        request_timeout_sec = 60
        audio_model_default = "gpt-4o-mini-transcribe"
        audio_language = None

    class _AICfg:
        # Empty per-slot keys → resolve_audio falls back to openai +
        # `_DEFAULT_AUDIO_MODEL["openai"]` (or to legacy
        # `openai.audio_model_default` since chat_provider == openai).
        provider = ""
        chat_provider = ""
        filter_provider = ""
        audio_provider = ""
        audio_model = ""
        vision_provider = ""
        vision_model = ""
        chat_model = ""
        filter_model = ""
        base_url = ""

    class _Settings:
        openai = _OpenAICfg()
        ai = _AICfg()

    from unread.youtube.transcript import _transcribe_audio

    meta = YoutubeMetadata(video_id="abc123", url="https://youtu.be/abc123", duration_sec=180)
    transcript, _model, _cost, duration = await _transcribe_audio(meta, settings=_Settings(), repo=_Repo())
    assert "text from seg1.mp3" in transcript
    assert "text from seg2.mp3" in transcript
    assert duration == 180

    events = [e for e, _ in captured]
    # The phase boundaries we promised the user.
    assert "youtube.audio.download.start" in events
    assert "youtube.audio.download.ok" in events
    assert "youtube.audio.transcode.start" in events
    assert "youtube.audio.transcode.ok" in events
    assert "youtube.audio.transcribe.start" in events
    assert events.count("youtube.audio.transcribe.chunk") == 2
    assert "youtube.audio.transcribe.ok" in events

    # The transcribe.chunk events carry chunk index + total.
    chunk_events = [kw for e, kw in captured if e == "youtube.audio.transcribe.chunk"]
    assert chunk_events[0]["chunk"] == 1 and chunk_events[0]["total"] == 2
    assert chunk_events[1]["chunk"] == 2 and chunk_events[1]["total"] == 2
