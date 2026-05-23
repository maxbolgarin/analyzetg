"""File handler — Telegram document/photo/audio/video → cmd_analyze_file.

Owns: size-gating against `settings.bot.max_file_mb`, download to a
per-request temp dir, dispatch into the existing files pipeline,
report upload via `unread.bot.reply.send_report`, and tmp cleanup.
"""

from __future__ import annotations

import contextlib
import shutil
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
from telethon import events

from unread.config import get_settings

if TYPE_CHECKING:
    from unread.bot.app import BotApp

log = structlog.get_logger(__name__)


# Kinds we accept. Mirrors `unread/files/extractors.py` categories.
_ACCEPTED_KINDS = frozenset({"text", "pdf", "docx", "audio", "video", "image"})


async def handle(
    event: events.NewMessage.Event,
    payload: dict,
    *,
    app: BotApp,
) -> None:
    """Process a file-shaped event end-to-end.

    Branches on `payload["source"]`:
    * ``"media"`` — a TG document/photo/audio/video. Downloads via
      `bot_client.download_media`, then routes to `cmd_analyze_file`.
    * ``"text"`` — plain text from the message body. Writes to a
      `.txt` file in the per-request temp dir and routes to
      `cmd_analyze_file`. (Going through the file path rather than the
      stdin sentinel keeps the bot uncoupled from `_read_stdin_bytes`,
      which reads from `sys.stdin` of the bot process.)
    """
    s = get_settings()
    progress = await event.reply("⏳ Working…")
    started = time.time()
    tmp_dir = _make_tmp_dir()
    try:
        local_path = await _materialize_input(event, payload, tmp_dir, app=app, s=s)
        if local_path is None:
            return  # _materialize_input has already replied with the reason.
        preset = _effective_preset(s, app, event.chat_id)
        await progress.edit(f"⏳ Analyzing `{local_path.name}`…")
        await _dispatch_analyze_file(local_path, preset=preset, s=s)
        await progress.edit("📄 Sending report…")
        from unread.bot import reply

        await reply.send_file_report(
            event,
            local_path=local_path,
            preset=preset,
            started=started,
            kind=payload.get("kind", "text") if payload.get("source") == "media" else "text",
        )
        with contextlib.suppress(Exception):
            await progress.delete()
    except Exception as e:
        log.exception("bot.file_handler_failed")
        with contextlib.suppress(Exception):
            await progress.edit(f"⚠️ {type(e).__name__}: {e}")
        raise
    finally:
        # Drop the downloaded payload — the report has been delivered;
        # keeping the raw file would just grow the bot's disk.
        with contextlib.suppress(Exception):
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ----------------------------------------------------------------------
# Input materialization
# ----------------------------------------------------------------------


async def _materialize_input(
    event: events.NewMessage.Event,
    payload: dict,
    tmp_dir: Path,
    *,
    app: BotApp,
    s,
) -> Path | None:
    """Resolve the payload to a local Path. Reply + return None on refusal."""
    source = payload.get("source", "")
    if source == "text":
        text = (payload.get("text") or "").strip()
        if not text:
            await event.reply("Empty message — nothing to analyze.")
            return None
        path = tmp_dir / "message.txt"
        path.write_text(text, encoding="utf-8")
        return path

    if source != "media":
        await event.reply(f"Unsupported source: {source!r}")
        return None

    kind = payload.get("kind", "unknown")
    if kind == "unknown":
        await event.reply(
            "I don't know how to handle this attachment. Supported: "
            "PDF, DOCX, audio, video, images, text/code files."
        )
        return None
    if kind not in _ACCEPTED_KINDS:
        await event.reply(f"Unsupported file kind: {kind!r}")
        return None

    size_bytes = payload.get("size")
    max_bytes = s.bot.max_file_mb * 1_000_000
    if isinstance(size_bytes, int) and size_bytes > max_bytes:
        await event.reply(
            f"File is {size_bytes / 1_000_000:.1f} MB — over the "
            f"{s.bot.max_file_mb} MB bot limit. Send a smaller file "
            "or raise `bot.max_file_mb` in config.toml."
        )
        return None

    assert app.bot_client is not None
    target = tmp_dir / payload.get("name", "attachment")
    # `download_media` is happy taking the message object; using it
    # directly (instead of `payload["media"]`) covers both photo and
    # document branches without extra plumbing.
    downloaded = await app.bot_client.download_media(event.message, file=str(target))
    if downloaded is None:
        await event.reply("Download failed (no data returned).")
        return None
    return Path(downloaded)


# ----------------------------------------------------------------------
# Pipeline call
# ----------------------------------------------------------------------


async def _dispatch_analyze_file(local_path: Path, *, preset: str, s) -> None:
    """Invoke `cmd_analyze_file` with bot-appropriate defaults."""
    from unread.files.commands import cmd_analyze_file

    language = s.locale.language or "en"
    report_language = s.locale.report_language or language
    source_language = s.locale.content_language or ""

    await cmd_analyze_file(
        ref=str(local_path),
        preset=preset or None,
        prompt_file=None,
        model=None,
        filter_model=None,
        output=None,  # let file_report_path pick the canonical location
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


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _make_tmp_dir() -> Path:
    """Per-request unique tmp dir under the bot's media tree."""
    s = get_settings()
    base = s.media.tmp_dir / "bot"
    base.mkdir(parents=True, exist_ok=True)
    # uuid4 for collision safety; per-request dir means cleanup is one
    # rmtree away with no worry about other handlers.
    d = base / uuid.uuid4().hex
    d.mkdir(parents=True, exist_ok=False)
    return d


def _effective_preset(s, app: BotApp, chat_id: int) -> str:
    """Sticky `/preset` for this chat, else bot default, else empty (analyzer default)."""
    chat_state = app._chat_state.get(chat_id) or {}
    sticky = (chat_state.get("preset") or "").strip()
    if sticky:
        return sticky
    return (s.bot.default_preset or "").strip()
