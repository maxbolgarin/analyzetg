"""Combined-mode runner: many bursted items → one merged analyze.

When the user taps `▶ Run combined` on the batch panel, every
combinable item in the burst is reduced to plain text, the chunks are
concatenated with section headers, written to a temp .txt, and run
through `cmd_analyze_file`. The result is one report that covers
every source.

TG-link items are not supported here yet — they need a Telethon user
session and a per-chat backfill pass to materialize the messages as
text. They're skipped (with a note in the report) and the user can
re-send the t.me link separately if they want it analyzed.
"""

from __future__ import annotations

import contextlib
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from unread.config import get_settings

if TYPE_CHECKING:
    from unread.bot.app import BotApp

log = structlog.get_logger(__name__)


async def run_combined(
    app: BotApp,
    *,
    items: list,
    panel_msg,
    original_event,
) -> None:
    """Extract text per item, write one combined .txt, analyze it.

    `panel_msg` is the batch panel — edited in-place to show progress
    ("⏳ Fetching 2/4…" → "⏳ Analyzing combined text…" → deleted on
    success). `original_event` is the user's last burst message,
    used as the reply anchor for the final report.
    """
    s = get_settings()
    tmp_dir = _make_tmp_dir(s)
    skipped: list[tuple[str, str]] = []  # (label, reason)
    started = time.time()

    if panel_msg is not None:
        with contextlib.suppress(Exception):
            await panel_msg.edit("⏳ Fetching sources…", buttons=None)

    sections: list[str] = []
    try:
        total = len(items)
        for idx, item in enumerate(items, start=1):
            label = _label_of(item)
            if panel_msg is not None and total > 1:
                with contextlib.suppress(Exception):
                    await panel_msg.edit(f"⏳ Fetching {idx}/{total}: {label}", buttons=None)
            try:
                text = await _extract_text(item, s=s, tmp_dir=tmp_dir, app=app)
            except _Unsupported as e:
                skipped.append((label, str(e)))
                log.info("bot.combined.skip", label=label, reason=str(e))
                continue
            except Exception as e:
                skipped.append((label, f"{type(e).__name__}: {e}"))
                log.exception("bot.combined.extract_failed", label=label)
                continue
            sections.append(f"# Source: {label}\n\n{text.strip()}\n")

        if not sections:
            msg = "✖ No items in this burst could be merged."
            if skipped:
                detail = "\n".join(f"  • {lbl}: {why}" for lbl, why in skipped)
                msg += f"\n\nSkipped:\n{detail}"
            if panel_msg is not None:
                with contextlib.suppress(Exception):
                    await panel_msg.edit(msg, buttons=None)
            return

        combined_path = tmp_dir / "combined.txt"
        combined_path.write_text("\n\n".join(sections), encoding="utf-8")
        log.info(
            "bot.combined.assembled",
            sections=len(sections),
            skipped=len(skipped),
            bytes=combined_path.stat().st_size,
        )

        if panel_msg is not None:
            note = ""
            if skipped:
                note = f" (skipped {len(skipped)})"
            with contextlib.suppress(Exception):
                await panel_msg.edit(f"⏳ Analyzing combined text…{note}", buttons=None)

        await _analyze_combined_file(combined_path, s=s, app=app, chat_id=original_event.chat_id)

        if panel_msg is not None:
            with contextlib.suppress(Exception):
                await panel_msg.edit("📄 Sending report…", buttons=None)

        from unread.bot import reply

        await reply.send_file_report(
            original_event,
            local_path=combined_path,
            preset=_effective_preset_combined(s, app, original_event.chat_id),
            started=started,
            kind="text",
        )

        if panel_msg is not None:
            with contextlib.suppress(Exception):
                await panel_msg.delete()

        if skipped:
            lines = "\n".join(f"• {lbl}: {why}" for lbl, why in skipped)
            await original_event.reply(f"⚠️ Combined run skipped {len(skipped)} item(s):\n{lines}")

    finally:
        with contextlib.suppress(Exception):
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Per-kind text extraction
# ---------------------------------------------------------------------------


class _Unsupported(RuntimeError):
    """Raised by `_extract_text` when an item kind isn't supported."""


