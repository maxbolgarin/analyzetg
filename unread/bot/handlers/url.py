"""Web URL handler — `cmd_analyze_website` wrapper."""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING

import structlog
from telethon import events

from unread.config import get_settings

if TYPE_CHECKING:
    from unread.bot.app import BotApp

log = structlog.get_logger(__name__)


async def handle(
    event: events.NewMessage.Event,
    payload: dict,
    *,
    app: BotApp,
) -> None:
    from unread.bot.handlers.file import _effective_preset
    from unread.website.commands import cmd_analyze_website
    from unread.website.urls import normalize_url, page_id

    s = get_settings()
    url = payload["url"]
    preset = _effective_preset(s, app, event.chat_id)
    started = time.time()

    progress = await event.reply(f"⏳ Fetching `{url}`…")
    try:
        await progress.edit("⏳ Analyzing page…")
        language = s.locale.language or "en"
        report_language = s.locale.report_language or language
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
            source_language=s.locale.content_language or "",
            yes=True,
        )
        await progress.edit("📄 Sending report…")
        from unread.bot import reply

        # page_id is a 16-char hex prefix the report filename embeds.
        hint = page_id(normalize_url(url))
        await reply.send_website_report(event, preset=preset, started=started, hint=hint)
        with contextlib.suppress(Exception):
            await progress.delete()
    except Exception as e:
        log.exception("bot.url_handler_failed", url=url)
        with contextlib.suppress(Exception):
            await progress.edit(f"⚠️ {type(e).__name__}: {e}")
        raise
