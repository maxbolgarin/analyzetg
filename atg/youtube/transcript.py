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
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from openai import AsyncOpenAI

from atg.config import Settings
from atg.db.repo import Repo
from atg.enrich.audio import _transcribe_file
from atg.media.download import (
    FfmpegMissing,
    NoAudioStream,
    transcode_for_openai,
)
from atg.util.logging import get_logger
from atg.util.pricing import audio_cost
from atg.youtube.metadata import YoutubeMetadata, _import_ytdlp

log = get_logger(__name__)

TranscriptSource = Literal["auto", "captions", "audio"]


class NoTranscriptAvailable(RuntimeError):
    """Raised when `source='captions'` was requested but the video has none."""


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


def _pick_subtitle_lang(
    metadata: YoutubeMetadata,
    preferred: list[str],
) -> tuple[str, bool] | None:
    """Choose the best subtitle language code + whether it's auto-generated.

    Returns `None` if no subtitles in any form exist.
    """
    manual = metadata.subtitles or {}
    auto = metadata.automatic_captions or {}
    for lang in preferred:
        if lang in manual:
            return lang, False
    for lang in preferred:
        if lang in auto:
            return lang, True
    if manual:
        return next(iter(manual)), False
    if auto:
        return next(iter(auto)), True
    return None


async def _fetch_captions(
    metadata: YoutubeMetadata,
    *,
    preferred_langs: list[str],
) -> tuple[list[tuple[int, str]], str] | None:
    """Download + parse subtitles. Returns `(timed_cues, lang_code)` or None.

    Uses a separate yt-dlp invocation per language pick (small) inside
    `asyncio.to_thread` since yt-dlp's API is sync.
    """
    pick = _pick_subtitle_lang(metadata, preferred_langs)
    if pick is None:
        return None
    lang, is_auto = pick

    def _download() -> str | None:
        yt_dlp = _import_ytdlp()
        with tempfile.TemporaryDirectory() as td:
            opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "writesubtitles": not is_auto,
                "writeautomaticsub": is_auto,
                "subtitleslangs": [lang],
                "subtitlesformat": "vtt",
                "outtmpl": str(Path(td) / "%(id)s.%(ext)s"),
                "noplaylist": True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([metadata.url])
            for f in Path(td).iterdir():
                if f.suffix == ".vtt":
                    return f.read_text(encoding="utf-8", errors="ignore")
        return None

    raw = await asyncio.to_thread(_download)
    if not raw:
        log.warning("youtube.captions.empty", video_id=metadata.video_id, lang=lang)
        return None
    cues = _parse_vtt_timed(raw)
    if not cues:
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
    return cues, lang


async def _download_audio(metadata: YoutubeMetadata, dest_dir: Path) -> Path:
    """Download bestaudio as mp3 into `dest_dir/<video_id>.mp3`. Sync via to_thread."""
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
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([metadata.url])
        for f in dest_dir.iterdir():
            if f.suffix == ".mp3":
                return f
        raise RuntimeError(f"yt-dlp produced no mp3 in {dest_dir}")

    return await asyncio.to_thread(_run)


async def _transcribe_audio(
    metadata: YoutubeMetadata,
    *,
    settings: Settings,
    repo: Repo,
) -> tuple[str, str, float, int]:
    """Whisper path: download audio → segment → transcribe → return text+model+cost+seconds."""
    audio_model = settings.openai.audio_model_default
    cfg_lang = settings.openai.audio_language or None
    duration = int(metadata.duration_sec or 0)

    with tempfile.TemporaryDirectory() as td_str:
        td = Path(td_str)
        downloaded = await _download_audio(metadata, td)
        try:
            parts = await transcode_for_openai(downloaded, "video", td)
        except FfmpegMissing as e:
            raise FfmpegMissing(
                "ffmpeg required to transcribe YouTube audio; install ffmpeg or update [media] ffmpeg_path."
            ) from e
        except NoAudioStream as e:
            raise RuntimeError(f"YouTube video {metadata.video_id} has no audio track") from e

        oai = AsyncOpenAI(
            api_key=settings.openai.api_key,
            timeout=settings.openai.request_timeout_sec,
        )
        texts: list[str] = []
        for part in parts:
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
        "openai.audio",
        phase="enrich_youtube",
        model=audio_model,
        seconds=duration,
        cost=cost,
        video_id=metadata.video_id,
    )
    return transcript, audio_model, cost, duration


def _preferred_caption_langs(settings: Settings) -> list[str]:
    """Caption language preference. Configured content_language wins, then en+ru."""
    locale = getattr(settings, "locale", None)
    cfg = (getattr(locale, "content_language", "") or getattr(locale, "language", "") or "").lower()
    out = [cfg] if cfg else []
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
            cues, lang = captions
            text = "\n".join(c[1] for c in cues)
            return TranscriptResult(
                text=text,
                source="captions",
                language=lang,
                duration_sec=metadata.duration_sec,
                cost_usd=0.0,
                timed_cues=cues,
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
