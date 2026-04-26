"""Image enricher: download a photo, send to a vision model, cache the description.

Dedup key is the Telegram photo id (stable across chats). Uses the OpenAI
Chat Completions vision format (an `image_url` with a `data:` base64 payload),
so the whole pipeline works through the existing `AsyncOpenAI` client —
no separate endpoint or file upload.
"""

from __future__ import annotations

import base64
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

from analyzetg.config import get_settings
from analyzetg.db.repo import Repo
from analyzetg.enrich.base import EnrichResult
from analyzetg.media.download import download_message
from analyzetg.models import Message
from analyzetg.util.flood import retry_on_429
from analyzetg.util.logging import get_logger
from analyzetg.util.pricing import chat_cost

if TYPE_CHECKING:
    from telethon import TelegramClient

log = get_logger(__name__)

_SYSTEM_PROMPT: dict[str, str] = {
    "en": (
        "You're an assistant describing images from a Telegram chat for "
        "downstream analysis. Describe the image briefly (up to 3 sentences) "
        "in the same language as the caption / chat. Focus: what's shown, "
        "any visible text (verbatim), key details. No invention, no fluff."
    ),
    "ru": (
        "Ты помощник, описывающий изображения из Telegram-чата для последующего"
        " анализа. Опиши изображение кратко (до 3 предложений) на том же языке,"
        " что и подпись/чат. Фокус: что изображено, текст на картинке (если есть),"
        " ключевые детали. Без выдумок, без общих слов."
    ),
}

_USER_PROMPT: dict[str, str] = {
    "en": (
        "Describe this image. If text is visible, transcribe it verbatim. Reply as plain text, no markdown."
    ),
    "ru": (
        "Опиши это изображение. Если видно текст — передай его дословно. "
        "Ответь простым текстом, без markdown."
    ),
}


def _resolve_prompts(language: str) -> tuple[str, str]:
    return (
        _SYSTEM_PROMPT.get(language, _SYSTEM_PROMPT["en"]),
        _USER_PROMPT.get(language, _USER_PROMPT["en"]),
    )


def _openai_client() -> AsyncOpenAI:
    s = get_settings()
    return AsyncOpenAI(api_key=s.openai.api_key, timeout=s.openai.request_timeout_sec)


def _mime_from_path(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in {"jpg", "jpeg"}:
        return "image/jpeg"
    if suffix == "png":
        return "image/png"
    if suffix == "webp":
        return "image/webp"
    if suffix == "gif":
        return "image/gif"
    return "image/jpeg"


@retry_on_429()
async def _vision_complete(oai: AsyncOpenAI, model: str, messages: list[dict]) -> object:
    return await oai.chat.completions.create(
        model=model,
        messages=messages,
        max_completion_tokens=400,
        temperature=0.2,
    )


async def enrich_image(
    msg: Message,
    *,
    client: TelegramClient,
    repo: Repo,
    model: str | None = None,
    language: str | None = None,
) -> EnrichResult | None:
    """Return a short description of the photo attached to `msg`, or None.

    Skips silently when the message has no photo or no stable `media_doc_id`
    (which Telethon uses for the photo's `id`).
    """
    settings = get_settings()
    if msg.media_type != "photo" or msg.media_doc_id is None:
        return None

    cached = await repo.get_media_enrichment(msg.media_doc_id, "image_description")
    if cached:
        content = cached.get("content") or ""
        msg.image_description = content
        return EnrichResult(
            kind="image_description",
            content=content,
            model=cached.get("model"),
            cache_hit=True,
        )

    used_model = model or settings.enrich.vision_model
    lang = (language or settings.locale.language or "en").lower()
    sys_prompt, user_prompt = _resolve_prompts(lang)

    tmp_dir = settings.media.tmp_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tel_msg = await client.get_messages(msg.chat_id, ids=msg.msg_id)
    if tel_msg is None or tel_msg.media is None:
        log.warning("enrich.image.no_media", chat_id=msg.chat_id, msg_id=msg.msg_id)
        return None

    src = tmp_dir / f"img_{msg.chat_id}_{msg.msg_id}"
    downloaded: Path | None = None
    try:
        downloaded = await download_message(client, tel_msg, src)
        mime = _mime_from_path(downloaded)
        raw = downloaded.read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"

        messages = [
            {"role": "system", "content": sys_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]

        oai = _openai_client()
        resp = await _vision_complete(oai, used_model, messages)
        choice = resp.choices[0]
        description = (choice.message.content or "").strip()
        usage = getattr(resp, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        details = getattr(usage, "prompt_tokens_details", None)
        cached_tokens = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        cost = chat_cost(used_model, prompt_tokens, cached_tokens, completion_tokens) or 0.0

        if not description:
            log.warning("enrich.image.empty_response", chat_id=msg.chat_id, msg_id=msg.msg_id)
            return None

        await repo.put_media_enrichment(
            int(msg.media_doc_id),
            "image_description",
            description,
            model=used_model,
            cost_usd=float(cost),
        )
        await repo.log_usage(
            kind="chat",
            model=used_model,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            cost_usd=float(cost),
            context={
                "phase": "enrich_image",
                "doc_id": msg.media_doc_id,
                "chat_id": msg.chat_id,
                "msg_id": msg.msg_id,
                "msg_date": msg.date.isoformat() if msg.date else None,
            },
        )
        log.info(
            "openai.chat",
            phase="enrich_image",
            model=used_model,
            prompt=prompt_tokens,
            cached=cached_tokens,
            completion=completion_tokens,
            cost=float(cost),
            doc_id=msg.media_doc_id,
            chat_id=msg.chat_id,
            msg_id=msg.msg_id,
            msg_date=msg.date.isoformat() if msg.date else None,
        )
        msg.image_description = description
        return EnrichResult(
            kind="image_description",
            content=description,
            cost_usd=float(cost),
            model=used_model,
        )
    finally:
        if downloaded is not None:
            with contextlib.suppress(FileNotFoundError):
                downloaded.unlink()
