"""CLI command: atg download-media.

Dumps raw media files (photos, voice, video, documents) from a chat to disk.
Separate from the enrichment pipeline — enrichment transforms media into
text for the analyzer; this command preserves the original bytes so the
user can keep an archive of their chat's attachments.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta
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

from analyzetg.analyzer.commands import _chat_slug, _topic_slug
from analyzetg.config import get_settings
from analyzetg.db.repo import open_repo
from analyzetg.media.download import download_message
from analyzetg.models import Message
from analyzetg.tg.client import tg_client
from analyzetg.tg.resolver import resolve
from analyzetg.util.logging import get_logger

if TYPE_CHECKING:
    pass

console = Console()
log = get_logger(__name__)

VALID_TYPES = ("voice", "videonote", "video", "photo", "doc")


def _parse_ymd(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d")


def _compute_window(
    since: str | None, until: str | None, last_days: int | None
) -> tuple[datetime | None, datetime | None]:
    if last_days:
        until_dt = datetime.now()
        return until_dt - timedelta(days=last_days), until_dt
    return _parse_ymd(since), _parse_ymd(until)


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
        # Telethon exposes the original filename via DocumentAttributeFilename.
        doc = getattr(tel_msg, "document", None) or getattr(getattr(tel_msg, "media", None), "document", None)
        if doc is not None:
            for attr in getattr(doc, "attributes", None) or []:
                file_name = getattr(attr, "file_name", None)
                if file_name:
                    return f"{msg.msg_id}_{_safe_filename_component(file_name)}"
        # No filename attribute — fall back to mime-based extension so the
        # file is at least openable in something.
        mime = (getattr(doc, "mime_type", "") or "").lower() if doc else ""
        if "pdf" in mime:
            return f"{msg.msg_id}.pdf"
        if "zip" in mime:
            return f"{msg.msg_id}.zip"
        return f"{msg.msg_id}.bin"
    return f"{msg.msg_id}.bin"


def _existing_for_msg(out_dir: Path, msg_id: int) -> Path | None:
    """Already-downloaded file for this msg_id, regardless of extension.

    Lets `--overwrite=false` skip a previously-saved file even if its
    extension drifted (e.g. a doc whose original name we couldn't
    recover on the first run — we saved `123.bin`, and the user
    doesn't want a duplicate).
    """
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

    Works off messages already synced to the local DB — run `atg sync` or
    `atg analyze` first if you need the latest messages. Keeps the command
    cheap and predictable (no surprise network fetches), and means
    repeated runs are idempotent until new messages land.
    """
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
        console.print(f"[dim]→ Resolving[/] {ref}")
        resolved = await resolve(client, repo, ref)
        chat_id = resolved.chat_id
        thread_id = thread if thread is not None else (resolved.thread_id or 0)

        msgs = await repo.iter_messages(
            chat_id,
            thread_id=thread_id if thread_id else None,
            since=since_dt,
            until=until_dt,
        )
        candidates = [
            m
            for m in msgs
            if m.media_type is not None
            and m.media_type in VALID_TYPES
            and (type_filter is None or m.media_type in type_filter)
        ]
        if limit is not None:
            candidates = candidates[:limit]

        if not candidates:
            console.print(
                "[yellow]No media matching filters.[/] "
                "Run [cyan]atg sync[/] or [cyan]atg analyze[/] first if this "
                "chat has new messages not yet in the local DB."
            )
            return

        # Compute output dir using the same chat/topic slug conventions
        # analyze uses, so `reports/` stays navigable.
        base = output or Path("reports")
        chat_slug = _chat_slug(resolved.title, chat_id)
        if thread_id:
            # We don't have the topic title handy here; use the numeric
            # fallback. Users on a single-topic forum who want a nicer
            # name can `atg describe <ref>` + rename the dir manually,
            # or we can auto-resolve in a later pass.
            topic_part = _topic_slug(None, thread_id)
            out_dir = base / chat_slug / topic_part / "media"
        else:
            out_dir = base / chat_slug / "media"

        # Preview counts by type so the user sees what they're buying.
        by_type: dict[str, int] = {}
        for m in candidates:
            by_type[m.media_type or "unknown"] = by_type.get(m.media_type or "unknown", 0) + 1
        breakdown = ", ".join(f"{v} {k}" for k, v in sorted(by_type.items(), key=lambda kv: -kv[1]))
        console.print(f"[bold]Plan:[/] {len(candidates)} file(s) — {breakdown} → [cyan]{out_dir}[/]")

        if dry_run:
            sample = candidates[:10]
            for m in sample:
                console.print(f"  [dim]{m.media_type:<10}[/] msg_id={m.msg_id}  {m.date}")
            if len(candidates) > len(sample):
                console.print(f"  [dim]…and {len(candidates) - len(sample)} more[/]")
            console.print("[dim]Dry run — no files written.[/]")
            return

        out_dir.mkdir(parents=True, exist_ok=True)
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
                        # Cheap pre-flight: if a file for this msg_id already
                        # exists and --overwrite is off, don't even hit
                        # Telegram for the metadata.
                        if not overwrite and _existing_for_msg(out_dir, m.msg_id) is not None:
                            stats["skipped"] += 1
                            progress.advance(task)
                            return

                        tel_msg = await client.get_messages(chat_id, ids=m.msg_id)
                        if tel_msg is None or getattr(tel_msg, "media", None) is None:
                            # Message deleted on the server, or media was
                            # revoked/expired — not an error, just unavailable.
                            log.debug(
                                "download_media.no_media_server",
                                chat_id=chat_id,
                                msg_id=m.msg_id,
                            )
                            stats["no_media"] += 1
                            progress.advance(task)
                            return
                        filename = media_filename(m, tel_msg)
                        dest = out_dir / filename
                        if dest.exists() and not overwrite:
                            stats["skipped"] += 1
                            progress.advance(task)
                            return
                        progress.update(task, label=f"[dim]{filename}[/]")
                        await download_message(client, tel_msg, dest)
                        stats["done"] += 1
                    except Exception as e:
                        log.error(
                            "download_media.error",
                            chat_id=chat_id,
                            msg_id=m.msg_id,
                            media_type=m.media_type,
                            err=str(e)[:300],
                        )
                        # Best-effort cleanup of a partial file so a retry
                        # can re-download cleanly.
                        partial = out_dir / f"{m.msg_id}.partial"
                        with contextlib.suppress(FileNotFoundError):
                            partial.unlink()
                        stats["failed"] += 1
                    finally:
                        progress.advance(task)

            await asyncio.gather(*(worker(m) for m in candidates))

        console.print(
            f"[green]Downloaded[/] {stats['done']}/{len(candidates)}  "
            f"[dim]skipped={stats['skipped']} "
            f"unavailable={stats['no_media']} failed={stats['failed']}[/]  "
            f"→ [cyan]{out_dir}[/]"
        )
