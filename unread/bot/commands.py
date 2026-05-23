"""Typer command surface for `unread bot`.

The `bot_app` Typer subgroup is constructed in :mod:`unread.cli` so it
can share the `_UnreadTyper` / `_UnreadGroup` machinery the rest of the
CLI uses. This module only owns the async command bodies — the same
split as `unread/tg/commands.py`.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console

from unread.config import get_settings, reset_settings
from unread.i18n import t as _t

console = Console()


def _maybe_opt_into_cwd_env_bot() -> str | None:
    """When the operator hasn't set `UNREAD_BOT_ENV_FILE` and a
    `./.env.bot` exists in CWD, point at it FOR THIS CALL ONLY.

    Returns the previous value of `UNREAD_BOT_ENV_FILE` (or None when
    unset) so the caller can restore it. We do NOT permanently mutate
    `os.environ` because the bot process spawns subprocesses (ffmpeg
    at minimum), and a leaked file path would pollute their env.

    Only `unread bot run` opts into CWD discovery — every other
    command stays strict (canonical `~/.unread/.env.bot` + explicit
    override only). This matches the user expectation that
    ``cp .env.bot.example .env.bot && unread bot run`` Just Works
    in a project checkout, while preserving the rule that a stray
    `.env.bot` in some unrelated directory never silently shadows
    real settings for non-bot commands.

    Returns the sentinel string `"__unread_sentinel_unset__"` when
    the var was unset before, so the caller can distinguish "was
    empty string" from "was missing".
    """
    if os.environ.get("UNREAD_BOT_ENV_FILE"):
        return None  # Caller already opted in; nothing to do, nothing to restore.
    cwd_candidate = Path.cwd() / ".env.bot"
    if not cwd_candidate.is_file():
        return None
    os.environ["UNREAD_BOT_ENV_FILE"] = str(cwd_candidate)
    return _UNSET_SENTINEL


_UNSET_SENTINEL = "__unread_sentinel_unset__"


def _restore_bot_env_var(previous: str | None) -> None:
    """Counterpart to `_maybe_opt_into_cwd_env_bot` — restore os.environ."""
    if previous is None:
        return
    if previous == _UNSET_SENTINEL:
        os.environ.pop("UNREAD_BOT_ENV_FILE", None)
    else:
        os.environ["UNREAD_BOT_ENV_FILE"] = previous


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

    # Opt into the CWD `.env.bot` convention BEFORE the settings
    # singleton crystallizes, so the values land in the dotenv
    # overlay rather than as too-late shell mutations. The env-var
    # mutation is restored on exit so test code (and any wrapping
    # process) doesn't see a leaked path.
    _restore = _maybe_opt_into_cwd_env_bot()
    try:
        reset_settings()
        s = get_settings()

        # Gate 1: Telegram api_id / api_hash (needed even for bot
        # mode — Telethon's MTProto layer authenticates the
        # *application* via api_id/api_hash separately from the
        # per-account auth).
        if not _telegram_credentials_present():
            console.print(
                f"[red]{_t('bot_missing_tg_creds')}[/]\n[grey70]{_t('bot_missing_tg_creds_hint')}[/]"
            )
            raise typer.Exit(1)

        # Gate 2: @BotFather token.
        if not s.bot.token:
            console.print(f"[red]{_t('bot_missing_token')}[/]\n[grey70]{_t('bot_missing_token_hint')}[/]")
            raise typer.Exit(1)

        # Gate 3: there must be SOME path to a non-zero owner_id.
        # Acceptable shapes:
        #   - `UNREAD_BOT_OWNER_ID` is set → use it directly.
        #   - A user session blob exists (on-disk SQLiteSession OR
        #     encrypted StringSession in the secrets DB) → BotApp
        #     derives owner_id from it during startup (via get_me()).
        # If neither is available, the bot has no allowlist and the
        # first person to message it would otherwise become the owner
        # (TOFU). We refuse that — operator must either expose the
        # session to the bot OR set the env var as an explicit
        # bootstrap allowlist for the upcoming `/upload_session`.
        from unread.bot.app import _has_session_blob

        if not s.bot.owner_id and not _has_session_blob(s):
            console.print(
                f"[red]{_t('bot_missing_owner_id')}[/]\n[grey70]{_t('bot_missing_owner_id_hint')}[/]"
            )
            raise typer.Exit(1)

        app = BotApp(s)
        await app.run_forever()
    finally:
        _restore_bot_env_var(_restore)
        reset_settings()
