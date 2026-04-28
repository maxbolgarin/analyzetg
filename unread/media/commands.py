"""CLI command: unread download-media (+ reusable save_raw_media helper).

`unread download-media` is a thin wrapper over the shared chat-run
pipeline: `prepare_chat_run` → `save_raw_media`. The same helper is
invoked from `unread dump --save-media` so a single text dump can bundle
the raw attachment bytes. Keeps original-media archival logic in one
place regardless of which CLI entry point the user reached.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from unread.config import get_settings
from unread.core.paths import chat_slug as _chat_slug
from unread.core.paths import compute_window as _compute_window
from unread.core.paths import reports_dir as _reports_dir
from unread.core.paths import topic_slug as _topic_slug
from unread.db.repo import open_repo
from unread.i18n import t as _t
from unread.i18n import tf as _tf
from unread.media.download import download_message
from unread.models import Message
from unread.tg.client import tg_client
from unread.tg.resolver import resolve
from unread.util.logging import get_logger

if TYPE_CHECKING:
    from unread.core.run import PreparedRun

console = Console()
log = get_logger(__name__)

VALID_TYPES = ("voice", "videonote", "video", "photo", "doc")


def _safe_filename_component(name: str) -> str:
    """Strip anything that could escape the destination dir.

    Not a full sanitizer — just enough to prevent `../../etc/passwd` shenanigans
    in Telegram filename attributes, which come from arbitrary senders.
    """
    return name.replace("/", "_").replace("\\", "_").lstrip(".") or "file"


def media_filename(msg: Message, tel_msg) -> str:
    """Pick a disk filename for a media message.

    Shape: `{msg_id}.{ext}` for fixed-format media (photo/voice/video),
    or `{msg_id}_{original-name}` for documents so PDFs, zip archives,
    etc. keep their human-readable filenames. msg_id prefix guarantees
    uniqueness inside the output directory and makes sorting obvious.
    """
    mt = msg.media_type
    if mt == "photo":
        return f"{msg.msg_id}.jpg"
    if mt == "voice":
        return f"{msg.msg_id}.ogg"
    if mt in {"videonote", "video"}:
        return f"{msg.msg_id}.mp4"
    if mt == "doc":
        doc = getattr(tel_msg, "document", None) or getattr(getattr(tel_msg, "media", None), "document", None)
        if doc is not None:
            for attr in getattr(doc, "attributes", None) or []:
                file_name = getattr(attr, "file_name", None)
                if file_name:
                    return f"{msg.msg_id}_{_safe_filename_component(file_name)}"
        mime = (getattr(doc, "mime_type", "") or "").lower() if doc else ""
        if "pdf" in mime:
            return f"{msg.msg_id}.pdf"
        if "zip" in mime:
            return f"{msg.msg_id}.zip"
        return f"{msg.msg_id}.bin"
    return f"{msg.msg_id}.bin"


def _existing_for_msg(out_dir: Path, msg_id: int) -> Path | None:
    """Already-downloaded file for this msg_id, regardless of extension."""
    try:
        for p in out_dir.glob(f"{msg_id}.*"):
            if p.is_file():
                return p
        for p in out_dir.glob(f"{msg_id}_*"):
            if p.is_file():
                return p
    except OSError:
        return None
    return None


async def save_raw_media(
    prepared: PreparedRun,
    *,
    types: set[str] | None = None,
    output_dir: Path | None = None,
    limit: int | None = None,
    overwrite: bool = False,
) -> dict[str, int]:
    """Walk `prepared.messages`, download + save raw media bytes.

    Used by both `unread download-media` (standalone) and `unread dump
    --save-media` (bundled with a text dump). When `output_dir` is
    None, derives the path from prepared's slugs:
      reports/<chat-slug>/media/                — non-forum / flat-forum
      reports/<chat-slug>/<topic-slug>/media/   — single-topic

    Returns a dict of counts: {'done', 'skipped', 'failed', 'no_media'}.
    """
    settings = prepared.settings
    client = prepared.client

    if output_dir is None:
        base = _reports_dir()
        chat_slug = _chat_slug(prepared.chat_title, prepared.chat_id)
        if prepared.thread_id:
            topic_slug = _topic_slug(prepared.thread_title, prepared.thread_id)
            output_dir = base / chat_slug / topic_slug / "media"
        else:
            output_dir = base / chat_slug / "media"

    candidates = [
        m
        for m in prepared.messages
        if m.media_type is not None
        and m.media_type in VALID_TYPES
        and (types is None or m.media_type in types)
    ]
    if limit is not None:
        candidates = candidates[:limit]
    if not candidates:
        return {"done": 0, "skipped": 0, "failed": 0, "no_media": 0}

    by_type: dict[str, int] = {}
    for m in candidates:
        by_type[m.media_type or "unknown"] = by_type.get(m.media_type or "unknown", 0) + 1
    breakdown = ", ".join(f"{v} {k}" for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]))
    console.print(
        f"[bold]{_t('media_saving_label')}[/] "
        f"{_tf('media_n_files', n=len(candidates))} — {breakdown} → [cyan]{output_dir}[/]"
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(settings.media.download_concurrency)
    stats = {"done": 0, "skipped": 0, "failed": 0, "no_media": 0}

    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("{task.fields[label]}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("downloading", total=len(candidates), label="")

        async def worker(m: Message) -> None:
            async with sem:
                try:
                    if not overwrite and _existing_for_msg(output_dir, m.msg_id) is not None:
                        stats["skipped"] += 1
                        progress.advance(task)
                        return
                    tel_msg = await client.get_messages(prepared.chat_id, ids=m.msg_id)
                    if tel_msg is None or getattr(tel_msg, "media", None) is None:
                        log.debug(
                            "save_raw_media.no_media_server",
                            chat_id=prepared.chat_id,
                            msg_id=m.msg_id,
                        )
                        stats["no_media"] += 1
                        progress.advance(task)
                        return
                    filename = media_filename(m, tel_msg)
                    dest = output_dir / filename
                    if dest.exists() and not overwrite:
                        stats["skipped"] += 1
                        progress.advance(task)
                        return
                    progress.update(task, label=f"[dim]{filename}[/]")
                    await download_message(client, tel_msg, dest)
                    stats["done"] += 1
                except Exception as e:
                    log.error(
                        "save_raw_media.error",
                        chat_id=prepared.chat_id,
                        msg_id=m.msg_id,
                        media_type=m.media_type,
                        err=str(e)[:300],
                    )
                    partial = output_dir / f"{m.msg_id}.partial"
                    with contextlib.suppress(FileNotFoundError):
                        partial.unlink()
                    stats["failed"] += 1
                finally:
                    progress.advance(task)

        await asyncio.gather(*(worker(m) for m in candidates))

    console.print(
        f"[green]Saved[/] {stats['done']}/{len(candidates)}  "
        f"[dim]skipped={stats['skipped']} "
        f"unavailable={stats['no_media']} failed={stats['failed']}[/]  "
        f"→ [cyan]{output_dir}[/]"
    )
    return stats


async def cmd_download_media(
    *,
    ref: str,
    thread: int | None = None,
    types: str | None = None,
    since: str | None = None,
    until: str | None = None,
    last_days: int | None = None,
    output: Path | None = None,
    limit: int | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> None:
    """Download raw media from a chat to local disk.

    Thin wrapper over `prepare_chat_run` + `save_raw_media`. Kept as a
    separate CLI for backwards compat and for users who want raw media
    without a text dump attached.
    """
    from unread.core.pipeline import prepare_chat_run
    from unread.enrich.base import EnrichOpts

    settings = get_settings()
    since_dt, until_dt = _compute_window(since, until, last_days)

    type_filter: set[str] | None = None
    if types:
        requested = {t.strip().lower() for t in types.split(",") if t.strip()}
        unknown = requested - set(VALID_TYPES)
        if unknown:
            raise typer.BadParameter(
                f"Unknown media types: {sorted(unknown)}. Valid: {', '.join(VALID_TYPES)}"
            )
        type_filter = requested

    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        console.print(f"[dim]{_t('media_resolving_label')}[/] {ref}")
        resolved = await resolve(client, repo, ref)
        chat_id = resolved.chat_id
        thread_id = thread if thread is not None else (resolved.thread_id or 0)

        prepared = await prepare_chat_run(
            client=client,
            repo=repo,
            settings=settings,
            chat_id=chat_id,
            thread_id=thread_id if thread_id else None,
            chat_title=resolved.title,
            thread_title=None,
            chat_username=resolved.username,
            since_dt=since_dt,
            until_dt=until_dt,
            full_history=False,
            enrich_opts=EnrichOpts(),  # raw bytes; no text enrichment
            include_transcripts=False,
            mark_read=False,
            # Download-media archives bytes, not text. filter_messages
            # would drop every media-only row (empty effective_text),
            # leaving save_raw_media with nothing to save — the whole
            # point of the command.
            skip_filter=True,
        )

        if dry_run:
            candidates = [
                m
                for m in prepared.messages
                if m.media_type in VALID_TYPES and (type_filter is None or m.media_type in type_filter)
            ]
            if limit is not None:
                candidates = candidates[:limit]
            if not candidates:
                console.print(f"[yellow]{_t('media_no_matching')}[/]")
                return
            by_type: dict[str, int] = {}
            for m in candidates:
                by_type[m.media_type or "unknown"] = by_type.get(m.media_type or "unknown", 0) + 1
            breakdown = ", ".join(f"{v} {k}" for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]))
            console.print(
                f"[bold]{_t('media_plan_label')}[/] {_tf('media_n_files', n=len(candidates))} — {breakdown}"
            )
            sample = candidates[:10]
            for m in sample:
                console.print(f"  [dim]{m.media_type:<10}[/] msg_id={m.msg_id}  {m.date}")
            if len(candidates) > len(sample):
                console.print(f"  [dim]{_tf('cli_prune_and_more', n=len(candidates) - len(sample))}[/]")
            console.print(f"[dim]{_t('media_dry_run_no_files')}[/]")
            return

        await save_raw_media(
            prepared,
            types=type_filter,
            output_dir=output,
            limit=limit,
            overwrite=overwrite,
        )
