"""Typer command surface for `unread bot`.

The `bot_app` Typer subgroup is constructed in :mod:`unread.cli` so it
can share the `_UnreadTyper` / `_UnreadGroup` machinery the rest of the
CLI uses. This module only owns the async command bodies — the same
split as `unread/tg/commands.py`.
"""

from __future__ import annotations

import typer
from rich.console import Console

from unread.config import get_settings
from unread.i18n import t as _t

console = Console()


async def cmd_bot_run() -> None:
    """Start the self-hosted Telegram bot in long-polling mode.

    Blocks forever (until SIGINT). Validates that the @BotFather token
    and Telegram api_id/api_hash are set before opening the first
    network connection.

    `owner_id` is auto-derived from `settings.telegram.session_path`
    when that session is present and authorized — so a deploy that
    mounts the session ahead of time doesn't need `UNREAD_BOT_OWNER_ID`
    at all. When neither a session nor an env-var owner_id is
    available, the bot has no safe allowlist and refuses to start.
    """
    from unread.bot.app import BotApp
    from unread.cli import _telegram_credentials_present

    s = get_settings()

    # Gate 1: Telegram api_id / api_hash (needed even for bot mode —
    # Telethon's MTProto layer authenticates the *application* via
    # api_id/api_hash separately from the per-account auth).
    if not _telegram_credentials_present():
        console.print(f"[red]{_t('bot_missing_tg_creds')}[/]\n[grey70]{_t('bot_missing_tg_creds_hint')}[/]")
        raise typer.Exit(1)

    # Gate 2: @BotFather token.
    if not s.bot.token:
        console.print(f"[red]{_t('bot_missing_token')}[/]\n[grey70]{_t('bot_missing_token_hint')}[/]")
        raise typer.Exit(1)

    # Gate 3: there must be SOME path to a non-zero owner_id. Two
    # acceptable shapes:
    #   - `UNREAD_BOT_OWNER_ID` is set → use it directly.
    #   - A user session file exists → BotApp will derive owner_id
    #     from it during startup (via get_me()).
    # If neither is available, the bot has no allowlist and the first
    # person to message it would otherwise become the owner (TOFU). We
    # refuse that — operator must either mount the session OR set the
    # env var as an explicit bootstrap allowlist.
    if not s.bot.owner_id and not s.telegram.session_path.exists():
        console.print(f"[red]{_t('bot_missing_owner_id')}[/]\n[grey70]{_t('bot_missing_owner_id_hint')}[/]")
        raise typer.Exit(1)

    app = BotApp(s)
    await app.run_forever()
