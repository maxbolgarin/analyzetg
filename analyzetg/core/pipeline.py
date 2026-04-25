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
    force_from_start: bool = False,
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
        local_max = (
            None if force_from_start else await repo.get_max_msg_id(chat_id, thread_param, min_msg_id=floor)
        )
        effective = floor if force_from_start else max(floor, local_max or 0)
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
    messages: list[Any],
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
    latest_msg_id = max((int(m.msg_id) for m in messages), default=0)
    latest_by_topic: dict[int, int] = {}
    for m in messages:
        if m.thread_id is not None:
            latest_by_topic[int(m.thread_id)] = max(latest_by_topic.get(int(m.thread_id), 0), int(m.msg_id))

    async def _mark_flat_forum() -> int:
        marked = 0
        if not topic_titles:
            return 0
        for tid, tname in topic_titles.items():
            latest = latest_by_topic.get(tid, 0)
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
        latest = latest_msg_id
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


async def _pull_linked_comments(
    *,
    client: Any,
    repo: Any,
    chat_id: int,
    primary_msgs: list[Any],
    since_dt: datetime | None,
    until_dt: datetime | None,
) -> tuple[dict[str, Any], list[Any]]:
    """Resolve `linked_chat_id`, backfill its date window, return (meta, msgs).

    Date window for the linked chat:
      - if explicit `since_dt`/`until_dt` were given, reuse them;
      - else span the primary chat's message range (min..max date) so
        comments come from the same period the channel posts cover;
      - else (no primary msgs and no window) skip — there's nothing to
        attach the comments to.

    On any failure (no linked chat, channel-info RPC error, backfill
    error) returns `({...None}, [])` so the caller proceeds without
    comments rather than aborting the analysis.
    """
    from analyzetg.tg.sync import backfill
    from analyzetg.tg.topics import get_linked_chat_id
    from analyzetg.util.logging import get_logger

    log = get_logger(__name__)
    null_meta: dict[str, Any] = {
        "chat_id": None,
        "title": None,
        "username": None,
        "internal_id": None,
    }

    chat_row = await repo.get_chat(chat_id)
    if not chat_row or chat_row.get("kind") != "channel":
        # Not a channel — comments don't apply. Silent no-op so generic
        # callers can pass `with_comments=True` defensively without
        # branching on chat kind.
        return null_meta, []

    linked_id = chat_row.get("linked_chat_id")
    if linked_id is None:
        try:
            linked_id = await get_linked_chat_id(client, chat_id)
        except Exception as e:
            log.warning("comments.linked_chat_lookup_failed", chat_id=chat_id, err=str(e)[:200])
            return null_meta, []
        if linked_id is None:
            console.print("[yellow]→ Channel has no linked discussion group; skipping comments.[/]")
            return null_meta, []
        # Persist the lookup so subsequent runs short-circuit.
        await repo.upsert_chat(
            chat_id,
            "channel",
            title=chat_row.get("title"),
            username=chat_row.get("username"),
            linked_chat_id=linked_id,
        )

    # Window for comments: explicit > derived from primary > skip.
    com_since = since_dt
    com_until = until_dt
    if com_since is None and com_until is None and primary_msgs:
        dates = [m.date for m in primary_msgs if m.date is not None]
        if dates:
            com_since = min(dates)
            com_until = max(dates)
    if com_since is None and com_until is None:
        return null_meta, []

    from analyzetg.core.paths import derive_internal_id

    # Resolve linked chat metadata (title/username) — try cached row,
    # fall back to Telethon get_entity for cosmetics. Never fatal.
    linked_row = await repo.get_chat(linked_id)
    title = (linked_row or {}).get("title")
    username = (linked_row or {}).get("username")
    if not title:
        try:
            from analyzetg.tg.client import entity_title, entity_username

            entity = await client.get_entity(linked_id)
            title = entity_title(entity)
            username = username or entity_username(entity)
            await repo.upsert_chat(
                linked_id,
                "supergroup",
                title=title,
                username=username,
            )
        except Exception as e:
            log.debug("comments.entity_lookup_failed", linked_id=linked_id, err=str(e)[:200])
            title = title or f"Comments {linked_id}"

    console.print(
        f"[dim]→ Including comments from linked chat[/] [bold]{title}[/] "
        f"({linked_id})[dim] for window {com_since} … {com_until}[/]"
    )

    # Backfill the linked chat for the comment window. Use since_date
    # when only since_dt is provided; otherwise rely on a forward pull
    # from local_max so we don't refetch what's already there.
    try:
        if com_since is not None:
            await backfill(
                client,
                repo,
                chat_id=linked_id,
                thread_id=None,
                since_date=com_since,
            )
        else:
            local_max = await repo.get_max_msg_id(linked_id, None) or 0
            await backfill(
                client,
                repo,
                chat_id=linked_id,
                thread_id=None,
                from_msg_id=local_max + 1,
                direction="forward",
            )
    except Exception as e:
        log.warning("comments.backfill_failed", linked_id=linked_id, err=str(e)[:200])
        # Fall through — we can still surface whatever's in the local DB.

    comments_msgs = await repo.iter_messages(
        linked_id,
        thread_id=None,
        since=com_since,
        until=com_until,
    )
    if not comments_msgs:
        console.print("[dim]→ No comments in window.[/]")

    meta: dict[str, Any] = {
        "chat_id": linked_id,
        "title": title,
        "username": username,
        "internal_id": derive_internal_id(linked_id),
    }
    return meta, comments_msgs


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
    skip_filter: bool = False,
    with_comments: bool = False,
) -> PreparedRun:
    """Prepare a single chat run: resolve → backfill → enrich → ready for consumer.

    Consumer (analyze / dump / download-media) then does its specific
    work with `prepared.messages` and awaits `prepared.mark_read_fn()`
    on success.

    `topic_titles` / `topic_markers` are the flat-forum knobs — caller
    precomputes them with `list_forum_topics`. For non-forum or
    single-topic, both stay None.

    `skip_filter=True` bypasses filter_messages + dedupe. download-media
    uses it: it wants raw media-only messages, which the text-filter
    (min_msg_chars, empty effective_text) would otherwise drop.
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
        force_from_start=bool(topic_markers),
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

    # Channel + comments: pull the linked discussion group's messages
    # from the same date range and merge them in BEFORE enrichment, so
    # the combined list goes through one enrichment pass and one filter
    # pass with identical opts. Each row keeps its original chat_id;
    # downstream formatter renders them as two sections via chat_groups.
    comments_meta: dict[str, Any] = {
        "chat_id": None,
        "title": None,
        "username": None,
        "internal_id": None,
    }
    if with_comments and msgs is not None:
        comments_meta, comments_msgs = await _pull_linked_comments(
            client=client,
            repo=repo,
            chat_id=chat_id,
            primary_msgs=msgs,
            since_dt=since_dt,
            until_dt=until_dt,
        )
        if comments_msgs:
            msgs = msgs + comments_msgs

    raw_count = len(msgs)

    enrich_stats = None
    if enrich_opts.any_enabled() and msgs:
        enrich_stats = await enrich_messages(msgs, client=client, repo=repo, opts=enrich_opts)
        summary = enrich_stats.summary()
        if summary:
            console.print(f"[dim]→ {summary}[/]")

    if not skip_filter:
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
        messages=msgs,
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
        comments_chat_id=comments_meta["chat_id"],
        comments_chat_title=comments_meta["title"],
        comments_chat_username=comments_meta["username"],
        comments_chat_internal_id=comments_meta["internal_id"],
    )


async def prepare_chat_runs_per_topic(
    *,
    client: Any,
    repo: Any,
    settings: Any,
    chat_id: int,
    chat_title: str | None,
    enrich_opts: EnrichOpts,
    chat_username: str | None = None,
    chat_internal_id: int | None = None,
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
    from_msg_id: int | None = None,
    full_history: bool = False,
    include_transcripts: bool = True,
    min_msg_chars: int | None = None,
    mark_read: bool = False,
    yes: bool = False,
):
    """Yield one PreparedRun per forum topic.

    Use `async for prepared in prepare_chat_runs_per_topic(...)`. The
    iterator defers each topic's backfill+enrichment until the
    consumer pulls it — keeps memory bounded for big forums.
    """
    from analyzetg.tg.topics import list_forum_topics
    from analyzetg.util.logging import get_logger

    log = get_logger(__name__)

    console.print("[dim]→ Listing forum topics...[/]")
    topics = await list_forum_topics(client, chat_id)
    explicit_period = bool(since_dt or until_dt or from_msg_id is not None or full_history)
    targets = topics if explicit_period else [t for t in topics if t.unread_count > 0]
    if not targets:
        console.print(
            "[yellow]No topics with unread messages.[/] "
            "Pass --last-days / --full-history to analyze everything anyway."
        )
        return

    if not yes:
        total_unread = sum(t.unread_count for t in targets)
        if not typer.confirm(
            f"Process {len(targets)} topic(s)"
            + (f" with {total_unread} unread" if not explicit_period else "")
            + "?",
            default=True,
        ):
            console.print("[dim]Aborted.[/]")
            return

    for t in targets:
        topic_title_display = f"{chat_title or chat_id} / {t.title}"
        console.print(
            f"\n[bold cyan]>>[/] {t.title} (topic_id={t.topic_id}"
            + (f", {t.unread_count} unread" if not explicit_period else "")
            + ")"
        )
        topic_from_msg = from_msg_id
        topic_full = full_history
        if not explicit_period:
            # Per-topic unread anchor: Telegram's topic-level marker is
            # "last read msg_id". Add 1 to get first-unread.
            topic_from_msg = t.read_inbox_max_id + 1
            topic_full = False

        try:
            prepared = await prepare_chat_run(
                client=client,
                repo=repo,
                settings=settings,
                chat_id=chat_id,
                thread_id=t.topic_id,
                chat_title=topic_title_display,
                thread_title=t.title,
                chat_username=chat_username,
                chat_internal_id=chat_internal_id,
                since_dt=since_dt,
                until_dt=until_dt,
                from_msg_id=topic_from_msg,
                full_history=topic_full,
                enrich_opts=enrich_opts,
                include_transcripts=include_transcripts,
                min_msg_chars=min_msg_chars,
                topic_titles=None,
                topic_markers=None,
                mark_read=mark_read,
            )
            yield prepared
        except typer.Exit:
            raise
        except Exception as e:
            log.error(
                "prepare_chat_runs_per_topic.error",
                chat_id=chat_id,
                topic_id=t.topic_id,
                err=str(e)[:300],
            )
            console.print(f"[red]Topic {t.title} failed:[/] {e}")


async def prepare_all_unread_runs(
    *,
    client: Any,
    repo: Any,
    settings: Any,
    enrich_opts: EnrichOpts,
    include_transcripts: bool = True,
    min_msg_chars: int | None = None,
    mark_read: bool = False,
    folder: str | None = None,
    yes: bool = False,
):
    """Yield one PreparedRun per chat with unread messages.

    Analog of today's `_run_no_ref`. Folder-scoped when `folder` is set
    (matches Telegram folder title case-insensitively).
    """
    from analyzetg.tg.dialogs import list_unread_dialogs
    from analyzetg.util.logging import get_logger

    log = get_logger(__name__)

    unread = await list_unread_dialogs(client)
    if not unread:
        console.print("[yellow]No dialogs with unread messages.[/]")
        return

    if folder:
        from analyzetg.tg.folders import list_folders, resolve_folder

        folders = await list_folders(client)
        matched = resolve_folder(folder, folders)
        if matched is None:
            titles = ", ".join(f"'{f.title}'" for f in folders) or "(none)"
            console.print(f"[red]No folder matching[/] '{folder}'. Available folders: {titles}")
            raise typer.Exit(2)
        ids = matched.include_chat_ids
        if not ids and matched.has_rule_based_inclusion:
            console.print(
                f"[yellow]Folder '{matched.title}' uses category rules "
                "(contacts/groups/bots/etc.) without explicit chats — "
                "rule expansion isn't supported.[/]"
            )
            raise typer.Exit(2)
        before = len(unread)
        unread = [d for d in unread if d.chat_id in ids]
        console.print(
            f"[dim]→ Folder[/] [bold]{matched.title}[/]"
            f"{' ' + matched.emoticon if matched.emoticon else ''}"
            f" [dim]— {len(unread)}/{before} unread chats match[/]"
        )
        if not unread:
            console.print("[yellow]No chats in this folder have unread messages.[/]")
            return

    if not yes:
        total = sum(d.unread_count for d in unread)
        if not typer.confirm(
            f"Process {len(unread)} chat(s) with {total} total unread message(s)?",
            default=False,
        ):
            console.print("[dim]Aborted.[/]")
            return

    for u in unread:
        console.print(f"\n[bold cyan]>>[/] {u.title or u.chat_id} ({u.unread_count} unread)")
        try:
            # Derive internal_id for t.me/c/ link template.
            internal_id = None
            if u.chat_id < 0:
                abs_id = abs(u.chat_id)
                if abs_id > 1_000_000_000_000:
                    internal_id = abs_id - 1_000_000_000_000

            # Forums carry unread state per topic. A single dialog marker can
            # miss topic-local unread ranges, so flatten with topic markers.
            if u.kind == "forum":
                from analyzetg.tg.topics import list_forum_topics

                topics = await list_forum_topics(client, u.chat_id)
                unread_topics = [t for t in topics if t.unread_count > 0]
                if not unread_topics:
                    console.print("[yellow]No unread forum topics after refresh.[/]")
                    continue
                topic_titles = {t.topic_id: t.title for t in topics}
                topic_markers = {t.topic_id: t.read_inbox_max_id for t in topics}
                min_unread_marker = min(t.read_inbox_max_id for t in unread_topics)
                prepared = await prepare_chat_run(
                    client=client,
                    repo=repo,
                    settings=settings,
                    chat_id=u.chat_id,
                    thread_id=None,
                    chat_title=u.title,
                    thread_title=None,
                    chat_username=u.username,
                    chat_internal_id=internal_id,
                    from_msg_id=min_unread_marker + 1,
                    full_history=False,
                    enrich_opts=enrich_opts,
                    include_transcripts=include_transcripts,
                    min_msg_chars=min_msg_chars,
                    topic_titles=topic_titles,
                    topic_markers=topic_markers,
                    mark_read=mark_read,
                )
                yield prepared
                continue

            # Dialog read_inbox_max_id is "last read msg_id"; +1 gets us
            # to first-unread. prepare_chat_run subtracts one inside
            # _determine_start, so the net is iter_messages with
            # min_msg_id = read_inbox_max_id (exclusive) = correct.
            from_msg = u.read_inbox_max_id + 1

            # Stale-marker guard: broadcast channels and accounts that
            # never explicitly read a chat sometimes report
            # `read_inbox_max_id=0` (or a very low value) alongside a
            # small unread_count. Without this clamp, Telethon walks the
            # whole chat history (300k+ msgs) just to surface 31 unread.
            # When the implied window is >10x unread_count, trust the
            # badge and start at `latest - unread_count - 50`.
            if u.unread_count > 0:
                try:
                    latest = await client.get_messages(u.chat_id, limit=1)
                    if latest:
                        latest_id = int(latest[0].id)
                        gap = latest_id - u.read_inbox_max_id
                        if gap > u.unread_count * 10:
                            clamped = max(latest_id - u.unread_count - 50, 1)
                            console.print(
                                f"[yellow]→ Stale read marker[/] "
                                f"(msg_id={u.read_inbox_max_id}, "
                                f"latest={latest_id}, unread={u.unread_count}); "
                                f"trusting unread badge → start at "
                                f"msg_id={clamped}"
                            )
                            from_msg = clamped
                except Exception as e:
                    log.debug(
                        "prepare_all_unread_runs.latest_lookup_failed",
                        chat_id=u.chat_id,
                        err=str(e)[:200],
                    )

            prepared = await prepare_chat_run(
                client=client,
                repo=repo,
                settings=settings,
                chat_id=u.chat_id,
                thread_id=None,
                chat_title=u.title,
                thread_title=None,
                chat_username=u.username,
                chat_internal_id=internal_id,
                from_msg_id=from_msg,
                full_history=False,
                enrich_opts=enrich_opts,
                include_transcripts=include_transcripts,
                min_msg_chars=min_msg_chars,
                mark_read=mark_read,
            )
            yield prepared
        except typer.Exit:
            raise
        except Exception as e:
            log.error(
                "prepare_all_unread_runs.chat_error",
                chat_id=u.chat_id,
                err=str(e)[:300],
            )
            console.print(f"[red]Failed:[/] {e}")