async def _extract_text(item, *, s, tmp_dir: Path, app) -> str:
    """Dispatch to the kind-specific extractor. Returns plain text."""
    if item.kind == "url":
        return await _extract_url(item.payload["url"], s=s)
    if item.kind == "youtube":
        return await _extract_youtube(item.payload["url"], s=s)
    if item.kind == "file":
        return await _extract_file(item, s=s, tmp_dir=tmp_dir, app=app)
    if item.kind == "tg":
        raise _Unsupported("TG-link merging not supported — send separately")
    raise _Unsupported(f"unknown kind {item.kind!r}")


async def _extract_url(url: str, *, s) -> str:
    """Fetch the page and return the joined readable-extract paragraphs."""
    from unread.website.content import WebsiteFetchError, fetch_page

    try:
        page = await fetch_page(url, settings=s)
    except WebsiteFetchError as e:
        raise _Unsupported(f"fetch failed: {e}") from e
    return "\n\n".join(page.paragraphs)


async def _extract_youtube(url: str, *, s) -> str:
    """Resolve metadata + transcript using the same path as `cmd_analyze_youtube`."""
    from unread.db.repo import open_repo
    from unread.youtube.metadata import fetch_metadata
    from unread.youtube.transcript import NoTranscriptAvailable, get_transcript
    from unread.youtube.urls import extract_video_id

    try:
        video_id = extract_video_id(url)
    except ValueError as e:
        raise _Unsupported(f"not a valid YouTube URL: {e}") from e
    metadata = await fetch_metadata(video_id)
    try:
        async with open_repo(s.storage.data_path) as repo:
            result = await get_transcript(metadata, source="auto", settings=s, repo=repo)
    except NoTranscriptAvailable as e:
        raise _Unsupported(str(e)) from e
    title = metadata.title or video_id
    return f"YouTube — {title}\n\n{result.text}"


async def _extract_file(item, *, s, tmp_dir: Path, app) -> str:
    """Materialize the file and run the existing extractor stack on it."""
    from unread.files.extractors import (
        extract_audio,
        extract_docx,
        extract_image,
        extract_pdf,
        extract_text,
        extract_video,
    )

    payload = item.payload
    if payload.get("source") == "text":
        return (payload.get("text") or "").strip()

    if payload.get("source") != "media":
        raise _Unsupported(f"unsupported file source: {payload.get('source')!r}")

    name = payload.get("name") or "attachment"
    target = tmp_dir / name
    downloaded = await app.bot_client.download_media(item.event.message, file=str(target))
    if downloaded is None:
        raise _Unsupported("download returned no data")
    path = Path(downloaded)

    kind = payload.get("kind", "unknown")
    if kind == "text":
        return extract_text(path).text
    if kind == "pdf":
        return extract_pdf(path).text
    if kind == "docx":
        return extract_docx(path).text
    if kind == "audio":
        return (await extract_audio(path)).text
    if kind == "video":
        return (await extract_video(path)).text
    if kind == "image":
        return (await extract_image(path)).text
    raise _Unsupported(f"unsupported file kind: {kind!r}")


# ---------------------------------------------------------------------------
# Combined-file analyze
# ---------------------------------------------------------------------------


async def _analyze_combined_file(path: Path, *, s, app, chat_id: int) -> None:
    """Same wiring as `bot.handlers.file._dispatch_analyze_file`."""
    from unread.bot.handlers.file import _effective_preset
    from unread.files.commands import cmd_analyze_file

    language = s.locale.language or "en"
    report_language = s.locale.report_language or language
    source_language = s.locale.content_language or ""
    preset = _effective_preset(s, app, chat_id)

    await cmd_analyze_file(
        ref=str(path),
        preset=preset or None,
        prompt_file=None,
        model=None,
        filter_model=None,
        output=None,
        console_out=False,
        no_console=True,
        no_cache=False,
        max_cost=None,
        dry_run=False,
        self_check=False,
        post_to=None,
        post_saved=False,
        language=language,
        report_language=report_language,
        source_language=source_language,
        yes=True,
    )


def _effective_preset_combined(s, app, chat_id: int) -> str:
    """Avoid an import cycle: re-export the file handler's helper."""
    from unread.bot.handlers.file import _effective_preset

    return _effective_preset(s, app, chat_id)


def _label_of(item) -> str:
    from unread.bot.burst import summary_line

    return summary_line(item)


def _make_tmp_dir(s) -> Path:
    base = s.media.tmp_dir / "bot" / "combined"
    base.mkdir(parents=True, exist_ok=True)
    d = base / uuid.uuid4().hex
    d.mkdir(parents=True, exist_ok=False)
    return d
