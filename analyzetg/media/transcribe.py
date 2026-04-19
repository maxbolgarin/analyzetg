"""Transcribe voice / videonote / video messages via OpenAI Audio API."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

from analyzetg.config import get_settings
from analyzetg.db.repo import Repo
from analyzetg.media.download import (
    FfmpegMissing,
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
    # response_format=text yields a plain string
    return resp if isinstance(resp, str) else getattr(resp, "text", str(resp))


async def transcribe_message(
    *,
    client: TelegramClient,
    repo: Repo,
    msg: Message,
    model: str | None = None,
    language: str | None = None,
) -> str | None:
    """Transcribe a single message if possible. Returns transcript text or None."""
    settings = get_settings()
    if msg.transcript is not None:
        return msg.transcript
    if msg.media_doc_id is None or msg.media_type is None:
        return None

    # Dedup by document_id across chats
    cached = await repo.get_media_transcript(msg.media_doc_id)
    if cached:
        await repo.set_message_transcript(
            msg.chat_id, msg.msg_id, cached["transcript"], cached["model"]
        )
        return cached["transcript"]

    used_model = model or settings.openai.audio_model_default
    used_lang = language or settings.openai.audio_language

    tmp_dir = settings.media.tmp_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tel_msg = await client.get_messages(msg.chat_id, ids=msg.msg_id)
    if tel_msg is None or tel_msg.media is None:
        log.warning("transcribe.no_media", chat_id=msg.chat_id, msg_id=msg.msg_id)
        return None

    src = tmp_dir / f"{msg.chat_id}_{msg.msg_id}"
    produced: list[Path] = []
    try:
        downloaded = await download_message(client, tel_msg, src)
        produced.append(downloaded)
        try:
            parts = await transcode_for_openai(downloaded, msg.media_type, tmp_dir)
        except FfmpegMissing as e:
            log.warning("transcribe.skipped_ffmpeg", err=str(e))
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

        # Record dedup cache (by doc_id)
        sha1: str | None = None
        with contextlib.suppress(Exception):
            sha1 = await asyncio.get_event_loop().run_in_executor(None, sha1_of_file, downloaded)
        await repo.put_media_transcript(
            doc_id=int(msg.media_doc_id),
            transcript=transcript,
            model=used_model,
            duration_sec=duration,
            language=used_lang,
            cost_usd=cost,
            file_sha1=sha1,
        )
        await repo.set_message_transcript(msg.chat_id, msg.msg_id, transcript, used_model)
        await repo.log_usage(
            kind="audio",
            model=used_model,
            audio_seconds=duration,
            cost_usd=cost,
            context={"doc_id": msg.media_doc_id, "chat_id": msg.chat_id, "msg_id": msg.msg_id},
        )
        return transcript
    finally:
        for p in produced:
            with contextlib.suppress(FileNotFoundError):
                p.unlink()
