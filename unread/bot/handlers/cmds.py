"""Trivial slash commands (`/start`, `/help`, `/ping`, `/preset`, `/cancel`).

These never call the analyze pipeline, so they bypass the worker
semaphore and reply immediately.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from telethon import events

if TYPE_CHECKING:
    from unread.bot.app import BotApp


_SLASH_COMMANDS = """\
Slash commands:
`/help` — this message
`/ping` — health check
`/settings` — show current sticky + default settings for this chat
`/preset <name>` — sticky preset (e.g. `/preset digest`); bare `/preset` clears
`/lang <code>` — sticky report language (e.g. `/lang en`); bare clears
`/enrich <list|all|none>` — sticky extra enrichments for TG chats (e.g. `image,link`)
`/window <day|week|month|msg|from_msg|none>` — sticky default TG window
`/confirm on|off` — toggle the pre-run confirm panel (default: on)
`/upload_session` — install your Telegram user session (one-time)
`/cancel` — drop any pending `/upload_session`
"""

# Telethon's default markdown parser is MarkdownV1-ish: **double**
# asterisks for bold, `backticks` for inline code. Single asterisks
# render literally — don't use them.

_HELP_TEXT_FULL = """\
**unread bot** — send me one of:
• a file (PDF, audio, video, text, code, …)
• a web URL → I'll summarize the page
• a YouTube URL → I'll summarize the transcript
• a forwarded Telegram message → I'll analyze its contents
• a `t.me/<chat>/<msg>` link → I'll pull the chat and analyze

"""

_HELP_TEXT_NO_SESSION = """\
**unread bot** — send me one of:
• a file (PDF, audio, video, text, code, …)
• a web URL → I'll summarize the page
• a YouTube URL → I'll summarize the transcript

⚠️ **No Telegram user session installed**, so I can't read your private chats. \
Forwarded messages, `t.me/<chat>/<msg>` links, and `@channel` refs won't work \
until you run `/upload_session` and send me your `session.sqlite` file.

"""


def _build_help_text(app: BotApp) -> str:
    base = _HELP_TEXT_FULL if app.user_session_ready else _HELP_TEXT_NO_SESSION
    return base + _SLASH_COMMANDS


async def handle(
    event: events.NewMessage.Event,
    payload: dict,
    *,
    app: BotApp,
) -> None:
    cmd = payload.get("name", "")
    args = payload.get("args", [])

    if cmd in ("start", "help"):
        await event.reply(_build_help_text(app), parse_mode="md")
        return

    if cmd == "ping":
        await event.reply("pong")
        return

    if cmd == "preset":
        chat_state = app._chat_state.setdefault(event.chat_id, {})
        if not args:
            chat_state.pop("preset", None)
            await event.reply("Sticky preset cleared. Falling back to the default.")
        else:
            preset = args[0].strip()
            chat_state["preset"] = preset
            await event.reply(f"Sticky preset → `{preset}` (used until you clear it).")
        return

    if cmd == "confirm":
        chat_state = app._chat_state.setdefault(event.chat_id, {})
        if not args:
            state = "off" if chat_state.get("confirm_disabled") else "on"
            await event.reply(
                f"Pre-run confirm panel is currently `{state}`. Use `/confirm on|off` to change.",
                parse_mode="md",
            )
            return
        choice = args[0].strip().lower()
        if choice == "off":
            chat_state["confirm_disabled"] = True
            await event.reply(
                "Pre-run confirm panel disabled. Messages will run immediately with sticky defaults."
            )
        elif choice == "on":
            chat_state.pop("confirm_disabled", None)
            await event.reply(
                "Pre-run confirm panel re-enabled. Each message will get a ▶ Run / ⚙ Change / ✖ Cancel panel."
            )
        else:
            await event.reply("Usage: `/confirm on` or `/confirm off`.", parse_mode="md")
        return

    if cmd == "lang":
        from unread.bot.runtime import STICKY_REPORT_LANGUAGE, parse_lang_value

        chat_state = app._chat_state.setdefault(event.chat_id, {})
        arg = args[0] if args else ""
        value, msg = parse_lang_value(arg)
        if value is None:
            await event.reply(msg)
            return
        if value:
            chat_state[STICKY_REPORT_LANGUAGE] = value
        else:
            chat_state.pop(STICKY_REPORT_LANGUAGE, None)
        await event.reply(msg)
        return

    if cmd == "enrich":
        from unread.bot.runtime import STICKY_ENRICH_EXTRAS, parse_enrich_list

        chat_state = app._chat_state.setdefault(event.chat_id, {})
        arg = " ".join(args) if args else ""
        value, msg = parse_enrich_list(arg)
        if value is None:
            await event.reply(msg)
            return
        if value:
            chat_state[STICKY_ENRICH_EXTRAS] = value
        else:
            chat_state.pop(STICKY_ENRICH_EXTRAS, None)
        await event.reply(msg)
        return

    if cmd == "window":
        from unread.bot.runtime import STICKY_TG_WINDOW, parse_window_value

        chat_state = app._chat_state.setdefault(event.chat_id, {})
        arg = args[0] if args else ""
        value, msg = parse_window_value(arg)
        if value is None:
            await event.reply(msg)
            return
        if value:
            chat_state[STICKY_TG_WINDOW] = value
        else:
            chat_state.pop(STICKY_TG_WINDOW, None)
        await event.reply(msg)
        return

    if cmd == "settings":
        from unread.bot.runtime import render_settings_overview
        from unread.config import get_settings

        chat_state = app._chat_state.get(event.chat_id) or {}
        text = render_settings_overview(chat_state, get_settings())
        await event.reply(text, parse_mode="md")
        return

    if cmd == "cancel":
        chat_state = app._chat_state.setdefault(event.chat_id, {})
        had_pending = chat_state.pop("pending_session_upload", False)
        if had_pending:
            await event.reply("Session-upload cancelled.")
        else:
            await event.reply("Nothing to cancel.")
        return

    if cmd == "upload_session":
        from unread.bot import session_upload

        await session_upload.start_upload(event, app=app)
        return

    await event.reply(f"Unknown command: /{cmd}. Try /help.")
