"""Forum topics (spec §7.6) and channel discussion resolution (§7.7)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from analyzetg.util.logging import get_logger

if TYPE_CHECKING:
    from telethon import TelegramClient

log = get_logger(__name__)


@dataclass(slots=True)
class ForumTopic:
    topic_id: int
    title: str
    icon_emoji: str | None = None
    top_message: int | None = None
    closed: bool = False
    pinned: bool = False
    unread_count: int = 0
    read_inbox_max_id: int = 0


async def list_forum_topics(client: TelegramClient, chat_id: int) -> list[ForumTopic]:
    """Paginate GetForumTopicsRequest until exhausted."""
    from datetime import datetime

    # Lives under `messages` in Telethon ≥ 1.36 (was moved from `channels`).
    from telethon.tl.functions.messages import GetForumTopicsRequest  # type: ignore[attr-defined]

    peer = await client.get_input_entity(chat_id)
    out: list[ForumTopic] = []
    offset_date: datetime | None = None
    offset_id = 0
    offset_topic = 0
    seen_ids: set[int] = set()
    # Hard cap to guarantee termination even if the server returns the same page
    # indefinitely or the advance-offsets never progress.
    for _ in range(200):
        resp = await client(
            GetForumTopicsRequest(
                peer=peer,
                offset_date=offset_date,
                offset_id=offset_id,
                offset_topic=offset_topic,
                limit=100,
            )
        )
        batch_topics = getattr(resp, "topics", []) or []
        if not batch_topics:
            break
        new_in_batch = 0
        for t in batch_topics:
            if t.id in seen_ids:
                continue
            seen_ids.add(t.id)
            new_in_batch += 1
            out.append(
                ForumTopic(
                    topic_id=t.id,
                    title=getattr(t, "title", ""),
                    icon_emoji=getattr(t, "icon_emoji_id", None),
                    top_message=getattr(t, "top_message", None),
                    closed=bool(getattr(t, "closed", False)),
                    pinned=bool(getattr(t, "pinned", False)),
                    unread_count=int(getattr(t, "unread_count", 0) or 0),
                    read_inbox_max_id=int(getattr(t, "read_inbox_max_id", 0) or 0),
                )
            )
        if len(batch_topics) < 100 or new_in_batch == 0:
            break
        last = batch_topics[-1]
        offset_id = getattr(last, "top_message", 0) or 0
        offset_topic = getattr(last, "id", 0) or 0
        msgs = getattr(resp, "messages", []) or []
        if msgs and getattr(msgs[-1], "date", None):
            offset_date = msgs[-1].date
    else:
        log.warning("topics.pagination_hit_safety_cap", collected=len(out))
    return out


async def get_linked_chat_id(client: TelegramClient, chat_id: int) -> int | None:
    """Return the discussion-group chat_id for a channel (spec §7.7)."""
    from telethon.tl.functions.channels import GetFullChannelRequest  # type: ignore[attr-defined]

    channel = await client.get_input_entity(chat_id)
    full = await client(GetFullChannelRequest(channel=channel))
    linked = getattr(full.full_chat, "linked_chat_id", None)
    if not linked:
        return None
    # linked_chat_id from full is a plain internal id; channels get -100 prefix.
    return int(f"-100{linked}")


async def get_full_channel_info(client: TelegramClient, chat_id: int) -> dict:
    """Rich metadata for a channel/supergroup/forum: participants, admins,
    online count, slowmode, pinned msg, invite link, linked discussion."""
    from telethon.tl.functions.channels import GetFullChannelRequest  # type: ignore[attr-defined]

    channel_entity = await client.get_input_entity(chat_id)
    full = await client(GetFullChannelRequest(channel=channel_entity))
    fc = full.full_chat

    invite_link: str | None = None
    exported = getattr(fc, "exported_invite", None)
    if exported is not None:
        invite_link = getattr(exported, "link", None)

    # The Channel object (from the chats array) carries per-channel flags.
    ch = None
    chats = getattr(full, "chats", None) or []
    for c in chats:
        if getattr(c, "id", None) is not None and int(c.id) == abs(chat_id) - 1_000_000_000_000:
            ch = c
            break
    if ch is None and chats:
        ch = chats[0]

    username: str | None = None
    if ch is not None:
        username = getattr(ch, "username", None)
        # If no @username, try usernames[] (multi-username channels).
        if not username:
            usernames = getattr(ch, "usernames", None) or []
            if usernames:
                username = getattr(usernames[0], "username", None)

    return {
        "participants_count": getattr(fc, "participants_count", None),
        "admins_count": getattr(fc, "admins_count", None),
        "kicked_count": getattr(fc, "kicked_count", None),
        "banned_count": getattr(fc, "banned_count", None),
        "online_count": getattr(fc, "online_count", None),
        "linked_chat_id": (int(f"-100{fc.linked_chat_id}") if getattr(fc, "linked_chat_id", None) else None),
        "about": getattr(fc, "about", None),
        "slowmode_seconds": getattr(fc, "slowmode_seconds", None),
        "pinned_msg_id": getattr(fc, "pinned_msg_id", None),
        "invite_link": invite_link,
        "username": username,
        "broadcast": bool(getattr(ch, "broadcast", False)) if ch else None,
        "megagroup": bool(getattr(ch, "megagroup", False)) if ch else None,
        "forum": bool(getattr(ch, "forum", False)) if ch else None,
        "restricted": bool(getattr(ch, "restricted", False)) if ch else None,
        "scam": bool(getattr(ch, "scam", False)) if ch else None,
        "verified": bool(getattr(ch, "verified", False)) if ch else None,
    }
