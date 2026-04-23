"""CLI commands for analyze + stats.

`cmd_analyze` resolves a chat reference, pulls messages fresh from Telegram
(no subscription row, no sync_state writes), and hands off to the existing
analysis pipeline. Default start-point is the dialog's unread marker.

Forum chats are first-class: `--thread N` targets one topic; `--all-flat`
collapses the whole forum into one analysis; `--all-per-topic` runs one
analysis per topic with unread. Without any mode flag in a TTY, a picker
prompts for choice.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from analyzetg.analyzer.pipeline import AnalysisOptions, AnalysisResult, run_analysis
from analyzetg.config import get_settings
from analyzetg.db.repo import Repo, open_repo
from analyzetg.tg.client import tg_client
from analyzetg.tg.dialogs import (
    UnreadDialog,
    get_unread_state,
    list_unread_dialogs,
    mark_as_read,
)
from analyzetg.tg.resolver import resolve
from analyzetg.tg.sync import backfill
from analyzetg.tg.topics import ForumTopic, list_forum_topics
from analyzetg.util.logging import get_logger

console = Console()
log = get_logger(__name__)


def _parse_ymd(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d")


def _derive_internal_id(chat_id: int) -> int | None:
    """Strip the `-100` prefix Telethon uses for channels/supergroups.

    Returns None for regular users / small groups where the id isn't
    suitable for a t.me/c/ link.
    """
    if chat_id >= 0:
        return None
    abs_id = abs(chat_id)
    if abs_id > 1_000_000_000_000:
        return abs_id - 1_000_000_000_000
    return None


def _parse_from_msg(value: str | None) -> int | None:
    if not value:
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    from analyzetg.tg.links import parse

    return parse(value).msg_id


def _has_explicit_period(
    since_dt: datetime | None,
    until_dt: datetime | None,
    from_msg_id: int | None,
    full_history: bool,
) -> bool:
    return bool(since_dt or until_dt or from_msg_id is not None or full_history)


async def cmd_analyze(
    *,
    ref: str | None,
    thread: int | None,
    from_msg: str | None,
    full_history: bool,
    since: str | None,
    until: str | None,
    last_days: int | None,
    preset: str,
    prompt_file: Path | None,
    model: str | None,
    filter_model: str | None,
    output: Path | None,
    console_out: bool = False,
    save_default: bool = False,
    mark_read: bool | None = None,
    no_cache: bool = False,
    include_transcripts: bool = True,
    min_msg_chars: int | None = None,
    all_flat: bool = False,
    all_per_topic: bool = False,
    folder: str | None = None,
) -> None:
    # No ref but --folder → batch-analyze unread chats in that folder; skip wizard.
    if ref is None and folder:
        await run_all_unread_analyze(
            preset=preset,
            prompt_file=prompt_file,
            model=model,
            filter_model=filter_model,
            output=output,
            console_out=console_out,
            mark_read=bool(mark_read),
            no_cache=no_cache,
            include_transcripts=include_transcripts,
            min_msg_chars=min_msg_chars,
            folder=folder,
        )
        return
    # No ref → interactive wizard (pick chat → thread → preset → period → run).
    # Wizard opens its own tg_client; return before this function tries to.
    if ref is None:
        from analyzetg.interactive import run_interactive_analyze

        await run_interactive_analyze(
            console_out=console_out,
            output=output,
            save_default=save_default,
            mark_read=mark_read,
        )
        return
    # Direct path: treat mark_read=None as False (CLI tri-state default).
    mark_read_bool = bool(mark_read)

    settings = get_settings()
    since_dt, until_dt = _compute_window(since, until, last_days)
    from_msg_id = _parse_from_msg(from_msg)

    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        console.print(f"[dim]→ Resolving[/] {ref}")
        resolved = await resolve(client, repo, ref)
        chat_id = resolved.chat_id
        thread_id = thread if thread is not None else (resolved.thread_id or 0)
        title = resolved.title
        console.print(
            f"[dim]→ Resolved[/] {title or chat_id} "
            f"[dim](id={chat_id}, kind={resolved.kind}"
            f"{', thread=' + str(thread_id) if thread_id else ''})[/]"
        )

        # A link like /group/100/5000 carries a msg_id; treat it as start
        # unless an explicit time window / full-history flag was passed.
        if (
            from_msg_id is None
            and not full_history
            and resolved.msg_id is not None
            and since_dt is None
            and until_dt is None
        ):
            from_msg_id = resolved.msg_id

        # --- Forum routing
        is_forum = resolved.kind == "forum"
        if is_forum and thread_id == 0 and not all_flat and not all_per_topic:
            # No explicit topic / mode. Decide via interactive picker or bail.
            all_flat, all_per_topic, thread_id = await _forum_pick_mode(client, chat_id, title)

        if is_forum and all_per_topic:
            await _run_forum_per_topic(
                client=client,
                repo=repo,
                chat_id=chat_id,
                chat_title=title,
                chat_username=resolved.username,
                chat_internal_id=_derive_internal_id(chat_id),
                since_dt=since_dt,
                until_dt=until_dt,
                from_msg_id=from_msg_id,
                full_history=full_history,
                preset=preset,
                prompt_file=prompt_file,
                model=model,
                filter_model=filter_model,
                output=output,
                console_out=console_out,
                mark_read=mark_read_bool,
                no_cache=no_cache,
                include_transcripts=include_transcripts,
                min_msg_chars=min_msg_chars,
            )
            return

        if is_forum and all_flat:
            if not _has_explicit_period(since_dt, until_dt, from_msg_id, full_history):
                console.print(
                    "[red]--all-flat on a forum needs an explicit period.[/]\n"
                    "Pass [cyan]--last-days N[/], [cyan]--full-history[/], or "
                    "[cyan]--since/--until[/]. Forum-wide unread across topics "
                    "isn't collapsible into one marker — use "
                    "[cyan]--all-per-topic[/] instead."
                )
                raise typer.Exit(2)
            # thread_id=None → iter_messages skips the thread filter entirely.
            thread_id = None

        # --- Single topic in a forum + unread-default: resolve the topic's
        # own read marker so the unread-default path has a usable anchor.
        if (
            is_forum
            and thread_id
            and thread_id > 0
            and not _has_explicit_period(since_dt, until_dt, from_msg_id, full_history)
        ):
            from analyzetg.tg.topics import list_forum_topics

            console.print("[dim]→ Looking up topic's unread marker...[/]")
            topics = await list_forum_topics(client, chat_id)
            matched = next((t for t in topics if t.topic_id == thread_id), None)
            if matched is None:
                console.print(f"[red]Topic {thread_id} not found in this forum.[/]")
                raise typer.Exit(2)
            if matched.unread_count == 0:
                console.print(
                    f"[yellow]No unread messages in topic '{matched.title}'.[/] "
                    "Pass --last-days / --full-history to analyze anyway."
                )
                raise typer.Exit(0)
            from_msg_id = matched.read_inbox_max_id + 1
            console.print(
                f"[dim]→ {matched.unread_count} unread in '{matched.title}' "
                f"after msg_id={matched.read_inbox_max_id}[/]"
            )

        # --- Single-chat / single-topic / flat-forum path
        await _run_single(
            client=client,
            repo=repo,
            chat_id=chat_id,
            thread_id=thread_id,
            title=title,
            chat_username=resolved.username,
            chat_internal_id=_derive_internal_id(chat_id),
            since_dt=since_dt,
            until_dt=until_dt,
            from_msg_id=from_msg_id,
            full_history=full_history,
            preset=preset,
            prompt_file=prompt_file,
            model=model,
            filter_model=filter_model,
            output=output,
            console_out=console_out,
            mark_read=mark_read_bool,
            no_cache=no_cache,
            include_transcripts=include_transcripts,
            min_msg_chars=min_msg_chars,
        )


async def _run_single(
    *,
    client,
    repo: Repo,
    chat_id: int,
    thread_id: int | None,
    title: str | None,
    chat_username: str | None,
    chat_internal_id: int | None,
    since_dt: datetime | None,
    until_dt: datetime | None,
    from_msg_id: int | None,
    full_history: bool,
    preset: str,
    prompt_file: Path | None,
    model: str | None,
    filter_model: str | None,
    output: Path | None,
    console_out: bool,
    mark_read: bool,
    no_cache: bool,
    include_transcripts: bool,
    min_msg_chars: int | None,
) -> None:
    """Analyze one chat or one thread. Shared by ref-mode and per-topic loop."""
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
    )
    console.print("[dim]→ Running analysis...[/]")

    opts = AnalysisOptions(
        preset=preset,
        prompt_file=prompt_file,
        model_override=model,
        filter_model_override=filter_model,
        use_cache=not no_cache,
        include_transcripts=include_transcripts,
        min_msg_chars=min_msg_chars,
        since=since_dt,
        until=until_dt,
        min_msg_id=start_msg_id if start_msg_id and start_msg_id > 0 else None,
    )
    result = await run_analysis(
        repo=repo,
        chat_id=chat_id,
        thread_id=thread_id,
        title=title,
        opts=opts,
        chat_username=chat_username,
        chat_internal_id=chat_internal_id,
    )

    if mark_read and result.msg_count > 0:
        # For flat-forum (thread_id=None), send_read_acknowledge moves the
        # dialog-level marker; individual topic markers aren't touched by
        # Telethon's high-level helper, so we skip (mark_as_read already does).
        latest = await repo.get_max_msg_id(chat_id, thread_id if thread_id else None)
        if latest:
            ok = await mark_as_read(client, chat_id, latest, thread_id=thread_id)
            if ok:
                console.print(f"[dim]→ Marked read up to msg_id={latest}[/]")

    _print_and_write(result, output=output, title=title, console_out=console_out)


async def _run_forum_per_topic(
    *,
    client,
    repo: Repo,
    chat_id: int,
    chat_title: str | None,
    chat_username: str | None,
    chat_internal_id: int | None,
    since_dt: datetime | None,
    until_dt: datetime | None,
    from_msg_id: int | None,
    full_history: bool,
    preset: str,
    prompt_file: Path | None,
    model: str | None,
    filter_model: str | None,
    output: Path | None,
    console_out: bool,
    mark_read: bool,
    no_cache: bool,
    include_transcripts: bool,
    min_msg_chars: int | None,
) -> None:
    """One analysis per topic; reports land in reports/{chat-slug}/."""
    console.print("[dim]→ Listing forum topics...[/]")
    topics = await list_forum_topics(client, chat_id)
    explicit_period = _has_explicit_period(since_dt, until_dt, from_msg_id, full_history)
    targets = topics if explicit_period else [t for t in topics if t.unread_count > 0]
    if not targets:
        console.print(
            "[yellow]No topics with unread messages.[/] "
            "Pass --last-days / --full-history to analyze everything anyway."
        )
        return

    _print_topics_table(targets, with_unread=True)
    total_unread = sum(t.unread_count for t in targets)
    if not typer.confirm(
        f"Analyze {len(targets)} topic(s)"
        + (f" with {total_unread} unread" if not explicit_period else "")
        + "?",
        default=True,
    ):
        console.print("[dim]Aborted.[/]")
        return

    if console_out:
        out_dir = None
    else:
        chat_slug = _slugify(chat_title or str(chat_id))
        # Layout: {output_root}/{chat-slug}/analyze/{topic-slug}-{preset}-{stamp}.md
        if output is not None and output.exists() and output.is_dir():
            out_dir = output / chat_slug / "analyze"
        elif output is not None and output.suffix:
            console.print(f"[red]--output {output} is a single file; per-topic mode needs a directory.[/]")
            raise typer.Exit(2)
        else:
            out_dir = (output or Path("reports")) / chat_slug / "analyze"
        out_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    for t in targets:
        topic_title = f"{chat_title or chat_id} / {t.title}"
        console.print(
            f"\n[bold cyan]>>[/] {t.title} "
            f"(topic_id={t.topic_id}" + (f", {t.unread_count} unread" if not explicit_period else "") + ")"
        )
        try:
            # Per-topic unread marker (not the dialog-level one).
            topic_from_msg = from_msg_id
            topic_full = full_history
            if not explicit_period:
                # Use topic's own read marker as the start.
                topic_from_msg = t.read_inbox_max_id + 1 if t.read_inbox_max_id else None
                # If topic has no read_inbox_max_id but unread_count > 0 (rare),
                # fall back to "full topic history".
                if topic_from_msg is None:
                    topic_full = True

            per_file = (
                out_dir / f"{_slugify(t.title or str(t.topic_id))}-{preset}-{stamp}.md" if out_dir else None
            )
            await _run_single(
                client=client,
                repo=repo,
                chat_id=chat_id,
                thread_id=t.topic_id,
                title=topic_title,
                chat_username=chat_username,
                chat_internal_id=chat_internal_id,
                since_dt=since_dt,
                until_dt=until_dt,
                from_msg_id=topic_from_msg,
                full_history=topic_full,
                preset=preset,
                prompt_file=prompt_file,
                model=model,
                filter_model=filter_model,
                output=per_file,
                console_out=console_out,
                mark_read=mark_read,
                no_cache=no_cache,
                include_transcripts=include_transcripts,
                min_msg_chars=min_msg_chars,
            )
        except typer.Exit:
            raise
        except Exception as e:
            log.error(
                "analyze.forum_per_topic.error",
                chat_id=chat_id,
                topic_id=t.topic_id,
                err=str(e)[:200],
            )
            console.print(f"[red]Topic {t.title} failed:[/] {e}")


async def _forum_pick_mode(client, chat_id: int, chat_title: str | None) -> tuple[bool, bool, int]:
    """Interactively pick a forum mode. Returns (all_flat, all_per_topic, thread_id)."""
    console.print("[dim]→ Listing forum topics...[/]")
    topics = await list_forum_topics(client, chat_id)
    if not topics:
        console.print("[yellow]No topics in this forum.[/]")
        raise typer.Exit(0)

    if not sys.stdin.isatty():
        _print_topics_table(topics, with_unread=True)
        console.print(
            "\n[red]This is a forum — pick one of:[/]\n"
            "  --thread <id>       single topic\n"
            "  --all-per-topic     one analysis per topic\n"
            "  --all-flat          whole forum as one chat (needs a period flag)\n"
            "Or run without flags in a terminal for an interactive picker."
        )
        raise typer.Exit(2)

    _print_topics_table(topics, with_unread=True)
    prompt = "Pick topic id, [cyan]A[/]ll-flat, [cyan]P[/]er-topic, [cyan]Q[/]uit"
    while True:
        answer = typer.prompt(prompt.replace("[cyan]", "").replace("[/]", ""), default="P")
        answer = answer.strip()
        up = answer.upper()
        if up == "Q":
            console.print("[dim]Aborted.[/]")
            raise typer.Exit(0)
        if up == "A":
            return True, False, 0
        if up == "P":
            return False, True, 0
        if answer.isdigit():
            tid = int(answer)
            if any(t.topic_id == tid for t in topics):
                return False, False, tid
            console.print(f"[red]No topic with id={tid}.[/]")
            continue
        console.print("[red]Not a valid choice. Try again.[/]")


def _print_topics_table(topics: list[ForumTopic], *, with_unread: bool = True) -> None:
    t = Table(title="Forum topics")
    cols = ["id", "title", "unread", "top_msg", "closed", "pinned"]
    if not with_unread:
        cols.remove("unread")
    for col in cols:
        t.add_column(col)
    for topic in topics:
        row = [
            str(topic.topic_id),
            topic.title,
            str(topic.unread_count) if with_unread else None,
            str(topic.top_message or ""),
            "yes" if topic.closed else "",
            "yes" if topic.pinned else "",
        ]
        t.add_row(*[c for c in row if c is not None])
    console.print(t)


async def _determine_start(
    *,
    client,
    chat_id: int,
    thread_id: int,
    full_history: bool,
    from_msg_id: int | None,
    time_window: tuple[datetime | None, datetime | None],
) -> int | None:
    """Return a msg_id lower bound (exclusive) or None for time-window / full mode."""
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
    client,
    repo: Repo,
    chat_id: int,
    thread_id: int,
    start_msg_id: int | None,
    since_dt: datetime | None,
) -> None:
    """Fetch messages from Telegram, skipping any range already in the DB."""
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
    else:
        await backfill(
            client,
            repo,
            chat_id=chat_id,
            thread_id=thread_param,
            since_date=since_dt,
        )


async def _run_no_ref(
    *,
    client,
    repo: Repo,
    preset: str,
    prompt_file: Path | None,
    model: str | None,
    filter_model: str | None,
    output: Path | None,
    console_out: bool,
    mark_read: bool,
    no_cache: bool,
    include_transcripts: bool,
    min_msg_chars: int | None,
    folder: str | None = None,
) -> None:
    """No <ref>: list dialogs with unread messages, confirm, analyze each.

    `folder`, if given, restricts the batch to chats in that Telegram folder
    (dialog filter) — matched case-insensitively against folder titles."""
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
                "rule expansion isn't supported. Add chats to the folder "
                "explicitly in Telegram, or drop --folder.[/]"
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

    _print_unread_table(unread)
    total = sum(d.unread_count for d in unread)
    if not typer.confirm(
        f"Analyze {len(unread)} chat(s) with {total} total unread message(s)?",
        default=False,
    ):
        console.print("[dim]Aborted.[/]")
        return

    if console_out:
        out_dir = None
    else:
        out_dir = _resolve_output_dir(output, len(unread))
        if out_dir is None:
            out_dir = Path("reports")
            out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    for u in unread:
        console.print(f"\n[bold cyan]>>[/] {u.title or u.chat_id} ({u.unread_count} unread)")
        try:
            local_max = await repo.get_max_msg_id(u.chat_id, min_msg_id=u.read_inbox_max_id)
            floor = max(u.read_inbox_max_id, local_max or 0)
            await backfill(
                client,
                repo,
                chat_id=u.chat_id,
                from_msg_id=floor + 1,
                direction="forward",
            )
            opts = AnalysisOptions(
                preset=preset,
                prompt_file=prompt_file,
                model_override=model,
                filter_model_override=filter_model,
                use_cache=not no_cache,
                include_transcripts=include_transcripts,
                min_msg_chars=min_msg_chars,
                min_msg_id=u.read_inbox_max_id,
            )
            result = await run_analysis(
                repo=repo,
                chat_id=u.chat_id,
                thread_id=0,
                title=u.title,
                opts=opts,
                chat_username=u.username,
                chat_internal_id=_derive_internal_id(u.chat_id),
            )
            per_file = None
            if out_dir:
                chat_out = out_dir / _slugify(u.title or str(u.chat_id)) / "analyze"
                chat_out.mkdir(parents=True, exist_ok=True)
                per_file = chat_out / f"{preset}-{stamp}.md"
            _print_and_write(result, output=per_file, title=u.title, console_out=console_out)
            if mark_read and result.msg_count > 0:
                latest = await repo.get_max_msg_id(u.chat_id)
                if latest:
                    ok = await mark_as_read(client, u.chat_id, latest)
                    if ok:
                        console.print(f"[dim]→ Marked read up to msg_id={latest}[/]")
        except Exception as e:
            log.error("analyze.no_ref.chat_error", chat_id=u.chat_id, err=str(e)[:200])
            console.print(f"[red]Failed:[/] {e}")


def _print_unread_table(dialogs: list[UnreadDialog]) -> None:
    t = Table(title="Dialogs with unread messages")
    for col in ("id", "kind", "title", "username", "unread"):
        t.add_column(col)
    for d in dialogs:
        t.add_row(
            str(d.chat_id),
            d.kind,
            d.title or "",
            f"@{d.username}" if d.username else "",
            str(d.unread_count),
        )
    console.print(t)


def _resolve_output_dir(output: Path | None, n_chats: int) -> Path | None:
    """In no-ref mode we write one file per chat; output must be a directory."""
    if output is None:
        return None
    if output.exists() and output.is_dir():
        return output
    if output.suffix:
        console.print(
            f"[red]--output {output} is a single file, but {n_chats} chats need per-chat files.[/]\n"
            "Pass a directory path or drop --output."
        )
        raise typer.Exit(2)
    output.mkdir(parents=True, exist_ok=True)
    return output


_SLUG_RE = re.compile(r"[^A-Za-z0-9_\-]+")


def _slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text).strip("-").lower()
    return slug[:40] or "chat"


def _print_and_write(
    result: AnalysisResult,
    *,
    output: Path | None,
    title: str | None,
    console_out: bool = False,
) -> None:
    console.print(
        f"[bold cyan]Run[/] preset={result.preset} msgs={result.msg_count} "
        f"chunks={result.chunk_count} cache_hits={result.cache_hits}/"
        f"{result.cache_hits + result.cache_misses} cost=${result.total_cost_usd:.4f}"
    )
    body = _with_truncation_banner(result)

    if result.truncated:
        console.print(
            "[bold red]⚠ Output truncated[/] — the model hit "
            "[cyan]output_budget_tokens[/]. Edit the preset file "
            f"([cyan]presets/{result.preset}.md[/]) to raise it, or re-run with "
            "[cyan]--no-cache[/] if a stale cache is in the way."
        )

    if console_out:
        from rich.markdown import Markdown
        from rich.rule import Rule

        console.print(Rule(title or "result", style="cyan"))
        console.print(Markdown(body))
        console.print(Rule(style="cyan"))
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(body, encoding="utf-8")
            console.print(f"[green]Also saved:[/] {output}")
        return

    if output is None:
        output = _default_output_path(title, result.preset)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(body, encoding="utf-8")
    console.print(f"[green]Written:[/] {output}")


def _with_truncation_banner(result) -> str:
    if not getattr(result, "truncated", False):
        return result.final_result
    banner = (
        "> ⚠️ **Output was truncated.** The model hit "
        "`output_budget_tokens` and stopped mid-response.\n"
        f"> Raise the cap in `presets/{result.preset}.md` "
        "(e.g. `output_budget_tokens: 4000`) and re-run with `--no-cache`.\n\n"
    )
    return banner + result.final_result


def _default_output_path(title: str | None, preset: str) -> Path:
    slug = _slugify(title or "chat")
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    # reports/{chat-slug}/analyze/{preset}-{stamp}.md
    return Path("reports") / slug / "analyze" / f"{preset}-{stamp}.md"


def _compute_window(
    since: str | None, until: str | None, last_days: int | None
) -> tuple[datetime | None, datetime | None]:
    if last_days:
        until_dt = datetime.now()
        since_dt = until_dt - timedelta(days=last_days)
        return since_dt, until_dt
    return _parse_ymd(since), _parse_ymd(until)


async def run_all_unread_analyze(
    *,
    preset: str = "summary",
    prompt_file: Path | None = None,
    model: str | None = None,
    filter_model: str | None = None,
    output: Path | None = None,
    console_out: bool = False,
    mark_read: bool = False,
    no_cache: bool = False,
    include_transcripts: bool = True,
    min_msg_chars: int | None = None,
    folder: str | None = None,
) -> None:
    """Public: run the batch-across-all-unread-chats flow (was the old no-ref default).

    Pass `folder="Alpha"` (or any case-insensitive substring of a folder title)
    to restrict the batch to chats in that Telegram folder."""
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        await _run_no_ref(
            client=client,
            repo=repo,
            preset=preset,
            prompt_file=prompt_file,
            model=model,
            filter_model=filter_model,
            output=output,
            console_out=console_out,
            mark_read=mark_read,
            no_cache=no_cache,
            include_transcripts=include_transcripts,
            min_msg_chars=min_msg_chars,
            folder=folder,
        )


async def cmd_stats(since: str | None, by: str) -> None:
    settings = get_settings()
    since_dt = _parse_ymd(since)
    async with open_repo(settings.storage.data_path) as repo:
        rows = await repo.stats_by(group_by=by, since=since_dt)
        hit_rate = await repo.cache_hit_rate(since=since_dt)
        total_cost = sum(float(r["cost_usd"] or 0) for r in rows)

        t = Table(title=f"Usage (by {by}){' since ' + since if since else ''}")
        cols = ("bucket", "calls", "prompt", "cached", "completion", "audio_s", "cost_usd")
        for c in cols:
            t.add_column(c)
        for r in rows:
            t.add_row(
                str(r["bucket"]) if r["bucket"] is not None else "-",
                str(r["calls"]),
                str(r["prompt_tokens"] or 0),
                str(r["cached_tokens"] or 0),
                str(r["completion_tokens"] or 0),
                str(r["audio_seconds"] or 0),
                f"${float(r['cost_usd'] or 0):.4f}",
            )
        console.print(t)
        console.print(f"[bold]Total cost:[/] ${total_cost:.4f}")
        console.print(f"[bold]Cache hit rate:[/] {hit_rate:.1%}")
