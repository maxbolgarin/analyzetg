"""YouTube URL handler — `cmd_analyze_youtube` wrapper."""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING

import structlog
from telethon import events

from unread.bot.confirm import RunOptions
from unread.bot.progress import edit_progress
from unread.config import get_settings

if TYPE_CHECKING:
    from unread.bot.app import BotApp

log = structlog.get_logger(__name__)


async def execute(
    event: events.NewMessage.Event,
    payload: dict,
    options: RunOptions,
    *,
    app: BotApp,
    progress_msg=None,
) -> None:
    from unread.bot.handlers.file import _effective_preset
    from unread.bot.runtime import (
        effective_language,
        effective_report_language,
        effective_source_language,
    )
    from unread.youtube.commands import cmd_analyze_youtube
    from unread.youtube.urls import extract_video_id

    s = get_settings()
    url = payload["url"]
    chat_state = app._chat_state.get(event.chat_id) or {}
    preset = _effective_preset(s, app, event.chat_id)
    started = time.time()

    try:
        video_id = extract_video_id(url)
    except ValueError as e:
        await event.reply(f"⚠️ Not a recognizable YouTube video URL: {e}")
        return

    if progress_msg is None:
        progress_msg = await event.reply(f"⏳ Pulling transcript for `{video_id}`…")
    else:
        await edit_progress(progress_msg, f"⏳ Pulling transcript for `{video_id}`…")
    try:
        await edit_progress(progress_msg, "⏳ Analyzing video…")
        language = effective_language(chat_state, s)
        report_language = effective_report_language(chat_state, s)
        await cmd_analyze_youtube(
            url=url,
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
            cite_context=0,
            post_to=None,
            post_saved=False,
            language=language,
            report_language=report_language,
            source_language=effective_source_language(chat_state, s),
            youtube_source=options.youtube_source or "auto",
            yes=True,
        )
        await edit_progress(progress_msg, "📄 Sending report…")
        from unread.bot import reply

        await reply.send_youtube_report(event, preset=preset, started=started, hint=video_id)
        with contextlib.suppress(Exception):
            await progress_msg.delete()
    except Exception as e:
        log.exception("bot.youtube_handler_failed", url=url)
        await edit_progress(progress_msg, f"⚠️ {type(e).__name__}: {e}")
        raise
