"""Self-hosted Telegram bot frontend for `unread`.

The bot is a thin Telegram-side adapter: it forwards files/URLs/YouTube/
forwarded-TG-messages into the existing `cmd_analyze_*` async pipelines
and uploads the resulting Markdown report back as a TG document.

Single-user by design: every event whose sender is not
`settings.bot.owner_id` is silently dropped. To read the owner's private
Telegram chats (which a bot_token cannot do), the bot also loads the
owner's already-bootstrapped Telethon user session and runs a second
client side-by-side in the same asyncio loop.

See the architecture map in CLAUDE.md ("Bot") for the full layout.
"""

from __future__ import annotations
