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
`/preset <name>` — sticky preset for the next analyses in this chat
`/preset` — clear the sticky preset
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
