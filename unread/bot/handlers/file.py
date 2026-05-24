"""File handler — Telegram document/photo/audio/video → cmd_analyze_file.

Owns: size-gating against `settings.bot.max_file_mb`, download to a
per-request temp dir, dispatch into the existing files pipeline,
report upload via `unread.bot.reply.send_report`, and tmp cleanup.

`execute` is the only public entry point — called from the bot's
batch dispatch (Run separately) and from the confirm-disabled
fast path. The per-message confirm panel went away when bursts
landed; the batch panel in `unread.bot.burst` covers everything.
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

from unread.bot.confirm import RunOptions
from unread.bot.progress import edit_progress
from unread.config import get_settings

if TYPE_CHECKING:
    from unread.bot.app import BotApp

log = structlog.get_logger(__name__)


# Kinds we accept. Mirrors `unread/files/extractors.py` categories.
_ACCEPTED_KINDS = frozenset({"text", "pdf", "docx", "audio", "video", "image"})


async def execute(
    event: events.NewMessage.Event,
    payload: dict,
    options: RunOptions,
    *,
    app: BotApp,
    progress_msg=None,
) -> None:
    """Process a file-shaped event end-to-end.

    `progress_msg` is the message handle used for status edits — when
    None, a fresh one is created via `event.reply`. When called from the
    callback handler the panel message is passed in so the user sees a
    single status line instead of a panel + a new progress message.
    """
    s = get_settings()
    if progress_msg is None:
        progress_msg = await event.reply("⏳ Working…")
    else:
        await edit_progress(progress_msg, "⏳ Working…")
    started = time.time()
    tmp_dir = _make_tmp_dir()
    try:
        local_path = await _materialize_input(event, payload, tmp_dir, app=app, s=s)
        if local_path is None:
            return  # _materialize_input has already replied with the reason.

        # When the bot received a forwarded photo + caption (or any
        # media + caption), combine the image's vision extract with
        # the caption text before analyzing. Otherwise the caption
        # gets silently dropped — the file path only sees the raw
        # media.
        caption = (payload.get("caption") or "").strip()
        combined_to_text = False
        if caption and payload.get("source") == "media":
            await edit_progress(progress_msg, f"⏳ Extracting `{local_path.name}` + caption…")
            local_path = await _combine_media_with_caption(local_path, caption=caption, tmp_dir=tmp_dir)
            # `_combine_media_with_caption` may have folded the image
            # vision-extract into a `.txt`. Track that so the report
            # lookup goes to reports/files/text/, not reports/files/<original kind>/.
            combined_to_text = local_path.suffix.lower() == ".txt"

        from unread.bot.runtime import effective_preset_for_kind

        chat_state = app._chat_state.get(event.chat_id) or {}
        # Smart default: a single file/voice/image is one document, not a
        # discussion — fall through to `single_msg` instead of "summary"
        # when no sticky / config preset is set.
        preset = effective_preset_for_kind(chat_state, s, "file")
        await edit_progress(progress_msg, f"⏳ Analyzing `{local_path.name}`…")
        await _dispatch_analyze_file(local_path, preset=preset, s=s, chat_state=chat_state)
        await edit_progress(progress_msg, "📄 Sending report…")
        from unread.bot import reply

        # cmd_analyze_file writes the report under reports/files/<kind>/,
        # where <kind> is detected from the actual file extension. When
        # we combined media+caption into a .txt above, the report lives
        # under reports/files/text/ regardless of the original media kind.
        if combined_to_text:
            report_kind = "text"
        elif payload.get("source") == "media":
            report_kind = payload.get("kind", "text")
        else:
            report_kind = "text"

        await reply.send_file_report(
            event,
            local_path=local_path,
            preset=preset,
            started=started,
            kind=report_kind,
        )
        with contextlib.suppress(Exception):
            await progress_msg.delete()
    except Exception as e:
        log.exception("bot.file_handler_failed")
        await edit_progress(progress_msg, f"⚠️ {type(e).__name__}: {e}")
        raise
    finally:
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
# Caption-aware extraction (forwarded media + text)
# ----------------------------------------------------------------------


async def _combine_media_with_caption(local_path: Path, *, caption: str, tmp_dir: Path) -> Path:
    """Extract text from `local_path`, prepend a caption section, write to .txt.

    Used when a forwarded message arrived with media + caption. The
    raw image / pdf / video alone would lose the caption text; passing
    a combined `.txt` to `cmd_analyze_file` keeps both in the analysis.

    Falls back to the original media path if extraction fails — better
    to analyze the media alone than to error out.
    """
    from unread.files.extractors import (
        detect_kind,
        extract_audio,
        extract_docx,
        extract_image,
        extract_pdf,
        extract_text,
        extract_video,
    )

    try:
        kind = detect_kind(local_path)
        if kind == "image":
            extracted = (await extract_image(local_path)).text
        elif kind == "audio":
            extracted = (await extract_audio(local_path)).text
        elif kind == "video":
            extracted = (await extract_video(local_path)).text
        elif kind == "pdf":
            extracted = extract_pdf(local_path).text
        elif kind == "docx":
            extracted = extract_docx(local_path).text
        elif kind == "text":
            extracted = extract_text(local_path).text
        else:
            log.warning("bot.combine_caption.unknown_kind", path=str(local_path))
            return local_path
    except Exception:
        log.exception("bot.combine_caption.extract_failed", path=str(local_path))
        return local_path

    combined_path = tmp_dir / f"{local_path.stem}_with_caption.txt"
    combined_path.write_text(
        f"# Caption\n\n{caption.strip()}\n\n# Media content\n\n{extracted.strip()}\n",
        encoding="utf-8",
    )
    return combined_path


# ----------------------------------------------------------------------
# Pipeline call
# ----------------------------------------------------------------------


async def _dispatch_analyze_file(local_path: Path, *, preset: str, s, chat_state: dict | None = None) -> None:
    """Invoke `cmd_analyze_file` with bot-appropriate defaults."""
    from unread.bot.runtime import (
        effective_language,
        effective_report_language,
        effective_source_language,
    )
    from unread.files.commands import cmd_analyze_file

    cs = chat_state or {}
    language = effective_language(cs, s)
    report_language = effective_report_language(cs, s)
    source_language = effective_source_language(cs, s)

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
