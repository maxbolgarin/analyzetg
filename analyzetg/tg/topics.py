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


async def list_forum_topics(client: TelegramClient, chat_id: int) -> list[ForumTopic]:
    """Paginate GetForumTopicsRequest until exhausted."""
    from telethon.tl.functions.channels import GetForumTopicsRequest  # type: ignore[attr-defined]

    channel = await client.get_input_entity(chat_id)
    out: list[ForumTopic] = []
    offset_date = 0
    offset_id = 0
    offset_topic = 0
    seen_ids: set[int] = set()
    # Hard cap to guarantee termination even if the server returns the same page
    # indefinitely or the advance-offsets never progress.
    for _ in range(200):
        resp = await client(
            GetForumTopicsRequest(
                channel=channel,
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
                )
            )
        if len(batch_topics) < 100 or new_in_batch == 0:
            break
        last = batch_topics[-1]
        offset_id = getattr(last, "top_message", 0) or 0
        offset_topic = getattr(last, "id", 0) or 0
        msgs = getattr(resp, "messages", []) or []
        if msgs and getattr(msgs[-1], "date", None):
            offset_date = int(msgs[-1].date.timestamp())
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
    """Collect subscriber count + linked chat id for `channel-info`."""
    from telethon.tl.functions.channels import GetFullChannelRequest  # type: ignore[attr-defined]

    channel = await client.get_input_entity(chat_id)
    full = await client(GetFullChannelRequest(channel=channel))
    return {
        "participants_count": getattr(full.full_chat, "participants_count", None),
        "linked_chat_id": (
            int(f"-100{full.full_chat.linked_chat_id}")
            if getattr(full.full_chat, "linked_chat_id", None)
            else None
        ),
        "about": getattr(full.full_chat, "about", None),
    }
