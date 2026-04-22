"""Dialog-level helpers: read-inbox marker and unread iteration.

Telethon's `Dialog` exposes `unread_count` and `read_inbox_max_id` directly
on the high-level dialog object; we expose thin async wrappers so callers
don't have to loop over `iter_dialogs()` themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
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


async def list_unread_dialogs(client: TelegramClient) -> list[UnreadDialog]:
    """All dialogs with `unread_count > 0`, ordered as Telegram returns them."""
    out: list[UnreadDialog] = []
    async for d in client.iter_dialogs(limit=None):  # type: ignore[arg-type]
        count = int(getattr(d, "unread_count", 0) or 0)
        if count <= 0:
            continue
        entity = d.entity
        out.append(
            UnreadDialog(
                chat_id=entity_id(entity),
                kind=_chat_kind(entity),
                title=entity_title(entity),
                username=entity_username(entity),
                unread_count=count,
                read_inbox_max_id=int(getattr(d, "read_inbox_max_id", 0) or 0),
            )
        )
    return out
