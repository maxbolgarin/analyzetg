"""YouTube transcript: captions first, Whisper fallback.

Two paths share one entry point:

- **captions**: yt-dlp pulls the existing `.vtt` subtitles file (manual
  uploads first, then YouTube's auto-captions). Free, instant. Returned
  text is the concatenated cue payload with timing + dedup of the
  rolling-overlap artifacts auto-captions emit.
- **audio**: yt-dlp downloads bestaudio + transcodes to mp3 → reuse
  `media.download.transcode_for_openai` to segment >24 MB files into
  600-second mp3 chunks → `enrich.audio._transcribe_file` per chunk.
  Costs Whisper-per-minute; logs to `usage_log` with `phase=enrich_youtube`.

`source="auto"` tries captions, falls back to audio when none. Both
paths return a `TranscriptResult` with `source` set so the caller can
record which path actually ran.
"""

from __future__ import annotations

import asyncio
import random
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from openai import AsyncOpenAI

from unread.config import Settings
from unread.db.repo import Repo
from unread.enrich.audio import _transcribe_file
from unread.media.download import (
    FfmpegMissing,
    NoAudioStream,
    transcode_for_openai,
)
from unread.util.flood import _user_visible_retry_status
from unread.util.logging import get_logger
from unread.util.pricing import audio_cost
from unread.youtube.metadata import YoutubeMetadata, _import_ytdlp

log = get_logger(__name__)

TranscriptSource = Literal["auto", "captions", "audio"]


class NoTranscriptAvailable(RuntimeError):
    """Raised when `source='captions'` was requested but the video has none."""


class YoutubeFetchError(RuntimeError):
    """Raised when yt-dlp can't fetch the video / its captions / its audio.

    Wraps `yt_dlp.utils.DownloadError` and any other exception coming
    out of `ydl.download()`. The command-layer wrapper turns this into
    a `typer.BadParameter`/`Exit` with a friendly banner; without this,
    an out-of-the-blue `DownloadError` traceback hits the user's
    terminal whenever YouTube changes a video format or a video is
    deleted/private/region-locked.
    """


@dataclass(slots=True)
class TranscriptResult:
    text: str
    source: Literal["captions", "audio"]
    language: str | None
    duration_sec: int | None
    cost_usd: float
    # Per-cue timestamps preserved from the captions track. Each entry is
    # `(start_sec, line_text)`. None for the audio path (Whisper text mode
    # has no segment markers; offsets get spread uniformly downstream).
    timed_cues: list[tuple[int, str]] | None = None
    # True when the captions track was YouTube's auto-generated one (vs a
    # manually uploaded one); None for the audio path.
    is_auto: bool | None = None


# yt-dlp emits VTT cue blocks like:
#   00:00:00.000 --> 00:00:03.500 align:start position:0%
#   <c.colorE5E5E5>line one</c><c>...</c>
# We strip timing, tags, the WEBVTT/Kind/Language headers, and dedup
# the rolling-overlap repeats auto-captions emit (same text appears in
# 3-5 consecutive cues).
_VTT_HEADER = re.compile(r"^(WEBVTT|Kind:|Language:|NOTE\b|STYLE\b)", re.IGNORECASE)
_VTT_TIMING = re.compile(r"(\d{2}):(\d{2}):(\d{2})\.\d{3}\s+-->\s+\d{2}:\d{2}:\d{2}\.\d{3}")
_VTT_TAG = re.compile(r"<[^>]+>")


