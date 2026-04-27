"""Telethon client wrapper and helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from telethon import TelegramClient

from unread.config import Settings, get_settings
from unread.util.logging import get_logger

log = get_logger(__name__)


def _chat_kind(entity) -> str:
    """Classify a Telethon entity."""
    # Imported lazily so tests can import this module without Telethon types at top.
    from telethon.tl.types import Channel, Chat, User  # type: ignore[attr-defined]

    if isinstance(entity, User):
        return "user"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        if getattr(entity, "forum", False):
            return "forum"
        if getattr(entity, "megagroup", False):
            return "supergroup"
        return "channel"
    return "user"


def entity_title(entity) -> str | None:
    """Best-effort display title for any entity kind."""
    title = getattr(entity, "title", None)
    if title:
        return title
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    full = f"{first} {last}".strip()
    if full:
        return full
    uname = getattr(entity, "username", None)
    return f"@{uname}" if uname else None


def entity_username(entity) -> str | None:
    return getattr(entity, "username", None)


def entity_id(entity) -> int:
    """Return the canonical chat_id, including -100 prefix for channels."""
    from telethon.utils import get_peer_id  # type: ignore[attr-defined]

    return get_peer_id(entity)


def build_client(settings: Settings | None = None) -> TelegramClient:
    s = settings or get_settings()
    s.telegram.session_path.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        str(s.telegram.session_path),
        api_id=s.telegram.api_id,
        api_hash=s.telegram.api_hash,
    )


@asynccontextmanager
async def tg_client(
    settings: Settings | None = None, require_auth: bool = True
) -> AsyncIterator[TelegramClient]:
    """Async context manager that connects and, optionally, enforces auth."""
    client = build_client(settings)
    await client.connect()
    try:
        if require_auth and not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized. Run `unread init` first.")
        yield client
    finally:
        await client.disconnect()
