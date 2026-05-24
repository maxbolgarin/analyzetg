"""Telegram chat / message handler.

Wraps `unread.analyzer.commands.cmd_analyze` — the same entry point the
CLI uses. cmd_analyze opens its own short-lived Telethon user client
via `tg_client(settings)`, reading the session from
`settings.telegram.session_path`. The bot's `/upload_session` flow
writes there too, so no further plumbing is needed.

Refuses up-front when no authorized user session is available, so the
operator gets a focused "send /upload_session" reply instead of a
Telethon traceback after the LLM has already burned time.
"""

from __future__ import annotations

import contextlib
import re
import time
from typing import TYPE_CHECKING

import structlog
import typer
from telethon import events

from unread.bot.confirm import RunOptions, enrich_csv
from unread.config import get_settings

if TYPE_CHECKING:
    from unread.bot.app import BotApp

log = structlog.get_logger(__name__)


# t.me link → (chat_part, msg_id) for caption-message-id extraction.
# Public form: t.me/<username>[/<msg_id>]
# Private form: t.me/c/<internal_id>/<msg_id>
_TME_PARSE = re.compile(
    r"^https?://(?:t\.me|telegram\.me)/(?P<chat>[A-Za-z0-9_]+|c/\d+)(?:/(?P<msg>\d+))?/?$",
    re.IGNORECASE,
)


async def execute(
    event: events.NewMessage.Event,
    payload: dict,
    options: RunOptions,
    *,
    app: BotApp,
    progress_msg=None,
) -> None:
    if not app.user_session_ready:
        await event.reply(
            "I don't have your Telegram user session — needed to read private "
            "chats. Send `/upload_session` and then drop your `session.sqlite` "
            "as a document (one-time setup).",
            parse_mode="md",
        )
        return

    from unread.analyzer.commands import cmd_analyze
    from unread.bot.handlers.file import _effective_preset

    s = get_settings()
    ref = payload["url"]
    preset = _effective_preset(s, app, event.chat_id)
    started = time.time()

    # Parse out a specific msg_id when the link points to a single
    # message inside a chat — the analyze pipeline uses it as the
    # window anchor (analyze from that message backward / forward
    # depending on cmd_analyze's defaults).
    from_msg: str | None = None
    if (m := _TME_PARSE.match(ref)) is not None and m.group("msg"):
        from_msg = m.group("msg")

    # Window: when the user pinned a specific message via `t.me/.../<msg>`,
    # let `cmd_analyze` use that as the anchor and walk from there. For
    # a bare `@chat` / `t.me/chat` ref, the CLI default "since the read
    # marker" usually produces nothing for bot users (they typically
    # read chats on their phone before asking the bot to summarize) —
    # surface the last N days instead, matching the CLI's
    # `default_lookback_days` setting.
    last_days: int | None = None
    if from_msg is None:
        last_days = s.sync.default_lookback_days

    if progress_msg is None:
        progress_msg = await event.reply(f"⏳ Resolving `{ref}`…")
    else:
        with contextlib.suppress(Exception):
            await progress_msg.edit(f"⏳ Resolving `{ref}`…", buttons=None)
    try:
        await progress_msg.edit("⏳ Pulling messages…")
        language = s.locale.language or "en"
        report_language = s.locale.report_language or language

        # User-toggled enrich kinds become a comma-joined extra list.
        # The CLI's `--enrich a,b,c` semantics mean: turn on a/b/c on
        # top of whatever's already enabled in settings. `cmd_analyze`
        # parses this the same way — voice/videonote stay on by default
        # via settings.enrich, and we add image/doc/link/video here.
        extra_enrich = enrich_csv(options)

        await cmd_analyze(
            ref=ref,
            thread=None,
            msg=None,
            from_msg=from_msg,
            full_history=False,
            since=None,
            until=None,
            last_days=last_days,
            last_msgs=None,
            preset=preset or None,
            prompt_file=None,
            model=None,
            filter_model=None,
            output=None,
            console_out=False,
            no_save=False,
            no_console=True,
            mark_read=False,
            no_cache=False,
            enrich=extra_enrich or None,
            yes=True,
            language=language,
            report_language=report_language,
            source_language=s.locale.content_language or "",
        )
        await progress_msg.edit("📄 Sending report…")
        await _upload_latest_tg_report(event, preset=preset, started=started)
        with contextlib.suppress(Exception):
            await progress_msg.delete()
    except typer.Exit as e:
        # `cmd_analyze` uses `typer.Exit(0)` to bail gracefully — most
        # commonly "no unread messages in this chat", but also "nothing
        # matched the time window". A 0 exit code is not an error; show
        # a friendly status. Non-zero exits surface as warnings.
        code = getattr(e, "exit_code", 0)
        if code == 0:
            with contextlib.suppress(Exception):
                await progress_msg.edit(
                    f"✓ Nothing to analyze in `{ref}` for the requested window. "
                    "Try a `t.me/<chat>/<msg>` link to anchor on a specific message.",
                )
            return
        log.warning("bot.tg_handler_typer_exit", ref=ref, exit_code=code)
        with contextlib.suppress(Exception):
            await progress_msg.edit(f"⚠️ Analyze exited with code {code}.")
    except Exception as e:
        log.exception("bot.tg_handler_failed", ref=ref)
        with contextlib.suppress(Exception):
            await progress_msg.edit(f"⚠️ {type(e).__name__}: {e}")
        raise


async def _upload_latest_tg_report(
    event: events.NewMessage.Event,
    *,
    preset: str,
    started: float,
) -> None:
    """TG-chat reports land under reports/<chat-slug>/<preset>-<stamp>.md.

    There's no single helper exposing the path for an arbitrary ref —
    cmd_analyze derives it deep inside the pipeline. Find the newest
    `.md` written since the request started, anywhere under reports/.
    """
    from unread.bot.reply import _pick_best_match, _upload_with_caption
    from unread.core.paths import reports_dir

    root = reports_dir()
    if not root.exists():
        await event.reply("⚠️ Analysis finished but reports dir is missing.")
        return
    # Skip per-source subdirs that the file/url/yt handlers own — TG
    # chat reports live at the top level (or under a chat-slug dir).
    candidates: list = []
    for p in root.rglob("*.md"):
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in {"files", "youtube", "website", "ask"}:
            continue
        if p.stat().st_mtime < started - 1:
            continue
        candidates.append(p)
    chosen = _pick_best_match(candidates, hint="", preset=preset)
    if chosen is None:
        await event.reply(
            "⚠️ Analysis finished but I couldn't find the saved report under `~/.unread/reports/`."
        )
        return
    await _upload_with_caption(event, chosen, started=started)