def _parse_vtt_timed(text: str) -> list[tuple[int, str]]:
    """Parse a WEBVTT body into `[(start_sec, text), …]`.

    Each entry's `start_sec` is the cue's start offset in whole seconds.
    Lines from the same cue are joined with " ". Adjacent duplicates
    (the rolling-overlap pattern auto-captions emit, where the same
    payload appears 3-5 times across consecutive cues) are dropped —
    only the *first* occurrence of a given text body is kept.
    """
    out: list[tuple[int, str]] = []
    seen: set[str] = set()
    cur_start: int | None = None
    cur_lines: list[str] = []

    def _flush() -> None:
        nonlocal cur_start, cur_lines
        if cur_start is None or not cur_lines:
            cur_start, cur_lines = None, []
            return
        body = " ".join(cur_lines).strip()
        cur_start, cur_lines = None, []
        if not body or body in seen:
            return
        seen.add(body)
        out.append((cur_start_local, body))

    cur_start_local: int = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            _flush()
            continue
        if _VTT_HEADER.match(line):
            continue
        m = _VTT_TIMING.search(line)
        if m:
            _flush()
            h, mm, ss = int(m.group(1)), int(m.group(2)), int(m.group(3))
            cur_start = h * 3600 + mm * 60 + ss
            cur_start_local = cur_start
            cur_lines = []
            continue
        if line.isdigit():
            # Numeric cue identifier — skip
            continue
        if cur_start is None:
            continue  # body line outside any cue — ignore
        cleaned = _VTT_TAG.sub("", line).strip()
        if cleaned:
            cur_lines.append(cleaned)
    _flush()
    return out


def _parse_vtt(text: str) -> str:
    """Return plain cue text (timestamps stripped). Back-compat shim."""
    return "\n".join(t for _, t in _parse_vtt_timed(text))


def _subtitle_candidates(
    metadata: YoutubeMetadata,
    preferred: list[str],
) -> list[tuple[str, bool]]:
    """Ordered list of `(lang_code, is_auto)` candidates to try.

    Order:
      1. For each preferred language: manual track first, then auto.
      2. Any remaining manual tracks (alphabetical, deterministic).
      3. Any remaining auto tracks (alphabetical, deterministic).

    Preserving the "preferred-language wins over manual-vs-auto"
    invariant: an auto Russian track in an install configured for
    Russian source content beats English manual subs that happen to
    be present.
    The list lets callers fall back when one entry 429s or returns
    empty — previously a single 429 on the user's preferred lang
    sent the whole flow to Whisper.
    """
    manual = metadata.subtitles or {}
    auto = metadata.automatic_captions or {}
    out: list[tuple[str, bool]] = []
    seen: set[tuple[str, bool]] = set()

    def _add(lang: str, is_auto: bool) -> None:
        key = (lang, is_auto)
        if key in seen:
            return
        seen.add(key)
        out.append(key)

    for lang in preferred:
        if lang in manual:
            _add(lang, False)
        if lang in auto:
            _add(lang, True)
    for lang in sorted(manual):
        _add(lang, False)
    for lang in sorted(auto):
        _add(lang, True)
    return out


# Patterns in `yt_dlp.utils.DownloadError` messages that mark a
# transient HTTP failure worth retrying. 429 = rate limit, 5xx =
# server-side hiccup. "Read timed out" / connection-reset hits pop up
# during long audio downloads when YouTube briefly closes the socket.
_RETRY_HTTP_PATTERNS = re.compile(
    r"\bHTTP Error (?:429|5\d\d)\b"
    r"|\bToo Many Requests\b"
    r"|\bService Unavailable\b"
    r"|\bBad Gateway\b"
    r"|\bGateway Timeout\b"
    r"|\bRead timed out\b"
    r"|\bConnection (?:reset|aborted)\b",
    re.IGNORECASE,
)
_HTTP_429_PATTERN = re.compile(r"\bHTTP Error 429\b|\bToo Many Requests\b", re.IGNORECASE)


