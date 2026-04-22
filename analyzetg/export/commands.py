"""CLI commands: analyzetg export, analyzetg dump.

`cmd_dump` mirrors `cmd_analyze`: resolve a ref, pull fresh from Telegram,
write md/jsonl/csv. No subscription row, no sync_state writes. Default
starting point is the dialog's unread marker.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console

from analyzetg.config import get_settings
from analyzetg.db.repo import Repo, open_repo
from analyzetg.export.markdown import export_csv, export_jsonl, export_md
from analyzetg.models import Message
from analyzetg.tg.client import tg_client
from analyzetg.tg.dialogs import (
    UnreadDialog,
    get_unread_state,
    list_unread_dialogs,
    mark_as_read,
)
from analyzetg.tg.resolver import resolve
from analyzetg.tg.sync import backfill
from analyzetg.util.logging import get_logger

console = Console()
log = get_logger(__name__)


def _parse_ymd(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d")


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
    mark_read: bool = False,
) -> None:
    """Pull chat history end-to-end and write it to a file. No OpenAI chat analysis.

    Default starting point is the dialog's unread marker. Pass
    `--last-days`, `--from-msg`, `--full-history`, or `--since/--until` to
    override. When <ref> is omitted, iterates every dialog with unread
    messages after a confirmation prompt.
    """
    settings = get_settings()
    since_dt, until_dt = _compute_window(since, until, last_days)
    from_msg_id = _parse_from_msg(from_msg)

    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        if ref is None:
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
            return

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

        start_msg_id = await _determine_start(
            client=client,
            chat_id=chat_id,
            thread_id=thread_id,
            full_history=full_history,
            from_msg_id=from_msg_id,
            time_window=(since_dt, until_dt),
        )

        console.print("[dim]→ Fetching new messages from Telegram...[/]")
        await _pull_history(
            client=client,
            repo=repo,
            chat_id=chat_id,
            thread_id=thread_id,
            start_msg_id=start_msg_id,
            since_dt=since_dt,
        )

        if with_transcribe:
            await _transcribe_pending(
                client=client,
                repo=repo,
                settings=settings,
                chat_id=chat_id,
                since_dt=since_dt,
                until_dt=until_dt,
            )

        msgs = await repo.iter_messages(
            chat_id,
            thread_id=thread_id,
            since=since_dt,
            until=until_dt,
            min_msg_id=start_msg_id if start_msg_id and start_msg_id > 0 else None,
        )
        if not include_transcripts:
            for m in msgs:
                if m.transcript and not m.text:
                    m.transcript = None

        if console_out:
            _print_console(msgs, title=resolved.title, fmt=fmt, count=len(msgs))
            if output is not None:
                output.parent.mkdir(parents=True, exist_ok=True)
                _write(msgs, fmt=fmt, output=output, title=resolved.title)
                console.print(f"[green]Also saved:[/] {output}")
        else:
            if output is None:
                output = _default_output_path(resolved.title, fmt)
            output.parent.mkdir(parents=True, exist_ok=True)
            _write(msgs, fmt=fmt, output=output, title=resolved.title)
            console.print(f"[green]Wrote[/] {len(msgs)} message(s) to {output}")

        if mark_read and msgs:
            latest = max(m.msg_id for m in msgs)
            ok = await mark_as_read(client, chat_id, latest, thread_id=thread_id or None)
            if ok:
                console.print(f"[dim]→ Marked read up to msg_id={latest}[/]")


async def _determine_start(
    *,
    client,
    chat_id: int,
    thread_id: int,
    full_history: bool,
    from_msg_id: int | None,
    time_window: tuple[datetime | None, datetime | None],
) -> int | None:
    if full_history:
        return None
    if from_msg_id is not None:
        return max(from_msg_id - 1, 0)
    if time_window[0] is not None or time_window[1] is not None:
        return None
    if thread_id:
        console.print(
            "[red]Per-topic unread is not exposed by Telegram's high-level API.[/]\n"
            "Pass [cyan]--last-days N[/], [cyan]--from-msg <id>[/], or [cyan]--full-history[/]."
        )
        raise typer.Exit(2)
    console.print("[dim]→ Reading unread marker...[/]")
    unread_count, read_marker = await get_unread_state(client, chat_id)
    if unread_count == 0:
        console.print(
            f"[yellow]No unread messages in chat {chat_id}.[/] "
            "Pass --last-days / --from-msg / --full-history to dump anyway."
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
    if start_msg_id is not None or since_dt is None:
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


async def _transcribe_pending(
    *,
    client,
    repo: Repo,
    settings,
    chat_id: int,
    since_dt: datetime | None,
    until_dt: datetime | None,
) -> None:
    from analyzetg.media.transcribe import transcribe_message

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
                path = out_dir / f"{_slugify(u.title or str(u.chat_id))}-{stamp}.{ext}"
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


_SLUG_RE = re.compile(r"[^A-Za-z0-9_\-]+")


def _slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text).strip("-").lower()
    return slug[:40] or "chat"


def _default_output_path(title: str | None, fmt: str) -> Path:
    slug = _slugify(title or "chat")
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    ext = {"md": "md", "jsonl": "jsonl", "csv": "csv"}.get(fmt, "md")
    return Path("reports") / f"{slug}-dump-{stamp}.{ext}"


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


def _compute_window(
    since: str | None, until: str | None, last_days: int | None
) -> tuple[datetime | None, datetime | None]:
    if last_days:
        until_dt = datetime.now()
        since_dt = until_dt - timedelta(days=last_days)
        return since_dt, until_dt
    return _parse_ymd(since), _parse_ymd(until)


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
