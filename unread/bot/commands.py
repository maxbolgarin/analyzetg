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

    Blocks forever (until SIGINT). Validates that the @BotFather token,
    owner_id, and Telegram api_id/api_hash are all set before opening
    the first network connection — operator gets a focused banner
    instead of a Telethon stack trace if something's missing.
    """
    from unread.bot.app import BotApp
    from unread.cli import _telegram_credentials_present

    s = get_settings()

    # Gate 1: Telegram api_id / api_hash (needed even for bot mode —
    # Telethon's MTProto layer authenticates the *application* via
    # api_id/api_hash separately from the per-account auth).
    if not _telegram_credentials_present():
        console.print(
            f"[red]{_t('bot_missing_tg_creds')}[/]\n"
            f"[grey70]{_t('bot_missing_tg_creds_hint')}[/]"
        )
        raise typer.Exit(1)

    # Gate 2: @BotFather token.
    if not s.bot.token:
        console.print(
            f"[red]{_t('bot_missing_token')}[/]\n"
            f"[grey70]{_t('bot_missing_token_hint')}[/]"
        )
        raise typer.Exit(1)

    # Gate 3: owner_id allowlist. A zero owner_id would mean "accept
    # nobody" (and silently). Refuse to start in that state — the
    # operator definitely meant to set it.
    if not s.bot.owner_id:
        console.print(
            f"[red]{_t('bot_missing_owner_id')}[/]\n"
            f"[grey70]{_t('bot_missing_owner_id_hint')}[/]"
        )
        raise typer.Exit(1)

    app = BotApp(s)
    await app.run_forever()
