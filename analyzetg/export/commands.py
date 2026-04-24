"""CLI commands: analyzetg export, analyzetg dump.

`cmd_dump` mirrors `cmd_analyze`: resolve a ref, pull fresh from Telegram,
write md/jsonl/csv. No subscription row, no sync_state writes. Default
starting point is the dialog's unread marker.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from analyzetg.config import get_settings
from analyzetg.core.paths import compute_window as _compute_window
from analyzetg.core.paths import has_explicit_period as _has_explicit_period
from analyzetg.core.paths import parse_ymd as _parse_ymd
from analyzetg.core.paths import slugify as _slugify
from analyzetg.db.repo import Repo, open_repo
from analyzetg.export.markdown import export_csv, export_jsonl, export_md
from analyzetg.models import Message
from analyzetg.tg.client import tg_client
from analyzetg.tg.dialogs import (
    UnreadDialog,
    list_unread_dialogs,
    mark_as_read,
)
from analyzetg.tg.resolver import resolve
from analyzetg.tg.sync import backfill
from analyzetg.tg.topics import ForumTopic, list_forum_topics
from analyzetg.util.logging import get_logger

console = Console()
log = get_logger(__name__)


def _parse_from_msg(value: str | None) -> int | None:
    if not value:
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    from analyzetg.tg.links import parse

    return parse(value).msg_id


def _write(msgs: list[Message], *, fmt: str, output: Path, title: str | None) -> None:
    if fmt == "md":
        export_md(msgs, title=title, output=output)
    elif fmt == "jsonl":
        export_jsonl(msgs, output)
    elif fmt == "csv":
        export_csv(msgs, output)
    else:
        console.print(f"[red]Unknown format:[/] {fmt}")
        raise typer.Exit(1)


async def cmd_export(*, chat: int, fmt: str, output: Path, since: str | None, until: str | None) -> None:
    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        chat_row = await repo.get_chat(chat)
        msgs = await repo.iter_messages(chat, since=_parse_ymd(since), until=_parse_ymd(until))
        title = (chat_row or {}).get("title")
        _write(msgs, fmt=fmt, output=output, title=title)
        console.print(f"[green]Exported[/] {len(msgs)} message(s) to {output}")


async def cmd_dump(
    *,
    ref: str | None,
    output: Path | None,
    fmt: str,
    since: str | None,
    until: str | None,
    last_days: int | None,
    full_history: bool,
    thread: int | None,
    from_msg: str | None,
    join: bool,
    with_transcribe: bool,
    include_transcripts: bool,
    console_out: bool = False,
    save_default: bool = False,
    mark_read: bool | None = None,
    all_flat: bool = False,
    all_per_topic: bool = False,
    enrich: str | None = None,
    enrich_all: bool = False,
    no_enrich: bool = False,
    save_media: bool = False,
    save_media_types: str | None = None,
    yes: bool = False,
) -> None:
    """Pull chat history end-to-end and write it to a file. No OpenAI chat analysis.

    Default starting point is the dialog's unread marker. Pass
    `--last-days`, `--from-msg`, `--full-history`, or `--since/--until` to
    override. When <ref> is omitted, iterates every dialog with unread
    messages after a confirmation prompt. Forum chats support
    `--thread N`, `--all-flat` (whole forum, explicit period required), or
    `--all-per-topic` (one file per topic).
    """
    # No ref → interactive wizard (pick chat → thread → enrich → period → run).
    # Wizard opens its own tg_client; return before this function tries to.
    if ref is None:
        from analyzetg.interactive import run_interactive_dump

        await run_interactive_dump(
            fmt=fmt,
            output=output,
            save_default=save_default,
            with_transcribe=with_transcribe,
            include_transcripts=include_transcripts,
            console_out=console_out,
            mark_read=mark_read,
        )
        return

    # Direct path: treat mark_read=None as False (CLI tri-state default).
    mark_read_bool = bool(mark_read)

    # Build EnrichOpts from CLI flags (analyzer/commands hosts the shared
    # helper). No preset for dump mode, so preset.enrich_kinds is empty.
    from analyzetg.analyzer.commands import build_enrich_opts

    enrich_opts = build_enrich_opts(
        cli_enrich=enrich,
        cli_enrich_all=enrich_all,
        cli_no_enrich=no_enrich,
        preset=None,
    )

    settings = get_settings()
    since_dt, until_dt = _compute_window(since, until, last_days)
    from_msg_id = _parse_from_msg(from_msg)

    # Parse save_media_types CSV once; None → all kinds.
    save_media_kinds: set[str] | None = None
    if save_media_types:
        save_media_kinds = {k.strip() for k in save_media_types.split(",") if k.strip()}

    from analyzetg.analyzer.commands import _derive_internal_id

    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        console.print(f"[dim]→ Resolving[/] {ref}")
        resolved = await resolve(client, repo, ref, join=join)
        chat_id = resolved.chat_id
        thread_id = thread if thread is not None else (resolved.thread_id or 0)
        console.print(
            f"[dim]→ Resolved[/] {resolved.title or chat_id} "
            f"[dim](id={chat_id}, kind={resolved.kind}"
            f"{', thread=' + str(thread_id) if thread_id else ''})[/]"
        )
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
            all_flat, all_per_topic, thread_id = await _forum_pick_mode(client, chat_id, resolved.title)

        if is_forum and all_per_topic:
            await _dump_forum_per_topic(
                client=client,
                repo=repo,
                settings=settings,
                chat_id=chat_id,
                chat_title=resolved.title,
                chat_username=resolved.username,
                chat_internal_id=_derive_internal_id(chat_id),
                since_dt=since_dt,
                until_dt=until_dt,
                from_msg_id=from_msg_id,
                full_history=full_history,
                fmt=fmt,
                output=output,
                with_transcribe=with_transcribe,
                include_transcripts=include_transcripts,
                console_out=console_out,
                mark_read=mark_read_bool,
                enrich_opts=enrich_opts,
                save_media=save_media,
                save_media_types=save_media_kinds,
                yes=yes,
            )
            return

        # Flat-forum: fetch topics for per-topic read markers + titles (same
        # precision analyze uses). Needed for both unread-floor computation
        # and per-topic mark-read after the dump finishes.
        topic_titles: dict[int, str] | None = None
        topic_markers: dict[int, int] | None = None
        thread_title: str | None = None

        if is_forum and all_flat:
            thread_id = None
            console.print("[dim]→ Listing forum topics for flat-forum grouping...[/]")
            topics_for_flat = await list_forum_topics(client, chat_id)
            topic_titles = {t.topic_id: t.title for t in topics_for_flat if t.title}
            topic_markers = {t.topic_id: int(t.read_inbox_max_id or 0) for t in topics_for_flat}
            if not _has_explicit_period(since_dt, until_dt, from_msg_id, full_history):
                non_zero = [m for m in topic_markers.values() if m > 0]
                if non_zero:
                    from_msg_id = min(non_zero)
                    unread_across = sum(t.unread_count for t in topics_for_flat)
                    console.print(
                        f"[dim]→ Forum unread: {unread_across} across "
                        f"{len(topic_markers)} topics "
                        f"(floor msg_id={from_msg_id} from oldest per-topic marker)[/]"
                    )

        # Single topic in a forum + unread-default → resolve topic's marker.
        if (
            is_forum
            and thread_id
            and thread_id > 0
            and not _has_explicit_period(since_dt, until_dt, from_msg_id, full_history)
        ):
            console.print("[dim]→ Looking up topic's unread marker...[/]")
            topics = await list_forum_topics(client, chat_id)
            matched = next((t for t in topics if t.topic_id == thread_id), None)
            if matched is None:
                console.print(f"[red]Topic {thread_id} not found in this forum.[/]")
                raise typer.Exit(2)
            if matched.unread_count == 0:
                console.print(
                    f"[yellow]No unread messages in topic '{matched.title}'.[/] "
                    "Pass --last-days / --full-history to dump anyway."
                )
                raise typer.Exit(0)
            from_msg_id = matched.read_inbox_max_id + 1
            thread_title = matched.title
            console.print(
                f"[dim]→ {matched.unread_count} unread in '{matched.title}' "
                f"after msg_id={matched.read_inbox_max_id}[/]"
            )
        elif is_forum and thread_id and thread_id > 0:
            # Explicit period path still needs the topic title for the
            # per-topic report directory layout.
            topics = await list_forum_topics(client, chat_id)
            matched = next((t for t in topics if t.topic_id == thread_id), None)
            thread_title = matched.title if matched else None

        await _dump_single(
            client=client,
            repo=repo,
            settings=settings,
            chat_id=chat_id,
            thread_id=thread_id,
            title=resolved.title,
            thread_title=thread_title,
            chat_username=resolved.username,
            chat_internal_id=_derive_internal_id(chat_id),
            since_dt=since_dt,
            until_dt=until_dt,
            from_msg_id=from_msg_id,
            full_history=full_history,
            fmt=fmt,
            output=output,
            with_transcribe=with_transcribe,
            include_transcripts=include_transcripts,
            console_out=console_out,
            mark_read=mark_read_bool,
            enrich_opts=enrich_opts,
            topic_titles=topic_titles,
            topic_markers=topic_markers,
            save_media=save_media,
            save_media_types=save_media_kinds,
        )


async def _dump_single(
    *,
    client,
    repo: Repo,
    settings,
    chat_id: int,
    thread_id: int | None,
    title: str | None,
    since_dt: datetime | None,
    until_dt: datetime | None,
    from_msg_id: int | None,
    full_history: bool,
    fmt: str,
    output: Path | None,
    with_transcribe: bool,
    include_transcripts: bool,
    console_out: bool,
    mark_read: bool,
    enrich_opts=None,
    thread_title: str | None = None,
    chat_username: str | None = None,
    chat_internal_id: int | None = None,
    topic_titles: dict[int, str] | None = None,
    topic_markers: dict[int, int] | None = None,
    save_media: bool = False,
    save_media_types: set[str] | None = None,
) -> None:
    """Dump one chat / thread / flat-forum using the shared pipeline."""
    from analyzetg.core.pipeline import prepare_chat_run
    from analyzetg.enrich.base import EnrichOpts

    # Legacy --with-transcribe: fall back to voice+videonote+video
    # enrichment when no enrich_opts was supplied. Direct enrichment
    # supersedes transcribe-only mode.
    effective_enrich = enrich_opts if enrich_opts is not None else EnrichOpts()
    if with_transcribe and not effective_enrich.any_enabled():
        effective_enrich = EnrichOpts(voice=True, videonote=True, video=True)

    prepared = await prepare_chat_run(
        client=client,
        repo=repo,
        settings=settings,
        chat_id=chat_id,
        thread_id=thread_id,
        chat_title=title,
        thread_title=thread_title,
        chat_username=chat_username,
        chat_internal_id=chat_internal_id,
        since_dt=since_dt,
        until_dt=until_dt,
        from_msg_id=from_msg_id,
        full_history=full_history,
        enrich_opts=effective_enrich,
        include_transcripts=include_transcripts,
        topic_titles=topic_titles,
        topic_markers=topic_markers,
        mark_read=mark_read,
    )

    if save_media and prepared.messages:
        from analyzetg.media.commands import save_raw_media

        await save_raw_media(
            prepared,
            types=save_media_types,
            output_dir=None,
            limit=None,
            overwrite=False,
        )

    msgs = prepared.messages
    if console_out:
        _print_console(msgs, title=title, fmt=fmt, count=len(msgs))
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            _write(msgs, fmt=fmt, output=output, title=title)
            console.print(f"[green]Also saved:[/] {output}")
    else:
        target = output if output is not None else _default_output_path(title, fmt)
        target.parent.mkdir(parents=True, exist_ok=True)
        _write(msgs, fmt=fmt, output=target, title=title)
        console.print(f"[green]Wrote[/] {len(msgs)} message(s) to {target}")

    if prepared.mark_read_fn and msgs:
        await prepared.mark_read_fn()


async def _dump_forum_per_topic(
    *,
    client,
    repo: Repo,
    settings,
    chat_id: int,
    chat_title: str | None,
    chat_username: str | None = None,
    chat_internal_id: int | None = None,
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
    from_msg_id: int | None = None,
    full_history: bool = False,
    fmt: str,
    output: Path | None,
    with_transcribe: bool,
    include_transcripts: bool,
    console_out: bool,
    mark_read: bool,
    enrich_opts=None,
    save_media: bool = False,
    save_media_types: set[str] | None = None,
    yes: bool = False,
) -> None:
    """One dump per topic, using the shared per-topic iterator.

    Layout: `{output_root}/{chat-slug}/{topic-slug}/dump/dump-{stamp}.{ext}`
    — mirrors the analyze per-topic layout so a forum's artefacts stay
    grouped by topic regardless of which command produced them.
    """
    from analyzetg.analyzer.commands import _chat_slug, _topic_slug
    from analyzetg.core.pipeline import prepare_chat_runs_per_topic
    from analyzetg.enrich.base import EnrichOpts

    effective_enrich = enrich_opts if enrich_opts is not None else EnrichOpts()
    if with_transcribe and not effective_enrich.any_enabled():
        effective_enrich = EnrichOpts(voice=True, videonote=True, video=True)

    if console_out:
        base_dir: Path | None = None
    else:
        if output is not None and output.exists() and output.is_dir():
            base_dir = output
        elif output is not None and output.suffix:
            console.print(f"[red]--output {output} is a single file; per-topic mode needs a directory.[/]")
            raise typer.Exit(2)
        else:
            base_dir = output or Path("reports")
        base_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    ext = {"md": "md", "jsonl": "jsonl", "csv": "csv"}.get(fmt, "md")
    chat_slug_str = _chat_slug(chat_title, chat_id)

    async for prepared in prepare_chat_runs_per_topic(
        client=client,
        repo=repo,
        settings=settings,
        chat_id=chat_id,
        chat_title=chat_title,
        chat_username=chat_username,
        chat_internal_id=chat_internal_id,
        since_dt=since_dt,
        until_dt=until_dt,
        from_msg_id=from_msg_id,
        full_history=full_history,
        enrich_opts=effective_enrich,
        include_transcripts=include_transcripts,
        mark_read=mark_read,
        yes=yes,
    ):
        try:
            if save_media and prepared.messages:
                from analyzetg.media.commands import save_raw_media

                await save_raw_media(
                    prepared,
                    types=save_media_types,
                    output_dir=None,
                    limit=None,
                    overwrite=False,
                )

            per_file = None
            if base_dir is not None:
                topic_slug_str = _topic_slug(prepared.thread_title, prepared.thread_id or 0)
                per_file = base_dir / chat_slug_str / topic_slug_str / "dump" / f"dump-{stamp}.{ext}"

            msgs = prepared.messages
            if console_out:
                _print_console(msgs, title=prepared.chat_title, fmt=fmt, count=len(msgs))
                if per_file is not None:
                    per_file.parent.mkdir(parents=True, exist_ok=True)
                    _write(msgs, fmt=fmt, output=per_file, title=prepared.chat_title)
            else:
                target = per_file if per_file else _default_output_path(prepared.chat_title, fmt)
                target.parent.mkdir(parents=True, exist_ok=True)
                _write(msgs, fmt=fmt, output=target, title=prepared.chat_title)
                console.print(f"[green]Wrote[/] {len(msgs)} message(s) to {target}")

            if prepared.mark_read_fn and msgs:
                await prepared.mark_read_fn()
        except typer.Exit:
            raise
        except Exception as e:
            log.error(
                "dump.forum_per_topic.error",
                chat_id=chat_id,
                topic_id=prepared.thread_id,
                err=str(e)[:200],
            )
            console.print(f"[red]Topic {prepared.thread_title} failed:[/] {e}")


async def _forum_pick_mode(client, chat_id: int, chat_title: str | None) -> tuple[bool, bool, int]:
    """Interactively pick a forum mode for dump. Returns (all_flat, all_per_topic, thread_id)."""
    import sys as _sys

    console.print("[dim]→ Listing forum topics...[/]")
    topics = await list_forum_topics(client, chat_id)
    if not topics:
        console.print("[yellow]No topics in this forum.[/]")
        raise typer.Exit(0)

    if not _sys.stdin.isatty():
        _print_topics_table(topics, with_unread=True)
        console.print(
            "\n[red]This is a forum — pick one of:[/]\n"
            "  --thread <id>       single topic\n"
            "  --all-per-topic     one file per topic\n"
            "  --all-flat          whole forum as one dump (needs a period flag)\n"
        )
        raise typer.Exit(2)

    _print_topics_table(topics, with_unread=True)
    while True:
        answer = typer.prompt("Pick topic id, A=all-flat, P=per-topic, Q=quit", default="P").strip()
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
    from rich.table import Table as _Table

    t = _Table(title="Forum topics")
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


async def _transcribe_pending(
    *,
    client,
    repo: Repo,
    settings,
    chat_id: int,
    since_dt: datetime | None,
    until_dt: datetime | None,
) -> None:
    from analyzetg.enrich.audio import transcribe_message

    pending = await repo.untranscribed_media(chat_id=chat_id, since=since_dt, until=until_dt)
    pending = [m for m in pending if _transcribable(m, settings)]
    console.print(f"[cyan]Transcribe[/] pending={len(pending)}")

    sem = asyncio.Semaphore(settings.media.download_concurrency)

    async def work(m: Message) -> None:
        async with sem:
            try:
                await transcribe_message(client=client, repo=repo, msg=m)
            except Exception as e:
                log.error(
                    "dump.transcribe_error",
                    chat_id=m.chat_id,
                    msg_id=m.msg_id,
                    err=str(e)[:200],
                )

    await asyncio.gather(*[work(m) for m in pending])


async def run_all_unread_dump(
    *,
    fmt: str = "md",
    output: Path | None = None,
    with_transcribe: bool = False,
    include_transcripts: bool = True,
    console_out: bool = False,
    mark_read: bool = False,
) -> None:
    """Public: dump every unread chat in one batch (was the old no-ref default)."""
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        await _dump_no_ref(
            client=client,
            repo=repo,
            output=output,
            fmt=fmt,
            with_transcribe=with_transcribe,
            include_transcripts=include_transcripts,
            console_out=console_out,
            mark_read=mark_read,
        )


async def _dump_no_ref(
    *,
    client,
    repo: Repo,
    output: Path | None,
    fmt: str,
    with_transcribe: bool,
    include_transcripts: bool,
    console_out: bool,
    mark_read: bool,
) -> None:
    unread = await list_unread_dialogs(client)
    if not unread:
        console.print("[yellow]No dialogs with unread messages.[/]")
        return
    _print_unread_table(unread)
    total = sum(d.unread_count for d in unread)
    if not typer.confirm(
        f"Dump {len(unread)} chat(s) with {total} total unread message(s)?",
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
    settings = get_settings()
    ext = {"md": "md", "jsonl": "jsonl", "csv": "csv"}.get(fmt, "md")
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
            if with_transcribe:
                await _transcribe_pending(
                    client=client,
                    repo=repo,
                    settings=settings,
                    chat_id=u.chat_id,
                    since_dt=None,
                    until_dt=None,
                )
            msgs = await repo.iter_messages(u.chat_id, min_msg_id=u.read_inbox_max_id)
            if not include_transcripts:
                for m in msgs:
                    if m.transcript and not m.text:
                        m.transcript = None
            if out_dir is None:
                _print_console(msgs, title=u.title, fmt=fmt, count=len(msgs))
            else:
                chat_out = out_dir / _slugify(u.title or str(u.chat_id)) / "dump"
                chat_out.mkdir(parents=True, exist_ok=True)
                path = chat_out / f"dump-{stamp}.{ext}"
                _write(msgs, fmt=fmt, output=path, title=u.title)
                console.print(f"[green]Wrote[/] {len(msgs)} message(s) to {path}")
            if mark_read and msgs:
                latest = max(m.msg_id for m in msgs)
                ok = await mark_as_read(client, u.chat_id, latest)
                if ok:
                    console.print(f"[dim]→ Marked read up to msg_id={latest}[/]")
        except Exception as e:
            log.error("dump.no_ref.chat_error", chat_id=u.chat_id, err=str(e)[:200])
            console.print(f"[red]Failed:[/] {e}")


def _print_unread_table(dialogs: list[UnreadDialog]) -> None:
    from rich.table import Table

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


def _default_output_path(title: str | None, fmt: str) -> Path:
    slug = _slugify(title or "") or "chat"
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    ext = {"md": "md", "jsonl": "jsonl", "csv": "csv"}.get(fmt, "md")
    # reports/{chat-slug}/dump/dump-{stamp}.{ext}
    return Path("reports") / slug / "dump" / f"dump-{stamp}.{ext}"


def _print_console(msgs: list[Message], *, title: str | None, fmt: str, count: int) -> None:
    """Render the dump inline. Only `md` uses Rich's Markdown; others print raw."""
    from rich.rule import Rule

    console.print(Rule(title or "dump", style="cyan"))
    if fmt == "md":
        from rich.markdown import Markdown

        from analyzetg.export.markdown import render_md

        console.print(Markdown(render_md(msgs, title=title)))
    else:
        # jsonl/csv aren't human-friendly in Rich's renderer — just print raw.
        import io

        buf = io.StringIO()
        if fmt == "jsonl":
            import json

            for m in msgs:
                buf.write(
                    json.dumps(
                        {
                            "chat_id": m.chat_id,
                            "msg_id": m.msg_id,
                            "date": m.date.isoformat(),
                            "sender_name": m.sender_name,
                            "text": m.text,
                            "transcript": m.transcript,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        elif fmt == "csv":
            import csv as _csv

            w = _csv.writer(buf)
            w.writerow(["msg_id", "date", "sender_name", "text", "transcript"])
            for m in msgs:
                w.writerow([m.msg_id, m.date.isoformat(), m.sender_name, m.text, m.transcript])
        console.print(buf.getvalue(), highlight=False)
    console.print(Rule(style="cyan"))
    console.print(f"[dim]{count} message(s)[/]")


def _transcribable(m: Message, settings) -> bool:
    if m.media_type == "voice" and not settings.media.transcribe_voice:
        return False
    if m.media_type == "videonote" and not settings.media.transcribe_videonote:
        return False
    if m.media_type == "video" and not settings.media.transcribe_video:
        return False
    d = m.media_duration
    return not (
        d is not None
        and (d > settings.media.max_media_duration_sec or d < settings.media.min_media_duration_sec)
    )
