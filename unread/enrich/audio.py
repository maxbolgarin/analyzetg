"""Audio transcription enricher for voice / videonote / video messages.

Downloads the media via existing `media.download` utilities, transcodes to
OpenAI-compatible mp3 (for video/videonote) via ffmpeg, and transcribes via
the audio slot's resolved provider (`settings.ai.audio_provider` —
openai / openrouter / local; capability filter snaps anthropic + google
back to openai). Results cache in `media_enrichments(kind='transcript')`
keyed by Telegram's `document_id` so the same audio forwarded across chats
is transcribed once.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

from unread.ai.providers import ProviderUnavailableError, make_audio_client, resolve_audio
from unread.config import get_settings
from unread.db.repo import Repo
from unread.enrich.base import EnrichResult
from unread.media.download import (
    FfmpegMissing,
    NoAudioStream,
    download_message,
    sha1_of_file,
    transcode_for_openai,
)
from unread.models import Message
from unread.util.flood import retry_on_429
from unread.util.logging import get_logger
from unread.util.pricing import audio_cost

if TYPE_CHECKING:
    from telethon import TelegramClient

log = get_logger(__name__)


def _audio_client_or_none() -> tuple[str, AsyncOpenAI] | None:
    """Resolve the audio slot's provider + construct its client.

    Returns ``(provider, client)`` on success, or ``None`` when the
    resolved provider has no key configured. Logs a one-line warning
    in the no-key case so the analyze run keeps going for text-only
    messages instead of crashing.
    """
    s = get_settings()
    provider, _model = resolve_audio(s)
    try:
        client = make_audio_client(provider, s)
    except ProviderUnavailableError as e:
        log.warning(
            "enrich.audio.skipped_no_key",
            provider=provider,
            err=str(e),
            hint="run `unread settings` and set the audio slot's API key",
        )
        return None
    return provider, client


@retry_on_429()
async def _transcribe_file(
    oai: AsyncOpenAI,
    path: Path,
    model: str,
    language: str | None,
) -> str:
    """Transcribe one audio file. `language=None`/empty → omit the param so
    Whisper autodetects (the API doesn't accept "auto" as a literal value)."""
    with path.open("rb") as f:
        kwargs: dict[str, object] = {
            "file": f,
            "model": model,
            "response_format": "text",
        }
        if language:
            kwargs["language"] = language
        resp = await oai.audio.transcriptions.create(**kwargs)
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

    # Resolve the audio slot's provider + construct its client. Skip
    # with a one-line warning when the resolved provider has no key —
    # the analyze pipeline keeps going for text-only messages.
    audio_resolved = _audio_client_or_none()
    if audio_resolved is None:
        return None
    audio_provider, oai = audio_resolved

    cached = await repo.get_media_enrichment(msg.media_doc_id, "transcript")
    if cached:
        content = cached.get("content") or ""
        used_model = cached.get("model") or ""
        await repo.set_message_transcript(msg.chat_id, msg.msg_id, content, used_model)
        msg.transcript = content
        msg.transcript_model = used_model
        return EnrichResult(kind="transcript", content=content, model=used_model, cache_hit=True)

    used_model = model or resolve_audio(settings)[1]
    # Empty string in config also means "autodetect" — normalize to None so
    # _transcribe_file omits the language param entirely.
    cfg_lang = settings.openai.audio_language or None
    used_lang = language or cfg_lang

    tmp_dir = settings.media.tmp_dir
    from unread.util.fsmode import ensure_private_dir

    ensure_private_dir(tmp_dir)
    tel_msg = await client.get_messages(msg.chat_id, ids=msg.msg_id)
    if tel_msg is None or tel_msg.media is None:
        log.warning("enrich.audio.no_media", chat_id=msg.chat_id, msg_id=msg.msg_id)
        return None

    src = tmp_dir / f"{msg.chat_id}_{msg.msg_id}"
    produced: list[Path] = []
    try:
        from unread.media.download import MediaTooLarge

        try:
            downloaded = await download_message(client, tel_msg, src)
        except MediaTooLarge as e:
            log.warning(
                "enrich.audio.too_large",
                chat_id=msg.chat_id,
                msg_id=msg.msg_id,
                err=str(e),
            )
            return None
        produced.append(downloaded)
        # gpt-4o-mini-transcribe / gpt-4o-transcribe / whisper-1 reject
        # opus voice files; force the transcoder to re-encode them as
        # mp3 first. The OpenRouter `openai/whisper-1` alias goes
        # through the same OpenAI Whisper backend, so it's covered too.
        prefer_mp3 = used_model in {
            "gpt-4o-mini-transcribe",
            "gpt-4o-transcribe",
            "whisper-1",
            "openai/whisper-1",
        }
        try:
            parts = await transcode_for_openai(downloaded, msg.media_type, tmp_dir, prefer_mp3=prefer_mp3)
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
                msg_date=msg.date.isoformat() if msg.date else None,
                media_type=msg.media_type,
            )
            return None
        produced.extend(p for p in parts if p != downloaded)

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
            context={
                "doc_id": msg.media_doc_id,
                "chat_id": msg.chat_id,
                "msg_id": msg.msg_id,
                "msg_date": msg.date.isoformat() if msg.date else None,
                "provider": audio_provider,
            },
        )
        log.info(
            "audio.transcribe",
            phase=f"enrich_{msg.media_type}",
            provider=audio_provider,
            model=used_model,
            seconds=duration,
            cost=float(cost or 0.0),
            doc_id=msg.media_doc_id,
            chat_id=msg.chat_id,
            msg_id=msg.msg_id,
            msg_date=msg.date.isoformat() if msg.date else None,
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
