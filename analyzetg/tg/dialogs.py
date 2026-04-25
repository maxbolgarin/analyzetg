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

    - Regular chat: uses Telethon's high-level `send_read_acknowledge`.
    - Forum topic (`thread_id` set): uses the raw `messages.ReadDiscussionRequest`
      which is the same mechanism Telegram uses for both forum topics and
      channel-comment threads. `thread_id` goes into `msg_id` (the topic's
      anchor message), `read_max_id` is the highest message now read.

    Returns True on success, False (with a log entry) on any failure.
    """
    from analyzetg.util.logging import get_logger

    log = get_logger(__name__)
    if thread_id:
        try:
            from telethon.tl.functions.messages import (  # type: ignore[attr-defined]
                ReadDiscussionRequest,
            )

            entity = await client.get_input_entity(chat_id)
            await client(
                ReadDiscussionRequest(
                    peer=entity,
                    msg_id=int(thread_id),
                    read_max_id=int(max_msg_id),
                )
            )
            return True
        except Exception as e:
            log.error(
                "mark_read.topic_error",
                chat_id=chat_id,
                thread_id=thread_id,
                err=str(e)[:200],
            )
            return False
    try:
        await client.send_read_acknowledge(chat_id, max_id=max_msg_id)
        return True
    except Exception as e:
        log.error("mark_read.error", chat_id=chat_id, err=str(e)[:200])
        return False


async def correct_forum_unread(
    client: TelegramClient,
    dialogs: list[UnreadDialog],
    *,
    concurrency: int = 5,
) -> None:
    """Replace unreliable dialog-level forum unread counts with per-topic sums.

    Mutates each candidate `UnreadDialog` in place: sets `unread_count` to
    the sum of per-topic `unread_count` from `GetForumTopicsRequest` and,
    when a misclassified supergroup turns out to be a forum, upgrades
    `kind="forum"`.

    Telegram's dialog-level unread for forums is unreliable in both
    directions:
    - **Too high:** counter is capped at 99,999 and isn't always
      decremented when topics are read elsewhere — you can see 24k or 99k
      on a forum that actually has < 100 unread per topic.
    - **Too low (zero):** after a partial mark-read, the dialog count can
      sit at 0 while per-topic counters are still non-zero.
    - **Misclassified:** Telethon sometimes returns a Channel entity
      without the `forum` flag set (stale cache), so `_chat_kind` calls
      it `supergroup` even when it's a forum.

    We probe every `kind='forum'` AND every `kind='supergroup'` (the
    cache-stale case). Supergroups that aren't forums raise from
    `GetForumTopicsRequest`; we silently swallow those — their count
    stays whatever the dialog said.

    Cost: one `GetForumTopicsRequest` per probe target, `concurrency`-way
    parallel. Typically 20–50 RPCs total when called over a full
    `iter_dialogs` snapshot, completing in ~100–500 ms.
    """
    import asyncio as _asyncio

    targets = [d for d in dialogs if d.kind in {"forum", "supergroup"}]
    if not targets:
        return

    from analyzetg.tg.topics import list_forum_topics
    from analyzetg.util.logging import get_logger as _get_logger

    log = _get_logger(__name__)
    sem = _asyncio.Semaphore(concurrency)

    async def _probe(d: UnreadDialog) -> None:
        async with sem:
            try:
                topics = await list_forum_topics(client, d.chat_id)
            except Exception as e:
                # Non-forum supergroup: GetForumTopicsRequest rejects the peer.
                # Expected for the probe path. Only WARN for kind='forum',
                # where a failure is actually surprising.
                if d.kind == "forum":
                    log.warning(
                        "correct_forum_unread.probe_failed",
                        chat_id=d.chat_id,
                        err=str(e)[:200],
                    )
                return
            d.unread_count = sum(t.unread_count for t in topics)
            if d.kind == "supergroup" and topics:
                d.kind = "forum"

    await _asyncio.gather(*[_probe(d) for d in targets])


async def list_unread_dialogs(client: TelegramClient) -> list[UnreadDialog]:
    """All dialogs with unread messages, sorted by unread_count descending.

    Non-forum chats: the dialog-level `unread_count` is authoritative and
    cheap to read — skip anything with zero.

    Forums: the dialog-level count is **unreliable in both directions**.
    Telegram caps it at 99,999 and doesn't always decrement when topics
    are read, so it can be *too high*; conversely, after a mark-read run
    that only advances the dialog marker, it can sit at **0** even when
    per-topic counts are non-zero (new messages arrived after the
    mark-read, Telegram hasn't re-aggregated). The only reliable source
    is summing per-topic `unread_count` from `GetForumTopicsRequest`.

    Complication: `entity.forum` as exposed by `iter_dialogs` is
    **unreliable** — Telethon sometimes receives a Channel entity
    without the forum flag set (server-side inconsistency, stale entity
    cache), so `_chat_kind` returns `"supergroup"` for what is actually
    a forum. To handle both cases, we probe every supergroup-with-zero
    **and** every forum-with-zero for real per-topic unreads. Supergroups
    that aren't forums raise a Telegram error on `GetForumTopicsRequest`,
    which we quietly swallow — they stay at count=0 and get dropped by
    the final filter.

    Cost: roughly one `GetForumTopicsRequest` per (forum + supergroup)
    in your dialog list, 5-way parallel. Users with many read
    supergroups pay a one-time latency hit (~100-300ms total) when
    opening the picker.
    """
    out: list[UnreadDialog] = []
    async for d in client.iter_dialogs(limit=None):  # type: ignore[arg-type]
        entity = d.entity
        kind = _chat_kind(entity)
        count = int(getattr(d, "unread_count", 0) or 0)
        # Forums or megagroups ("supergroup" in our taxonomy): include even
        # with dialog-level count 0 — Telegram's forum unread state is
        # per-topic and _chat_kind can misclassify a forum as supergroup
        # when `entity.forum` is stale. Non-forum/megagroup: trust the
        # dialog count.
        could_be_forum = kind in {"forum", "supergroup"}
        if not could_be_forum and count <= 0:
            continue
        last_date: datetime | None = getattr(d, "date", None)
        if last_date is None:
            msg = getattr(d, "message", None)
            if msg is not None:
                last_date = getattr(msg, "date", None)
        out.append(
            UnreadDialog(
                chat_id=entity_id(entity),
                kind=kind,
                title=entity_title(entity),
                username=entity_username(entity),
                unread_count=count,
                read_inbox_max_id=int(getattr(d, "read_inbox_max_id", 0) or 0),
                last_msg_date=last_date,
            )
        )

    # Fix unreliable dialog-level forum counts (capped at 99k, not always
    # decremented, sometimes misclassified as supergroup).
    await correct_forum_unread(client, out)

    # Drop any dialog whose real count is 0 (forums that looked unread at
    # the dialog level but were empty per-topic, supergroups that turned
    # out not to be forums, and vice-versa).
    out = [d for d in out if d.unread_count > 0]
    out.sort(key=lambda d: (-d.unread_count, (d.title or "").lower()))
    return out
