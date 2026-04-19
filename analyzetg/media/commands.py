"""CLI command: analyzetg transcribe."""

from __future__ import annotations

import asyncio
from datetime import datetime

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from analyzetg.config import get_settings
from analyzetg.db.repo import open_repo
from analyzetg.media.transcribe import transcribe_message
from analyzetg.tg.client import tg_client
from analyzetg.util.logging import get_logger

console = Console()
log = get_logger(__name__)


def _parse_ymd(v: str | None) -> datetime | None:
    if not v:
        return None
    return datetime.strptime(v, "%Y-%m-%d")


async def cmd_transcribe(
    *,
    chat: int | None,
    since: str | None,
    until: str | None,
    model: str | None,
    max_duration: int | None,
    limit: int | None,
    dry_run: bool,
) -> None:
    settings = get_settings()
    since_dt = _parse_ymd(since)
    until_dt = _parse_ymd(until)
    max_dur = max_duration if max_duration is not None else settings.media.max_media_duration_sec
    min_dur = settings.media.min_media_duration_sec

    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        pending = await repo.untranscribed_media(
            chat_id=chat, since=since_dt, until=until_dt, limit=limit
        )

        def eligible(m) -> bool:
            if m.media_type == "voice" and not settings.media.transcribe_voice:
                return False
            if m.media_type == "videonote" and not settings.media.transcribe_videonote:
                return False
            if m.media_type == "video" and not settings.media.transcribe_video:
                return False
            return not (
                m.media_duration is not None
                and (m.media_duration > max_dur or m.media_duration < min_dur)
            )

        eligible_msgs = [m for m in pending if eligible(m)]
        console.print(
            f"[cyan]Transcribe[/] pending={len(pending)} eligible={len(eligible_msgs)}"
            f" (max_duration={max_dur}s, model={model or settings.openai.audio_model_default})"
        )
        if dry_run or not eligible_msgs:
            return

        sem = asyncio.Semaphore(settings.media.download_concurrency)
        done = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("transcribing", total=len(eligible_msgs))

            async def worker(m) -> None:
                nonlocal done
                async with sem:
                    try:
                        await transcribe_message(
                            client=client, repo=repo, msg=m, model=model,
                        )
                    except Exception as e:
                        log.error(
                            "transcribe.error",
                            chat_id=m.chat_id, msg_id=m.msg_id, err=str(e)[:200],
                        )
                    done += 1
                    progress.update(task, advance=1)

            await asyncio.gather(*[worker(m) for m in eligible_msgs])

        console.print(f"[green]Transcribed {done} message(s).[/]")
