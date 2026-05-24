"""Web URL handler — `cmd_analyze_website` wrapper."""

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
    from unread.website.commands import cmd_analyze_website
    from unread.website.urls import normalize_url, page_id

    s = get_settings()
    url = payload["url"]
    chat_state = app._chat_state.get(event.chat_id) or {}
    preset = _effective_preset(s, app, event.chat_id)
    started = time.time()

    if progress_msg is None:
        progress_msg = await event.reply(f"⏳ Fetching `{url}`…")
    else:
        await edit_progress(progress_msg, f"⏳ Fetching `{url}`…")
    try:
        await edit_progress(progress_msg, "⏳ Analyzing page…")
        language = effective_language(chat_state, s)
        report_language = effective_report_language(chat_state, s)
        await cmd_analyze_website(
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
            post_to=None,
            post_saved=False,
            language=language,
            report_language=report_language,
            source_language=effective_source_language(chat_state, s),
            yes=True,
        )
        await edit_progress(progress_msg, "📄 Sending report…")
        from unread.bot import reply

        # page_id is a 16-char hex prefix the report filename embeds.
        hint = page_id(normalize_url(url))
        await reply.send_website_report(event, preset=preset, started=started, hint=hint)
        with contextlib.suppress(Exception):
            await progress_msg.delete()
    except Exception as e:
        log.exception("bot.url_handler_failed", url=url)
        await edit_progress(progress_msg, f"⚠️ {type(e).__name__}: {e}")
        raise
