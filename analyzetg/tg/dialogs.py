"""Dialog-level helpers: read-inbox marker and unread iteration.

Telethon's `Dialog` exposes `unread_count` and `read_inbox_max_id` directly
on the high-level dialog object; we expose thin async wrappers so callers
don't have to loop over `iter_dialogs()` themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from analyzetg.tg.client import (
    _chat_kind,
    entity_id,
    entity_title,
    entity_username,
)

if TYPE_CHECKING:
    from telethon import TelegramClient


@dataclass(slots=True)
class UnreadDialog:
    chat_id: int
    kind: str
    title: str | None
    username: str | None
    unread_count: int
    read_inbox_max_id: int
    last_msg_date: datetime | None = None


async def get_unread_state(client: TelegramClient, chat_id: int) -> tuple[int, int]:
    """Return `(unread_count, read_inbox_max_id)` for one chat.

    Uses `GetPeerDialogsRequest` for a direct lookup (no full dialog scan).
    Returns `(0, 0)` if the peer has no dialog entry (e.g. a channel you
    never opened in your client).
    """
    from telethon.tl.functions.messages import (  # type: ignore[attr-defined]
        GetPeerDialogsRequest,
    )

    entity = await client.get_input_entity(chat_id)
    result = await client(GetPeerDialogsRequest(peers=[entity]))
    dialogs = getattr(result, "dialogs", None) or []
    if not dialogs:
        return 0, 0
    d = dialogs[0]
    return int(getattr(d, "unread_count", 0) or 0), int(getattr(d, "read_inbox_max_id", 0) or 0)


async def mark_as_read(
    client: TelegramClient, chat_id: int, max_msg_id: int, thread_id: int | None = None
) -> bool:
    """Advance Telegram's read marker for `chat_id` up to `max_msg_id` inclusive.

    Returns True on success. Forum-topic read markers (`thread_id != None`)
    aren't supported by Telethon's high-level helper — returns False with a
    log entry rather than raising.
    """
    from analyzetg.util.logging import get_logger

    log = get_logger(__name__)
    if thread_id:
        log.warning("mark_read.topic_unsupported", chat_id=chat_id, thread_id=thread_id)
        return False
    try:
        await client.send_read_acknowledge(chat_id, max_id=max_msg_id)
        return True
    except Exception as e:
        log.error("mark_read.error", chat_id=chat_id, err=str(e)[:200])
        return False


async def list_unread_dialogs(client: TelegramClient) -> list[UnreadDialog]:
    """All dialogs with `unread_count > 0`, sorted by unread_count descending.

    Fills `last_msg_date` from the dialog's top-message date (free — it's in
    the response already). First-unread-message date requires a per-chat
    `get_messages` call; interactive callers do that lazily.
    """
    out: list[UnreadDialog] = []
    async for d in client.iter_dialogs(limit=None):  # type: ignore[arg-type]
        count = int(getattr(d, "unread_count", 0) or 0)
        if count <= 0:
            continue
        entity = d.entity
        # Dialog.date == top message's date; fall back via .message if needed.
        last_date: datetime | None = getattr(d, "date", None)
        if last_date is None:
            msg = getattr(d, "message", None)
            if msg is not None:
                last_date = getattr(msg, "date", None)
        out.append(
            UnreadDialog(
                chat_id=entity_id(entity),
                kind=_chat_kind(entity),
                title=entity_title(entity),
                username=entity_username(entity),
                unread_count=count,
                read_inbox_max_id=int(getattr(d, "read_inbox_max_id", 0) or 0),
                last_msg_date=last_date,
            )
        )
    out.sort(key=lambda d: (-d.unread_count, (d.title or "").lower()))
    return out
