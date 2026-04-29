"""Telethon client wrapper and helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from telethon import TelegramClient

from unread.config import Settings, get_settings
from unread.util.logging import get_logger

log = get_logger(__name__)


def _chat_kind(entity) -> str:
    """Classify a Telethon entity."""
    # Imported lazily so tests can import this module without Telethon types at top.
    from telethon.tl.types import Channel, Chat, User  # type: ignore[attr-defined]

    if isinstance(entity, User):
        return "user"
    if isinstance(entity, Chat):
        return "group"
    if isinstance(entity, Channel):
        if getattr(entity, "forum", False):
            return "forum"
        if getattr(entity, "megagroup", False):
            return "supergroup"
        return "channel"
    return "user"


def entity_title(entity) -> str | None:
    """Best-effort display title for any entity kind."""
    title = getattr(entity, "title", None)
    if title:
        return title
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    full = f"{first} {last}".strip()
    if full:
        return full
    uname = getattr(entity, "username", None)
    return f"@{uname}" if uname else None


def entity_username(entity) -> str | None:
    return getattr(entity, "username", None)


def entity_id(entity) -> int:
    """Return the canonical chat_id, including -100 prefix for channels."""
    from telethon.utils import get_peer_id  # type: ignore[attr-defined]

    return get_peer_id(entity)


class TelegramSessionExpired(RuntimeError):
    """Raised when Telethon reports the local session is unauthorized.

    Propagated up to command boundaries (`cli._dispatch_analyze`,
    `cmd_dump`, `cmd_sync`, the runner, etc.) where it's converted into
    a friendly banner + ``typer.Exit(1)``. Defined as its own subclass
    so command boundaries can catch *only* this case without swallowing
    unrelated runtime errors.
    """


def _exit_missing_telegram_credentials() -> None:
    """Show a friendly first-run banner instead of Telethon's raw ValueError.

    Catches the common "fresh install / never logged in" case at the one
    chokepoint every Telegram-using command flows through (`build_client`).
    Without this, commands like `describe`, `sync`, `dump @user`, the
    wizard, etc. crash with an unhelpful Telethon traceback.

    Delegates to `cli._print_first_run_banner` for the exact copy so
    every Telegram-missing path (root analyze gate, individual subcommands,
    interactive wizard) shows identical text.
    """
    import typer

    from unread.cli import _print_first_run_banner

    _print_first_run_banner("telegram")
    raise typer.Exit(1)


def exit_session_expired() -> None:
    """Friendly exit for the "session file present but unauthorized" path.

    Distinct from `_exit_missing_telegram_credentials` — that fires when
    api_id/hash are blank, which happens before any session file exists.
    This fires when api_id/hash are populated but Telethon refuses to
    authorize (token revoked from another device, account banned,
    session corrupted, password change). The fix in both cases is the
    same wizard, but the copy needs to differ so the user knows it's a
    re-auth, not a fresh setup.
    """
    import typer
    from rich.console import Console

    from unread.i18n import t as _t

    console = Console()
    console.print(f"[bold yellow]{_t('tg_session_expired_title')}[/]")
    console.print(_t("tg_session_expired_hint"))
    raise typer.Exit(1)


def build_client(settings: Settings | None = None) -> TelegramClient:
    s = settings or get_settings()
    if not s.telegram.api_id or not s.telegram.api_hash:
        _exit_missing_telegram_credentials()
    s.telegram.session_path.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        str(s.telegram.session_path),
        api_id=s.telegram.api_id,
        api_hash=s.telegram.api_hash,
    )


@asynccontextmanager
async def tg_client(
    settings: Settings | None = None, require_auth: bool = True
) -> AsyncIterator[TelegramClient]:
    """Async context manager that connects and, optionally, enforces auth.

    Raises :class:`TelegramSessionExpired` (a `RuntimeError` subclass) when
    `require_auth=True` and the local session is not authorized. Command
    boundaries catch that and emit `exit_session_expired()`.
    """
    client = build_client(settings)
    await client.connect()
    try:
        if require_auth and not await client.is_user_authorized():
            raise TelegramSessionExpired("Telegram session is not authorized. Run `unread tg init --force`.")
        yield client
    finally:
        await client.disconnect()
