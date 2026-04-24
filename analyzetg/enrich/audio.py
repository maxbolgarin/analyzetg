"""Audio transcription enricher for voice / videonote / video messages.

Downloads the media via existing `media.download` utilities, transcodes to
OpenAI-compatible mp3 (for video/videonote) via ffmpeg, and transcribes via
the OpenAI Audio API. Results cache in `media_enrichments(kind='transcript')`
keyed by Telegram's `document_id` so the same audio forwarded across chats
is transcribed once.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

from analyzetg.config import get_settings
from analyzetg.db.repo import Repo
from analyzetg.enrich.base import EnrichResult
from analyzetg.media.download import (
    FfmpegMissing,
    NoAudioStream,
    download_message,
    sha1_of_file,
    transcode_for_openai,
)
from analyzetg.models import Message
from analyzetg.util.flood import retry_on_429
from analyzetg.util.logging import get_logger
from analyzetg.util.pricing import audio_cost

if TYPE_CHECKING:
    from telethon import TelegramClient

log = get_logger(__name__)


def _openai_client() -> AsyncOpenAI:
    s = get_settings()
    return AsyncOpenAI(api_key=s.openai.api_key, timeout=s.openai.request_timeout_sec)


@retry_on_429()
async def _transcribe_file(oai: AsyncOpenAI, path: Path, model: str, language: str) -> str:
    with path.open("rb") as f:
        resp = await oai.audio.transcriptions.create(
            file=f,
            model=model,
            language=language or "auto",
            response_format="text",
        )
    return resp if isinstance(resp, str) else getattr(resp, "text", str(resp))


async def enrich_audio(
    msg: Message,
    *,
    client: TelegramClient,
    repo: Repo,
    model: str | None = None,
    language: str | None = None,
) -> EnrichResult | None:
    """Transcribe a voice / videonote / video message into text.

    Cache-check-then-call:
      1. If the message already has a transcript in-memory, return it as a hit.
      2. If the doc_id has a cached transcript (even from a different chat),
         copy it onto the message and return as hit.
      3. Otherwise download, transcode, transcribe, cache, return.

    Returns None only when there's no media to transcribe — the orchestrator
    treats that as a skip, not an error.
    """
    settings = get_settings()
    if msg.transcript is not None:
        return EnrichResult(kind="transcript", content=msg.transcript, cache_hit=True)
    if msg.media_doc_id is None or msg.media_type is None:
        return None

    cached = await repo.get_media_enrichment(msg.media_doc_id, "transcript")
    if cached:
        content = cached.get("content") or ""
        used_model = cached.get("model") or ""
        await repo.set_message_transcript(msg.chat_id, msg.msg_id, content, used_model)
        msg.transcript = content
        msg.transcript_model = used_model
        return EnrichResult(kind="transcript", content=content, model=used_model, cache_hit=True)

    used_model = model or settings.openai.audio_model_default
    used_lang = language or settings.openai.audio_language

    tmp_dir = settings.media.tmp_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tel_msg = await client.get_messages(msg.chat_id, ids=msg.msg_id)
    if tel_msg is None or tel_msg.media is None:
        log.warning("enrich.audio.no_media", chat_id=msg.chat_id, msg_id=msg.msg_id)
        return None

    src = tmp_dir / f"{msg.chat_id}_{msg.msg_id}"
    produced: list[Path] = []
    try:
        downloaded = await download_message(client, tel_msg, src)
        produced.append(downloaded)
        try:
            parts = await transcode_for_openai(downloaded, msg.media_type, tmp_dir)
        except FfmpegMissing as e:
            log.warning("enrich.audio.skipped_ffmpeg", err=str(e))
            return None
        except NoAudioStream:
            # Silent video or screen recording — nothing to transcribe. Not
            # an error, just a skip. Don't surface at WARNING level; a forum
            # may have hundreds of these.
            log.info(
                "enrich.audio.no_audio_track",
                chat_id=msg.chat_id,
                msg_id=msg.msg_id,
                media_type=msg.media_type,
            )
            return None
        produced.extend(p for p in parts if p != downloaded)

        oai = _openai_client()
        texts: list[str] = []
        for part in parts:
            text = await _transcribe_file(oai, part, used_model, used_lang)
            texts.append(text.strip())
        transcript = "\n".join(t for t in texts if t)
        duration = msg.media_duration or 0
        cost = audio_cost(used_model, duration)

        sha1: str | None = None
        with contextlib.suppress(Exception):
            sha1 = await asyncio.get_event_loop().run_in_executor(None, sha1_of_file, downloaded)
        await repo.put_media_enrichment(
            int(msg.media_doc_id),
            "transcript",
            transcript,
            model=used_model,
            cost_usd=cost,
            duration_sec=duration,
            language=used_lang,
            file_sha1=sha1,
        )
        await repo.set_message_transcript(msg.chat_id, msg.msg_id, transcript, used_model)
        msg.transcript = transcript
        msg.transcript_model = used_model
        await repo.log_usage(
            kind="audio",
            model=used_model,
            audio_seconds=duration,
            cost_usd=cost,
            context={"doc_id": msg.media_doc_id, "chat_id": msg.chat_id, "msg_id": msg.msg_id},
        )
        return EnrichResult(
            kind="transcript",
            content=transcript,
            cost_usd=float(cost or 0.0),
            model=used_model,
        )
    finally:
        for p in produced:
            with contextlib.suppress(FileNotFoundError):
                p.unlink()


# Legacy name kept so existing callers (analyzer/commands.py fallback, tests)
# that imported `transcribe_message` keep working during the transition.
async def transcribe_message(
    *,
    client: TelegramClient,
    repo: Repo,
    msg: Message,
    model: str | None = None,
    language: str | None = None,
) -> str | None:
    res = await enrich_audio(msg, client=client, repo=repo, model=model, language=language)
    return res.content if res else None