def _yt_dlp_run(
    yt_dlp: Any,
    opts: dict[str, Any],
    url: str,
    *,
    what: str,
    max_attempts: int = 4,
    retry_429: bool = True,
) -> None:
    """Run `ydl.download([url])` with our own retry-on-rate-limit loop.

    yt-dlp's built-in `retries` option helps with main-stream
    fragments but doesn't always cover the subtitle-fetch sub-step
    where YouTube currently 429s most often. We catch DownloadError,
    inspect the message for a known-retriable HTTP status / timeout,
    and back off exponentially. Non-retriable errors re-raise
    immediately so a deleted/private/region-locked video still
    surfaces fast.

    `retry_429=False` makes 429 raise immediately (5xx/timeouts still
    retry). Use for caption fetches: yt-dlp already retries internally,
    and a 429 that reaches us almost always means YouTube's
    auto-translation is unavailable rather than real rate-limiting —
    the next candidate language (or Whisper fallback) will succeed
    faster than 4× exponential backoff.

    Sync (called from `asyncio.to_thread`); `time.sleep` is fine.
    """
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            return
        except yt_dlp.utils.DownloadError as e:
            msg = str(e)
            if not _RETRY_HTTP_PATTERNS.search(msg):
                raise
            if not retry_429 and _HTTP_429_PATTERN.search(msg):
                raise
            last_err = e
            if attempt == max_attempts - 1:
                break
            delay = min(2.0**attempt, 30.0) + random.uniform(0, 1.5)
            log.warning(
                "youtube.yt_dlp.retry",
                what=what,
                attempt=attempt + 1,
                delay=round(delay, 2),
                err=msg[:200],
            )
            _user_visible_retry_status(
                f"YouTube rate limit on {what} — retrying in {delay:.0f}s "
                f"(attempt {attempt + 1}/{max_attempts})…"
            )
            time.sleep(delay)
    assert last_err is not None
    raise last_err


def _yt_dlp_base_opts() -> dict[str, Any]:
    """Common yt-dlp options that bake in modest internal retries.

    Ours is the outer retry loop; yt-dlp's `retries` covers the
    fragment-level transient stuff (slow chunks, partial reads).
    """
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
    }


async def _fetch_captions(
    metadata: YoutubeMetadata,
    *,
    preferred_langs: list[str],
) -> tuple[list[tuple[int, str]], str, bool] | None:
    """Try each candidate subtitle track in order; return first non-empty parse.

    Per-candidate failures (429, empty file, parse-empty) fall
    through to the next entry instead of aborting the whole captions
    path. Only when every candidate is exhausted does the caller
    fall back to Whisper.
    """
    candidates = _subtitle_candidates(metadata, preferred_langs)
    if not candidates:
        log.info("youtube.captions.none", video_id=metadata.video_id)
        return None

    attempted: list[str] = []
    for lang, is_auto in candidates:
        attempted.append(f"{lang}{'(auto)' if is_auto else ''}")
        result = await _try_single_caption_track(metadata, lang=lang, is_auto=is_auto)
        if result is not None:
            return result

    log.warning(
        "youtube.captions.all_failed",
        video_id=metadata.video_id,
        attempted=",".join(attempted),
    )
    return None


async def _try_single_caption_track(
    metadata: YoutubeMetadata,
    *,
    lang: str,
    is_auto: bool,
) -> tuple[list[tuple[int, str]], str, bool] | None:
    """Download + parse one subtitle track. None on any failure."""

    def _download() -> str | None:
        yt_dlp = _import_ytdlp()
        with tempfile.TemporaryDirectory() as td:
            opts = {
                **_yt_dlp_base_opts(),
                "skip_download": True,
                "writesubtitles": not is_auto,
                "writeautomaticsub": is_auto,
                "subtitleslangs": [lang],
                "subtitlesformat": "vtt",
                "outtmpl": str(Path(td) / "%(id)s.%(ext)s"),
            }
            try:
                _yt_dlp_run(
                    yt_dlp,
                    opts,
                    metadata.url,
                    what=f"captions[{lang}{'/auto' if is_auto else ''}]",
                    retry_429=False,
                )
            except yt_dlp.utils.DownloadError as e:
                # Captions are best-effort — log and let the next
                # candidate try. Warn (not exception) so a transient
                # YouTube hiccup doesn't spam diagnostics.
                log.warning(
                    "youtube.captions.download_failed",
                    video_id=metadata.video_id,
                    lang=lang,
                    is_auto=is_auto,
                    err=str(e)[:200],
                )
                return None
            for f in Path(td).iterdir():
                if f.suffix == ".vtt":
                    return f.read_text(encoding="utf-8", errors="ignore")
        return None

    raw = await asyncio.to_thread(_download)
    if not raw:
        log.warning(
            "youtube.captions.empty",
            video_id=metadata.video_id,
            lang=lang,
            is_auto=is_auto,
        )
        return None
    cues = _parse_vtt_timed(raw)
    if not cues:
        log.warning(
            "youtube.captions.parse_empty",
            video_id=metadata.video_id,
            lang=lang,
            is_auto=is_auto,
        )
        return None
    total_chars = sum(len(c[1]) for c in cues)
    log.info(
        "youtube.captions.ok",
        video_id=metadata.video_id,
        lang=lang,
        is_auto=is_auto,
        cues=len(cues),
        chars=total_chars,
    )
    return cues, lang, is_auto


