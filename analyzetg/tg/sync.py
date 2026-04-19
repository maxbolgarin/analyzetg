"""Incremental synchronization of Telegram subscriptions (spec §7)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from analyzetg.config import get_settings
from analyzetg.db.repo import Repo
from analyzetg.models import Message, Subscription
from analyzetg.util.flood import RateLimiter, retry_on_flood
from analyzetg.util.logging import get_logger

if TYPE_CHECKING:
    from telethon import TelegramClient

log = get_logger(__name__)


# ------------------------------------------------------------- media detection


def detect_media(msg: Any) -> tuple[str | None, int | None, int | None]:
    """Return (media_type, doc_id, duration_sec) per spec §8.1."""
    try:
        from telethon.tl.types import (  # type: ignore[attr-defined]
            DocumentAttributeAudio,
            DocumentAttributeVideo,
            MessageMediaDocument,
            MessageMediaPhoto,
        )
    except Exception:
        return None, None, None

    media = getattr(msg, "media", None)
    if media is None:
        return None, None, None
    if isinstance(media, MessageMediaPhoto):
        return "photo", None, None
    if isinstance(media, MessageMediaDocument):
        doc = getattr(media, "document", None)
        if doc is None:
            return "doc", None, None
        doc_id = getattr(doc, "id", None)
        for attr in getattr(doc, "attributes", []) or []:
            if isinstance(attr, DocumentAttributeAudio):
                if getattr(attr, "voice", False):
                    return "voice", doc_id, int(getattr(attr, "duration", 0) or 0)
                return "doc", doc_id, int(getattr(attr, "duration", 0) or 0)
            if isinstance(attr, DocumentAttributeVideo):
                kind = "videonote" if getattr(attr, "round_message", False) else "video"
                return kind, doc_id, int(getattr(attr, "duration", 0) or 0)
        return "doc", doc_id, None
    return None, None, None


# --------------------------------------------------------------- normalization


def _forward_str(fwd) -> str | None:
    if fwd is None:
        return None
    # from_name is set for hidden senders; else sender_id / from_id
    name = getattr(fwd, "from_name", None)
    if name:
        return name
    from_id = getattr(fwd, "from_id", None)
    if from_id is not None:
        return f"id:{getattr(from_id, 'user_id', getattr(from_id, 'channel_id', from_id))}"
    return None


def _sender_display(msg: Any) -> tuple[int | None, str | None]:
    sender_id = getattr(msg, "sender_id", None)
    sender = getattr(msg, "sender", None)
    name: str | None = None
    if sender is not None:
        username = getattr(sender, "username", None)
        if username:
            name = f"@{username}"
        else:
            first = getattr(sender, "first_name", "") or ""
            last = getattr(sender, "last_name", "") or ""
            title = getattr(sender, "title", None)
            name = (title or f"{first} {last}").strip() or None
    return sender_id, name


def _thread_id_for(msg: Any, subscription: Subscription) -> int | None:
    """Derive thread_id per spec §7.5.

    - For forum topic subscription → the reply_to_top_id / topic id.
    - For comments subscription → subscription.thread_id (discussion group is flat).
    - Otherwise → None (main timeline).
    """
    reply_to = getattr(msg, "reply_to", None)
    if reply_to is not None and getattr(reply_to, "forum_topic", False):
        top = getattr(reply_to, "reply_to_top_id", None) or getattr(reply_to, "reply_to_msg_id", None)
        if top:
            return int(top)
    if subscription.source_kind == "topic":
        return subscription.thread_id or None
    if subscription.source_kind == "comments" and subscription.thread_id:
        return subscription.thread_id
    return None


def normalize(msg: Any, subscription: Subscription) -> Message:
    """Convert Telethon message → persisted Message row."""
    media_type, doc_id, duration = detect_media(msg)
    sender_id, sender_name = _sender_display(msg)
    thread_id = _thread_id_for(msg, subscription)
    date = getattr(msg, "date", None) or datetime.now(UTC)
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    reply_to_obj = getattr(msg, "reply_to", None)
    reply_to_id = None
    if reply_to_obj is not None:
        reply_to_id = getattr(reply_to_obj, "reply_to_msg_id", None)
    return Message(
        chat_id=int(subscription.chat_id),
        msg_id=int(msg.id),
        thread_id=thread_id,
        date=date,
        sender_id=sender_id,
        sender_name=sender_name,
        text=getattr(msg, "message", None) or getattr(msg, "text", None),
        reply_to=reply_to_id,
        forward_from=_forward_str(getattr(msg, "fwd_from", None)),
        media_type=media_type,  # type: ignore[arg-type]
        media_doc_id=doc_id,
        media_duration=duration,
    )


# --------------------------------------------------------------- start points


def determine_start(sub: Subscription) -> dict[str, Any]:
    """Choose iter_messages() kwargs for the FIRST sync of a subscription (§7.2)."""
    settings = get_settings()
    if sub.start_from_msg_id is not None and sub.start_from_msg_id > 0:
        return {"min_id": sub.start_from_msg_id - 1}
    if sub.start_from_date is not None:
        return {"offset_date": sub.start_from_date, "reverse": True}
    lookback = datetime.now(UTC) - timedelta(days=settings.sync.default_lookback_days)
    return {"offset_date": lookback, "reverse": True}


# ----------------------------------------------------------------- iteration


@retry_on_flood()
async def _fetch_top_id(client: TelegramClient, chat_id: int) -> int | None:
    async for m in client.iter_messages(chat_id, limit=1):
        return int(m.id)
    return None


async def sync_subscription(
    client: TelegramClient,
    repo: Repo,
    sub: Subscription,
    *,
    dry_run: bool = False,
) -> int:
    """Incrementally fetch new messages for one subscription. Returns count."""
    settings = get_settings()
    state = await repo.get_sync_state(sub.chat_id, sub.thread_id)

    iter_kwargs: dict[str, Any] = {"entity": sub.chat_id, "reverse": True}
    if sub.thread_id and sub.thread_id != 0:
        iter_kwargs["reply_to"] = sub.thread_id

    if state and state.last_msg_id:
        iter_kwargs["min_id"] = state.last_msg_id
    # --last hint encoded as negative start_from_msg_id
    elif sub.start_from_msg_id is not None and sub.start_from_msg_id < 0:
        last = -sub.start_from_msg_id
        top = await _fetch_top_id(client, sub.chat_id)
        if top is not None:
            iter_kwargs["min_id"] = max(0, top - last) - 1
        else:
            iter_kwargs.update(determine_start(sub))
    else:
        iter_kwargs.update(determine_start(sub))

    limiter = RateLimiter(settings.telegram.max_msgs_per_minute)
    batch: list[Message] = []
    added = 0
    batch_size = settings.sync.batch_size

    @retry_on_flood()
    async def _run() -> int:
        nonlocal added
        count = 0
        async for msg in client.iter_messages(**iter_kwargs):  # type: ignore[arg-type]
            await limiter.acquire()
            nmsg = normalize(msg, sub)
            batch.append(nmsg)
            if len(batch) >= batch_size:
                if not dry_run:
                    await repo.upsert_messages(batch)
                    await repo.update_sync_state(
                        sub.chat_id, sub.thread_id, max(m.msg_id for m in batch)
                    )
                count += len(batch)
                batch.clear()
                await asyncio.sleep(0.1)
        if batch:
            if not dry_run:
                await repo.upsert_messages(batch)
                await repo.update_sync_state(
                    sub.chat_id, sub.thread_id, max(m.msg_id for m in batch)
                )
            count += len(batch)
            batch.clear()
        return count

    added = await _run()
    log.info(
        "sync.done",
        chat_id=sub.chat_id,
        thread_id=sub.thread_id,
        added=added,
        dry_run=dry_run,
    )
    return added


# ---------------------------------------------------------------- backfill


@retry_on_flood()
async def backfill(
    client: TelegramClient,
    repo: Repo,
    *,
    chat_id: int,
    from_msg_id: int,
    direction: str = "back",
) -> int:
    """One-shot history backfill starting from a specific message.

    direction=back → older messages (reverse=False, offset_id=from_msg_id).
    direction=forward → newer messages (reverse=True, min_id=from_msg_id-1).
    """
    # Ensure we have a base subscription to attribute messages to.
    sub = await repo.get_subscription(chat_id, 0)
    if sub is None:
        sub = Subscription(chat_id=chat_id, thread_id=0, title=None, source_kind="chat")

    iter_kwargs: dict[str, Any] = {"entity": chat_id}
    if direction == "forward":
        iter_kwargs["reverse"] = True
        iter_kwargs["min_id"] = max(from_msg_id - 1, 0)
    else:
        iter_kwargs["reverse"] = False
        iter_kwargs["offset_id"] = from_msg_id

    settings = get_settings()
    limiter = RateLimiter(settings.telegram.max_msgs_per_minute)
    batch: list[Message] = []
    total = 0
    async for msg in client.iter_messages(**iter_kwargs):  # type: ignore[arg-type]
        await limiter.acquire()
        batch.append(normalize(msg, sub))
        if len(batch) >= settings.sync.batch_size:
            await repo.upsert_messages(batch)
            total += len(batch)
            batch.clear()
            await asyncio.sleep(0.1)
    if batch:
        await repo.upsert_messages(batch)
        total += len(batch)
    return total
