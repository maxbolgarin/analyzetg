"""Image enricher: download a photo, send to a vision model, cache the description.

Dedup key is the Telegram photo id (stable across chats). Routes through
the vision slot's resolved provider (`settings.ai.vision_provider`) —
OpenAI / Anthropic / Google / OpenRouter / local each have a native
adapter in :mod:`unread.ai.vision_provider`.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

from unread.ai.providers import ProviderSafetyBlockedError, ProviderUnavailableError, resolve_vision
from unread.ai.vision_provider import make_vision_provider
from unread.config import get_settings
from unread.db.repo import Repo
from unread.enrich.base import EnrichResult
from unread.media.download import download_message
from unread.models import Message
from unread.util.logging import get_logger
from unread.util.pricing import chat_cost

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

    # Resolve the vision slot's provider + model. If the resolved
    # provider has no key configured, skip cleanly with a one-line
    # warning so the rest of the analyze pipeline keeps going.
    vision_provider, default_model = resolve_vision(settings)
    used_model = model or default_model
    try:
        adapter = make_vision_provider(vision_provider, settings)
    except ProviderUnavailableError as e:
        log.warning(
            "enrich.image.skipped_no_key",
            provider=vision_provider,
            chat_id=msg.chat_id,
            msg_id=msg.msg_id,
            err=str(e),
            hint="run `unread settings` and set the vision slot's API key",
        )
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

    # Fall back through report_language → language so descriptions match
    # the analysis output language regardless of UI locale.
    lang = (language or settings.locale.report_language or settings.locale.language or "en").lower()
    sys_prompt, user_prompt = _resolve_prompts(lang)

    tmp_dir = settings.media.tmp_dir
    from unread.util.fsmode import ensure_private_dir

    ensure_private_dir(tmp_dir)
    tel_msg = await client.get_messages(msg.chat_id, ids=msg.msg_id)
    if tel_msg is None or tel_msg.media is None:
        log.warning("enrich.image.no_media", chat_id=msg.chat_id, msg_id=msg.msg_id)
        return None

    src = tmp_dir / f"img_{msg.chat_id}_{msg.msg_id}"
    downloaded: Path | None = None
    try:
        from unread.media.download import MediaTooLarge

        try:
            downloaded = await download_message(client, tel_msg, src)
        except MediaTooLarge as e:
            log.warning(
                "enrich.image.too_large",
                chat_id=msg.chat_id,
                msg_id=msg.msg_id,
                err=str(e),
            )
            return None
        mime = _mime_from_path(downloaded)
        raw = downloaded.read_bytes()

        try:
            result = await adapter.describe_image(
                model=used_model,
                image_bytes=raw,
                mime_type=mime,
                system_prompt=sys_prompt,
                user_prompt=user_prompt,
                max_tokens=400,
                temperature=0.2,
            )
        except ProviderSafetyBlockedError as e:
            log.warning(
                "enrich.image.safety_blocked",
                provider=vision_provider,
                model=used_model,
                chat_id=msg.chat_id,
                msg_id=msg.msg_id,
                reason=getattr(e, "reason", ""),
            )
            return None

        description = result.text
        cost = (
            chat_cost(
                used_model,
                result.prompt_tokens,
                result.cached_tokens,
                result.completion_tokens,
            )
            or 0.0
        )

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
            prompt_tokens=result.prompt_tokens,
            cached_tokens=result.cached_tokens,
            completion_tokens=result.completion_tokens,
            cost_usd=float(cost),
            context={
                "phase": "enrich_image",
                "doc_id": msg.media_doc_id,
                "chat_id": msg.chat_id,
                "msg_id": msg.msg_id,
                "msg_date": msg.date.isoformat() if msg.date else None,
                "provider": vision_provider,
            },
        )
        log.info(
            "vision.describe",
            phase="enrich_image",
            provider=vision_provider,
            model=used_model,
            prompt=result.prompt_tokens,
            cached=result.cached_tokens,
            completion=result.completion_tokens,
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
