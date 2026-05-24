"""Per-chat burst collector.

When the user pastes several links / drops several files in quick
succession, we don't want to ask `[▶ Run]` once per message. Instead,
each incoming analysis-shaped event is appended to a per-chat burst
and a short debounce timer is (re)started. When the quiet window
elapses, one consolidated panel is sent: `▶ Run separately` /
`▶ Run combined`. The user taps once and gets either N reports (one
per source) or a single merged report.

Only the analysis kinds — file / url / youtube / tg — bucket here.
Slash commands and session uploads keep their instant-reply path in
`app._handle`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from unread.bot.app import BotApp

log = structlog.get_logger(__name__)


# How long to wait after the last burst-eligible message before flushing.
# Short enough that a single message feels prompt; long enough to catch
# a copy-paste of 3-10 URLs typed within a couple of seconds.
DEFAULT_DEBOUNCE_SECONDS = 2.5


@dataclass
class BurstItem:
    """One classified-but-not-yet-confirmed message inside a burst."""

    kind: str
    payload: dict
    event: Any  # Telethon NewMessage.Event — kept so the run path can reply.
    arrived_at: float = field(default_factory=time.time)


@dataclass
class BurstState:
    """Mutable per-chat accumulator. Lives on `app._chat_state[chat_id]["burst"]`."""

    items: list[BurstItem] = field(default_factory=list)
    debounce_task: asyncio.Task | None = None


def _get_state(app: BotApp, chat_id: int) -> BurstState:
    chat_state = app._chat_state.setdefault(chat_id, {})
    state = chat_state.get("burst")
    if state is None:
        state = BurstState()
        chat_state["burst"] = state
    return state


async def add_to_burst(
    app: BotApp,
    event: Any,
    kind: str,
    payload: dict,
    *,
    debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
) -> None:
    """Append an item and (re)start the debounce timer for this chat.

    A new message while a previous debounce is still pending cancels
    that timer and starts a fresh one — the burst grows until the user
    stops sending. The flushed panel is sent in reply to the *last*
    message in the burst (most natural anchor).
    """
    state = _get_state(app, event.chat_id)
    state.items.append(BurstItem(kind=kind, payload=payload, event=event))

    if state.debounce_task is not None and not state.debounce_task.done():
        state.debounce_task.cancel()

    state.debounce_task = asyncio.create_task(_debounce_then_flush(app, event.chat_id, debounce_seconds))
    # Pin to the app's task set so a graceful shutdown awaits the flush
    # instead of losing the panel mid-burst.
    app._tasks.add(state.debounce_task)
    state.debounce_task.add_done_callback(app._tasks.discard)


async def _debounce_then_flush(app: BotApp, chat_id: int, debounce_seconds: float) -> None:
    """Sleep `debounce_seconds`, then flush. Cancellation is normal."""
    try:
        await asyncio.sleep(debounce_seconds)
    except asyncio.CancelledError:
        return
    try:
        await _flush_burst(app, chat_id)
    except Exception:
        log.exception("bot.burst.flush_failed", chat_id=chat_id)


async def _flush_burst(app: BotApp, chat_id: int) -> None:
    """Drain the chat's burst into one confirm panel.

    Reads & clears `state.items` atomically (asyncio is single-threaded
    within a chat, so this is safe without locks). If the burst was
    cancelled out from under us — items already drained, no items, or
    the chat state vanished — the call is a no-op.
    """
    from unread.bot.confirm import (
        PendingRun,
        RunOptions,
        build_batch_panel,
    )

    chat_state = app._chat_state.get(chat_id)
    if not chat_state:
        return
    state: BurstState | None = chat_state.get("burst")
    if state is None or not state.items:
        return
    items = list(state.items)
    state.items.clear()
    state.debounce_task = None

    last_event = items[-1].event
    # First send the panel with a placeholder ID so the buttons exist;
    # then edit with the real ID once Telethon returns the sent message.
    text, buttons = build_batch_panel(items=items, panel_msg_id=0)
    panel = await last_event.reply(text, buttons=buttons, parse_mode="md")
    text, buttons = build_batch_panel(items=items, panel_msg_id=panel.id)
    with contextlib.suppress(Exception):
        await panel.edit(text, buttons=buttons, parse_mode="md")

    pending_runs = chat_state.setdefault("pending_runs", {})
    pending_runs[panel.id] = PendingRun(
        kind="batch",
        payload={"items": items},
        options=RunOptions(),
        event=last_event,
    )


def summary_line(item: BurstItem) -> str:
    """One-line description for the panel's bullet list."""
    if item.kind == "file":
        if item.payload.get("source") == "text":
            return "📄 text message"
        return f"📄 {item.payload.get('name') or 'file'}"
    if item.kind == "url":
        return f"🌐 {item.payload.get('url', '')}"
    if item.kind == "youtube":
        return f"🎬 {item.payload.get('url', '')}"
    if item.kind == "tg":
        return f"💬 {item.payload.get('url', '')}"
    return f"? {item.kind}"


def combinable_items(items: list[BurstItem]) -> list[BurstItem]:
    """Items eligible for the `▶ Run combined` path.

    TG-link items need a Telethon user session and a per-chat backfill
    pass — too much work for the initial combined-mode implementation.
    They're filtered out here so the combined button can be hidden when
    a burst contains nothing else.
    """
    return [it for it in items if it.kind in ("file", "url", "youtube")]
