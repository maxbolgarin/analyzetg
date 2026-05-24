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
from unread.bot.progress import edit_progress
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
    from unread.bot.runtime import (
        effective_preset,
        effective_report_language,
        effective_source_language,
        resolve_options,
    )

    s = get_settings()
    ref = payload["url"]
    chat_state = app._chat_state.get(event.chat_id) or {}
    preset = effective_preset(chat_state, s)
    # Sticky `/window` and `/enrich` defaults get folded into the
    # picker's per-run choices so a no-tap run uses what the user has
    # already configured for this chat.
    options = resolve_options(chat_state=chat_state, settings=s, options=options)
    started = time.time()

    # Parsed once, consumed by both the explicit-window branch and the
    # legacy default branch below.
    parsed_msg: str | None = None
    if (m := _TME_PARSE.match(ref)) is not None and m.group("msg"):
        parsed_msg = m.group("msg")

    # Window selection priority:
    #   1. options.tg_window — set by the TG-link choice panel.
    #   2. legacy default — use msg as from_msg anchor; bare chat
    #      falls back to `s.sync.default_lookback_days`.
    msg: str | None = None
    from_msg: str | None = None
    last_days: int | None = None
    last_msgs: int | None = None

    window = options.tg_window
    if window == "msg":
        msg = parsed_msg
    elif window == "from_msg":
        from_msg = parsed_msg
    elif window == "1d":
        last_days = 1
    elif window == "7d":
        last_days = 7
    elif window == "30d":
        last_days = 30
    else:
        # Legacy default — preserves today's behavior for bursts
        # that never touched the choice panel.
        from_msg = parsed_msg
        if from_msg is None:
            last_days = s.sync.default_lookback_days

    if progress_msg is None:
        progress_msg = await event.reply(f"⏳ Resolving `{ref}`…")
    else:
        await edit_progress(progress_msg, f"⏳ Resolving `{ref}`…")
    try:
        await edit_progress(progress_msg, _pulling_status(window, parsed_msg))
        language = s.locale.language or "en"
        report_language = effective_report_language(chat_state, s)

        # User-toggled enrich kinds become a comma-joined extra list.
        # The CLI's `--enrich a,b,c` semantics mean: turn on a/b/c on
        # top of whatever's already enabled in settings. `cmd_analyze`
        # parses this the same way — voice/videonote stay on by default
        # via settings.enrich, and we add image/doc/link/video here.
        extra_enrich = enrich_csv(options)

        await cmd_analyze(
            ref=ref,
            thread=None,
            msg=msg,
            from_msg=from_msg,
            full_history=False,
            since=None,
            until=None,
            last_days=last_days,
            last_msgs=last_msgs,
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
            source_language=effective_source_language(chat_state, s),
        )
        await edit_progress(progress_msg, "📄 Sending report…")
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
            await edit_progress(
                progress_msg,
                f"✓ Nothing to analyze in `{ref}` for the requested window. "
                "The chat / topic may be quiet — try a longer window "
                "(Last week / Last month).",
            )
            return
        log.warning("bot.tg_handler_typer_exit", ref=ref, exit_code=code)
        await edit_progress(progress_msg, f"⚠️ Analyze exited with code {code}.")
    except Exception as e:
        log.exception("bot.tg_handler_failed", ref=ref)
        await edit_progress(progress_msg, f"⚠️ {type(e).__name__}: {e}")
        raise


def _pulling_status(window: str | None, parsed_msg: str | None) -> str:
    """Status text shown while `cmd_analyze` is pulling messages.

    Translates the chosen window into something concrete the user can
    read in the chat, instead of a generic "Pulling messages…".
    """
    if window == "msg":
        return f"⏳ Pulling message `{parsed_msg or '?'}`…"
    if window == "from_msg":
        return f"⏳ Pulling messages from `{parsed_msg or '?'}` onward…"
    if window == "1d":
        return "⏳ Pulling messages from the last day…"
    if window == "7d":
        return "⏳ Pulling messages from the last week…"
    if window == "30d":
        return "⏳ Pulling messages from the last month…"
    if parsed_msg:
        return f"⏳ Pulling messages around `{parsed_msg}`…"
    return "⏳ Pulling recent messages…"


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
