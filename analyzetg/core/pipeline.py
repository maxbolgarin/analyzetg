"""Shared preparation pipeline for chat runs.

Every `atg analyze` / `atg dump` / `atg download-media` invocation
eventually reaches `prepare_chat_run` here, which handles:

  - chat/thread ref resolution (already done by caller)
  - start-msg-id determination (unread vs from_msg vs full-history)
  - backfill (forward + optionally backward for full-history)
  - iter_messages + per-topic unread filter (flat-forum)
  - enrichment (voice → transcript, etc.)
  - mark-read closure (not fired here; consumer awaits when ready)

Returns a `PreparedRun` (see `core/run.py`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

import typer
from rich.console import Console

from analyzetg.core.run import PreparedRun

if TYPE_CHECKING:
    from analyzetg.enrich.base import EnrichOpts

console = Console()


async def _determine_start(
    *,
    client: Any,
    chat_id: int,
    thread_id: int,
    full_history: bool,
    from_msg_id: int | None,
    time_window: tuple[datetime | None, datetime | None],
) -> int | None:
    """Return a msg_id lower bound (exclusive) or None for time-window / full mode.

    Lifted verbatim from analyzer/commands.py and export/commands.py —
    same logic was duplicated in both. Semantics unchanged.
    """
    from analyzetg.tg.dialogs import get_unread_state

    if full_history:
        return None
    if from_msg_id is not None:
        return max(from_msg_id - 1, 0)
    if time_window[0] is not None or time_window[1] is not None:
        return None
    if thread_id:
        console.print(
            "[red]Per-topic unread isn't exposed by Telegram for arbitrary threads.[/]\n"
            "Pass [cyan]--last-days N[/], [cyan]--from-msg <id>[/], or [cyan]--full-history[/]."
        )
        raise typer.Exit(2)
    console.print("[dim]→ Reading unread marker...[/]")
    unread_count, read_marker = await get_unread_state(client, chat_id)
    if unread_count == 0:
        console.print(
            f"[yellow]No unread messages in chat {chat_id}.[/] "
            "Pass --last-days / --from-msg / --full-history to analyze anyway."
        )
        raise typer.Exit(0)
    console.print(f"[dim]→ {unread_count} unread message(s) after msg_id={read_marker}[/]")
    return read_marker


async def _pull_history(
    *,
    client: Any,
    repo: Any,
    chat_id: int,
    thread_id: int,
    start_msg_id: int | None,
    since_dt: datetime | None,
    full_history: bool = False,
) -> None:
    """Fetch new messages from Telegram, skipping what's already in the DB.

    Forward pass: catch up from local_max. For full_history, a second
    backward pass walks from local_min to msg_id=1 so "full history"
    actually means full history — not "history since last sync".

    Lifted verbatim from analyzer/commands.py. Same logic was
    duplicated in export/commands.py with identical semantics.
    """
    from analyzetg.tg.sync import backfill

    thread_param = thread_id if thread_id else None
    if start_msg_id is not None or (since_dt is None):
        floor = start_msg_id if start_msg_id is not None else 0
        local_max = await repo.get_max_msg_id(chat_id, thread_param, min_msg_id=floor)
        effective = max(floor, local_max or 0)
        if local_max and local_max > floor:
            console.print(f"[dim]→ Have up to msg_id={local_max} locally, fetching only newer[/]")
        await backfill(
            client,
            repo,
            chat_id=chat_id,
            thread_id=thread_param,
            from_msg_id=effective + 1,
            direction="forward",
        )
        if full_history and start_msg_id is None:
            local_min = await repo.get_min_msg_id(chat_id, thread_param)
            if local_min and local_min > 1:
                console.print(f"[dim]→ Have from msg_id={local_min} locally, fetching older history…[/]")
                await backfill(
                    client,
                    repo,
                    chat_id=chat_id,
                    thread_id=thread_param,
                    from_msg_id=local_min,
                    direction="back",
                )
            elif local_min is None:
                console.print("[dim]→ No local messages; fetching full chat history…[/]")
                await backfill(
                    client,
                    repo,
                    chat_id=chat_id,
                    thread_id=thread_param,
                    direction="back",
                )
    else:
        await backfill(
            client,
            repo,
            chat_id=chat_id,
            thread_id=thread_param,
            since_date=since_dt,
        )


def _build_mark_read_fn(
    *,
    client: Any,
    repo: Any,
    chat_id: int,
    thread_id: int | None,
    topic_titles: dict[int, str] | None,
    enabled: bool,
) -> Callable[[], Awaitable[int]] | None:
    """Return a coroutine that advances the right Telegram read marker.

    Three shapes in one place (no consumer-side branching needed):

      1. Flat-forum (topic_titles populated, thread_id is None):
         loop over topics, mark each topic read up to its local max.
      2. Single-topic (thread_id > 0): one ReadDiscussionRequest.
      3. Non-forum (thread_id falsy, no topic_titles): one
         send_read_acknowledge.

    Returns None when `enabled` is False — consumers gate with
    `if prepared.mark_read_fn: await prepared.mark_read_fn()`.
    """
    if not enabled:
        return None

    from analyzetg.tg.dialogs import mark_as_read
    from analyzetg.util.logging import get_logger

    log = get_logger(__name__)

    async def _mark_flat_forum() -> int:
        marked = 0
        if not topic_titles:
            return 0
        for tid, tname in topic_titles.items():
            latest = await repo.get_max_msg_id(chat_id, thread_id=tid)
            if not latest:
                continue
            if await mark_as_read(client, chat_id, latest, thread_id=tid):
                marked += 1
                log.debug(
                    "mark_read.topic",
                    chat_id=chat_id,
                    thread_id=tid,
                    name=tname,
                    max_id=latest,
                )
        console.print(f"[dim]→ Marked read across {marked}/{len(topic_titles)} topics[/]")
        return marked

    async def _mark_single() -> int:
        latest = await repo.get_max_msg_id(chat_id, thread_id if thread_id else None)
        if not latest:
            return 0
        ok = await mark_as_read(client, chat_id, latest, thread_id=thread_id)
        if ok:
            console.print(f"[dim]→ Marked read up to msg_id={latest}[/]")
            return 1
        return 0

    if topic_titles:
        return _mark_flat_forum
    return _mark_single


async def prepare_chat_run(
    *,
    client: Any,
    repo: Any,
    settings: Any,
    chat_id: int,
    thread_id: int | None,
    chat_title: str | None,
    enrich_opts: EnrichOpts,
    thread_title: str | None = None,
    chat_username: str | None = None,
    chat_internal_id: int | None = None,
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
    from_msg_id: int | None = None,
    full_history: bool = False,
    include_transcripts: bool = True,
    min_msg_chars: int | None = None,
    topic_titles: dict[int, str] | None = None,
    topic_markers: dict[int, int] | None = None,
    mark_read: bool = False,
) -> PreparedRun:
    """Prepare a single chat run: resolve → backfill → enrich → ready for consumer.

    Consumer (analyze / dump / download-media) then does its specific
    work with `prepared.messages` and awaits `prepared.mark_read_fn()`
    on success.

    `topic_titles` / `topic_markers` are the flat-forum knobs — caller
    precomputes them with `list_forum_topics`. For non-forum or
    single-topic, both stay None.
    """
    from analyzetg.analyzer.filters import FilterOpts, dedupe, filter_messages
    from analyzetg.enrich.pipeline import enrich_messages

    start_msg_id = await _determine_start(
        client=client,
        chat_id=chat_id,
        thread_id=thread_id if thread_id else 0,
        full_history=full_history,
        from_msg_id=from_msg_id,
        time_window=(since_dt, until_dt),
    )

    console.print("[dim]→ Fetching new messages from Telegram...[/]")
    await _pull_history(
        client=client,
        repo=repo,
        chat_id=chat_id,
        thread_id=thread_id if thread_id else 0,
        start_msg_id=start_msg_id,
        since_dt=since_dt,
        full_history=full_history,
    )

    msgs = await repo.iter_messages(
        chat_id,
        thread_id=thread_id,
        since=since_dt,
        until=until_dt,
        min_msg_id=start_msg_id if start_msg_id and start_msg_id > 0 else None,
    )

    # Per-topic unread filter (flat-forum only). Mirrors
    # analyzer/pipeline.py:run_analysis exactly.
    if topic_markers:
        before = len(msgs)
        msgs = [
            m
            for m in msgs
            if m.thread_id is None
            or m.thread_id not in topic_markers
            or m.msg_id > topic_markers[m.thread_id]
        ]
        if before != len(msgs):
            console.print(f"[dim]→ Filtered per-topic: kept {len(msgs)} / dropped {before - len(msgs)}[/]")

    raw_count = len(msgs)

    enrich_stats = None
    if enrich_opts.any_enabled() and msgs:
        enrich_stats = await enrich_messages(msgs, client=client, repo=repo, opts=enrich_opts)
        summary = enrich_stats.summary()
        if summary:
            console.print(f"[dim]→ {summary}[/]")

    f_opts = FilterOpts(
        min_msg_chars=min_msg_chars if min_msg_chars is not None else settings.analyze.min_msg_chars,
        include_transcripts=include_transcripts,
        text_only=not include_transcripts,
    )
    msgs = filter_messages(msgs, f_opts)
    if settings.analyze.dedupe_forwards:
        msgs = dedupe(msgs)

    mark_read_fn = _build_mark_read_fn(
        client=client,
        repo=repo,
        chat_id=chat_id,
        thread_id=thread_id,
        topic_titles=topic_titles,
        enabled=mark_read,
    )

    return PreparedRun(
        chat_id=chat_id,
        thread_id=thread_id,
        chat_title=chat_title,
        thread_title=thread_title,
        chat_username=chat_username,
        chat_internal_id=chat_internal_id,
        messages=msgs,
        period=(since_dt, until_dt),
        topic_titles=topic_titles,
        topic_markers=topic_markers,
        raw_msg_count=raw_count,
        enrich_stats=enrich_stats,
        mark_read_fn=mark_read_fn,
        client=client,
        repo=repo,
        settings=settings,
    )