async def _download_audio(metadata: YoutubeMetadata, dest_dir: Path) -> Path:
    """Download bestaudio as mp3 into `dest_dir/<video_id>.mp3`. Sync via to_thread."""
    out_template = str(dest_dir / "%(id)s.%(ext)s")
    log.info(
        "youtube.audio.download.start",
        video_id=metadata.video_id,
        duration_sec=metadata.duration_sec,
    )

    def _run() -> Path:
        yt_dlp = _import_ytdlp()
        opts = {
            **_yt_dlp_base_opts(),
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
            _yt_dlp_run(yt_dlp, opts, metadata.url, what="audio download")
        except yt_dlp.utils.DownloadError as e:
            # Re-raise as our typed error so the command layer can show a
            # friendly banner instead of yt-dlp's internal traceback.
            # Common triggers: deleted / private / region-locked / age-
            # restricted video, persistent rate-limit (after retries),
            # yt-dlp lagging behind a YouTube format change.
            raise YoutubeFetchError(str(e)) from e
        for f in dest_dir.iterdir():
            if f.suffix == ".mp3":
                return f
        raise YoutubeFetchError(f"yt-dlp produced no mp3 in {dest_dir}")

    path = await asyncio.to_thread(_run)
    size_mb = path.stat().st_size / (1024 * 1024)
    log.info(
        "youtube.audio.download.ok",
        video_id=metadata.video_id,
        size_mb=round(size_mb, 2),
    )
    return path


async def _transcribe_audio(
    metadata: YoutubeMetadata,
    *,
    settings: Settings,
    repo: Repo,
) -> tuple[str, str, float, int]:
    """Whisper path: download audio → segment → transcribe → return text+model+cost+seconds.

    Whisper is OpenAI-only; if the user picked a different chat
    provider and didn't add an OpenAI key, raise a focused error
    instead of letting the SDK throw a 401 mid-download.
    """
    if not settings.openai.api_key:
        from unread.i18n import t as _t

        raise RuntimeError(_t("youtube_whisper_no_openai"))
    audio_model = settings.openai.audio_model_default
    cfg_lang = settings.openai.audio_language or None
    duration = int(metadata.duration_sec or 0)

    with tempfile.TemporaryDirectory() as td_str:
        td = Path(td_str)
        downloaded = await _download_audio(metadata, td)
        log.info(
            "youtube.audio.transcode.start",
            video_id=metadata.video_id,
        )
        try:
            parts = await transcode_for_openai(downloaded, "video", td)
        except FfmpegMissing as e:
            from unread.i18n import t as _t

            raise FfmpegMissing(_t("error_ffmpeg_missing_youtube")) from e
        except NoAudioStream as e:
            from unread.i18n import t as _t

            raise RuntimeError(_t("youtube_no_audio_track").format(video_id=metadata.video_id)) from e
        log.info(
            "youtube.audio.transcode.ok",
            video_id=metadata.video_id,
            segments=len(parts),
        )

        oai = AsyncOpenAI(
            api_key=settings.openai.api_key,
            timeout=settings.openai.request_timeout_sec,
        )
        log.info(
            "youtube.audio.transcribe.start",
            video_id=metadata.video_id,
            segments=len(parts),
            model=audio_model,
        )
        texts: list[str] = []
        for idx, part in enumerate(parts, start=1):
            part_size_mb = round(part.stat().st_size / (1024 * 1024), 2)
            log.info(
                "youtube.audio.transcribe.chunk",
                video_id=metadata.video_id,
                chunk=idx,
                total=len(parts),
                size_mb=part_size_mb,
            )
            piece = await _transcribe_file(oai, part, audio_model, cfg_lang)
            texts.append(piece.strip())
    transcript = "\n".join(t for t in texts if t)
    cost = float(audio_cost(audio_model, duration) or 0.0)
    await repo.log_usage(
        kind="audio",
        model=audio_model,
        audio_seconds=duration,
        cost_usd=cost,
        context={
            "phase": "enrich_youtube",
            "video_id": metadata.video_id,
            "channel_id": metadata.channel_id,
        },
    )
    log.info(
        "youtube.audio.transcribe.ok",
        video_id=metadata.video_id,
        chars=len(transcript),
        cost=round(cost, 4),
    )
    log.info(
        "openai.audio",
        phase="enrich_youtube",
        model=audio_model,
        seconds=duration,
        cost=cost,
        video_id=metadata.video_id,
    )
    return transcript, audio_model, cost, duration


def _preferred_caption_langs(settings: Settings) -> list[str]:
    """Caption language preference.

    Order:
      1. ``locale.content_language`` — Whisper-style source hint. When
         set, the user is telling us what language the input is in, so
         that's the highest-priority caption track to fetch.
      2. ``locale.report_language`` — output language. A reasonable
         second guess if the captions are user-supplied translations.
      3. ``locale.language`` — UI language fallback.
      4. ``en``, ``ru`` — final default fallbacks so the picker still
         finds something if none of the above is set.
    """
    locale = getattr(settings, "locale", None)
    out: list[str] = []
    for attr in ("content_language", "report_language", "language"):
        val = (getattr(locale, attr, "") or "").lower()
        if val and val not in out:
            out.append(val)
    for fallback in ("en", "ru"):
        if fallback not in out:
            out.append(fallback)
    return out


async def get_transcript(
    metadata: YoutubeMetadata,
    *,
    source: TranscriptSource = "auto",
    settings: Settings,
    repo: Repo,
) -> TranscriptResult:
    """Resolve a transcript per the requested source.

    `source='captions'`: raises `NoTranscriptAvailable` if YouTube has none.
    `source='audio'`: always Whisper. Skips the captions probe.
    `source='auto'`: captions first, Whisper fallback.
    """
    preferred = _preferred_caption_langs(settings)

    if source in ("captions", "auto"):
        captions = await _fetch_captions(metadata, preferred_langs=preferred)
        if captions is not None:
            cues, lang, is_auto = captions
            text = "\n".join(c[1] for c in cues)
            return TranscriptResult(
                text=text,
                source="captions",
                language=lang,
                duration_sec=metadata.duration_sec,
                cost_usd=0.0,
                timed_cues=cues,
                is_auto=is_auto,
            )
        if source == "captions":
            raise NoTranscriptAvailable(
                f"video {metadata.video_id} has no captions; re-run with "
                "--youtube-source=audio to use Whisper instead."
            )

    text, _model, cost, duration = await _transcribe_audio(metadata, settings=settings, repo=repo)
    return TranscriptResult(
        text=text,
        source="audio",
        language=settings.openai.audio_language or None,
        duration_sec=duration,
        cost_usd=cost,
        timed_cues=None,
    )
