"""CLI commands: analyzetg export, analyzetg dump."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console

from analyzetg.config import get_settings
from analyzetg.db.repo import open_repo
from analyzetg.export.markdown import export_csv, export_jsonl, export_md
from analyzetg.models import Message, Subscription
from analyzetg.util.logging import get_logger

console = Console()
log = get_logger(__name__)


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d")


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


async def cmd_export(
    *, chat: int, fmt: str, output: Path, since: str | None, until: str | None
) -> None:
    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        chat_row = await repo.get_chat(chat)
        msgs = await repo.iter_messages(chat, since=_parse(since), until=_parse(until))
        title = (chat_row or {}).get("title")
        _write(msgs, fmt=fmt, output=output, title=title)
        console.print(f"[green]Exported[/] {len(msgs)} message(s) to {output}")


async def cmd_dump(
    *,
    ref: str,
    output: Path,
    fmt: str,
    since: str | None,
    until: str | None,
    last_days: int | None,
    full_history: bool,
    thread: int | None,
    join: bool,
    with_transcribe: bool,
    include_transcripts: bool,
    no_subscribe: bool,
) -> None:
    """Pull chat history end-to-end and write it to a file. No OpenAI chat analysis.

    By default a subscription row is created so subsequent dumps (or a plain
    `sync --all`) only fetch new messages. Pass --no-subscribe for a one-shot
    pull that doesn't persist the subscription.
    """
    from analyzetg.tg.client import tg_client
    from analyzetg.tg.resolver import resolve
    from analyzetg.tg.sync import sync_subscription

    settings = get_settings()
    now = datetime.now()
    if last_days:
        since_dt: datetime | None = now - timedelta(days=last_days)
        until_dt: datetime | None = now
    else:
        since_dt = _parse(since)
        until_dt = _parse(until)
    if full_history:
        since_dt = None  # do not filter the export
        start_from_date = datetime(1970, 1, 1)
    else:
        start_from_date = since_dt

    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        resolved = await resolve(client, repo, ref, join=join)
        thread_id = thread or 0
        sub = Subscription(
            chat_id=resolved.chat_id,
            thread_id=thread_id,
            title=resolved.title,
            source_kind="topic" if thread_id else _default_source_kind(resolved.kind),
            start_from_date=start_from_date,
            transcribe_voice=with_transcribe,
            transcribe_videonote=with_transcribe,
            transcribe_video=False,
        )

        if not no_subscribe:
            await repo.upsert_subscription(sub)

        added = await sync_subscription(client, repo, sub)
        console.print(
            f"[cyan]Sync[/] chat={sub.chat_id} thread={sub.thread_id} -> +{added} msg(s)"
        )

        if with_transcribe:
            from analyzetg.media.transcribe import transcribe_message

            pending = await repo.untranscribed_media(
                chat_id=sub.chat_id, since=since_dt, until=until_dt
            )
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
                            chat_id=m.chat_id, msg_id=m.msg_id, err=str(e)[:200],
                        )

            await asyncio.gather(*[work(m) for m in pending])

        msgs = await repo.iter_messages(
            sub.chat_id,
            thread_id=sub.thread_id,
            since=since_dt,
            until=until_dt,
        )
        if not include_transcripts:
            # Hide transcripts from the exported body (but keep them in the DB).
            for m in msgs:
                if m.transcript and not m.text:
                    m.transcript = None

        _write(msgs, fmt=fmt, output=output, title=resolved.title)
        console.print(
            f"[green]Wrote[/] {len(msgs)} message(s) to {output}"
            f"{' (subscription persisted)' if not no_subscribe else ''}"
        )


def _default_source_kind(kind: str) -> str:
    return "channel" if kind == "channel" else "chat"


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
        and (
            d > settings.media.max_media_duration_sec
            or d < settings.media.min_media_duration_sec
        )
    )
