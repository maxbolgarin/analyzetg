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

    Also wipes the local session BEFORE printing — without this, the
    `auth_key`-based status check in `is_session_authorized_sync` would
    keep reading the stale-but-non-empty file and report "session
    linked" on the very next `unread help` invocation, contradicting
    the banner we just showed. The user has to re-auth anyway, so
    pre-clearing matches what `unread tg login --force` would do.
    """
    import typer
    from rich.console import Console

    from unread.i18n import t as _t

    _wipe_local_session()
    console = Console()
    console.print(f"[bold yellow]{_t('tg_session_expired_title')}[/]")
    console.print(_t("tg_session_expired_hint"))
    raise typer.Exit(1)


async def offer_inline_tg_init(reason: str) -> bool:
    """Offer to run `unread tg login` inline when a TG-needing command is blocked.

    Returns ``True`` iff init ran to completion (caller should retry the
    failing operation). Returns ``False`` iff the user declined; the
    caller is expected to surface its original exit path. Caller is
    responsible for the non-TTY check — this helper assumes a real
    terminal is available, so it can be called from any retry loop
    that already knows interactive prompts are safe.

    `reason`:
      - ``"missing_creds"`` — api_id / api_hash are blank. No inline
        confirm: ``cmd_init``'s own ``_run_telegram_creds_step`` asks
        "Set up Telegram login now? (y/N)" as the single decline gate,
        so a second prompt here would just make the user press y twice.
      - ``"session_expired"`` — creds present, on-disk session
        unauthorized (revoked / corrupted / password rotated). Confirm
        before proceeding because nothing else gates the re-auth phone
        prompt.

    On accept, the function:

    1. Wipes the local session for ``"session_expired"`` (init's
       "session already valid" short-circuit would skip re-auth otherwise).
    2. Runs ``cmd_init(scope="telegram_only")`` — same wizard
       ``unread tg login`` uses, so persistence is identical.
    3. Drops the settings singleton so the caller's retry picks up
       freshly-written api_id / api_hash / session.

    The retry coupling lives in ``tg_client``'s single one-shot loop,
    not at every call site, so command code doesn't need to know about
    inline init at all.
    """
    from rich.console import Console

    from unread.config import reset_settings

    console = Console()
    if reason == "missing_creds":
        console.print("[yellow]Telegram is not configured (api_id / api_hash missing).[/]")
        # No confirm here — the wizard's first step asks for consent.
    else:
        from unread.util.prompt import confirm

        console.print("[yellow]Telegram session expired or invalid.[/]")
        if not confirm("Run `unread tg login` now and continue?", default=True):
            return False

    # Deferred import to break the tg.client ↔ tg.commands cycle —
    # tg.commands imports build_client from this module at top of file.
    from unread.tg.commands import cmd_init

    if reason == "session_expired":
        _wipe_local_session()
    await cmd_init(scope="telegram_only")
    reset_settings()
    return True


def _wipe_local_session() -> None:
    """Clear whichever session storage the active backend uses.

    For the file-based backends (`db` / `keychain`) this unlinks both
    Telethon session-file variants. For the passphrase backend it
    clears the encrypted ``telegram.session_string`` slot in the
    secrets table. Best-effort: any IO / keychain failure is logged
    and swallowed so the friendly banner still gets printed.
    """
    import contextlib
    from pathlib import Path

    from unread.secrets_backend import BACKEND_PASSPHRASE, read_active_backend_sync

    try:
        s = get_settings()
    except Exception as e:
        log.warning("tg.session_wipe_settings_failed", err=str(e)[:200])
        return

    backend = read_active_backend_sync(s.storage.data_path)
    if backend == BACKEND_PASSPHRASE:
        try:
            import sqlite3

            conn = sqlite3.connect(s.storage.data_path)
            try:
                conn.execute(
                    "DELETE FROM secrets WHERE key = ?",
                    ("telegram.session_string",),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            log.warning("tg.session_wipe_passphrase_failed", err=str(e)[:200])
        return

    session_path = Path(s.telegram.session_path)
    for candidate in (session_path, session_path.with_name(session_path.name + ".session")):
        with contextlib.suppress(FileNotFoundError):
            candidate.unlink()
        # Telethon writes a `-journal` sidecar during transactions; clean
        # it up too so a future status read can't accidentally resurrect
        # the auth_key from a half-applied write.
        for sidecar_suffix in ("-journal", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError, OSError):
                Path(str(candidate) + sidecar_suffix).unlink()


def build_client(settings: Settings | None = None) -> TelegramClient:
    """Construct a Telethon client using the active session backend.

    Two paths:

    * ``passphrase`` backend → :class:`telethon.sessions.StringSession`
      loaded from the encrypted ``telegram.session_string`` row in
      ``data.sqlite::secrets``. There is no plaintext on-disk session
      file in this mode — the whole point.
    * ``db`` / ``keychain`` (default) → on-disk
      :class:`telethon.sessions.SQLiteSession` at ``session_path``.
      Same as the pre-Phase-3 behavior.
    """
    s = settings or get_settings()
    if not s.telegram.api_id or not s.telegram.api_hash:
        _exit_missing_telegram_credentials()

    from unread.secrets_backend import BACKEND_PASSPHRASE, read_active_backend_sync

    backend = read_active_backend_sync(s.storage.data_path)
    if backend == BACKEND_PASSPHRASE:
        from telethon.sessions import StringSession

        from unread.security.passphrase import read_session_string_sync

        session_str = read_session_string_sync(s.storage.data_path)
        client = TelegramClient(
            StringSession(session_str),
            api_id=s.telegram.api_id,
            api_hash=s.telegram.api_hash,
        )
        # Stash the original string so `tg_client` can detect a
        # session-state change post-connect and re-encrypt only when
        # something actually rotated. Telethon doesn't expose a clean
        # "dirty" flag, so a snapshot diff is the simplest signal.
        client._unread_session_str_at_load = session_str  # type: ignore[attr-defined]
        return client

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

    Auto-init: in interactive shells, the first time we hit a missing
    credentials / unauthorized session per command invocation we offer
    to run ``unread tg login`` inline. If the user accepts and init
    succeeds, we retry the connection once with the freshly written
    creds / session. Non-TTY environments skip the offer and behave
    exactly as before — see :func:`offer_inline_tg_init`.
    """
    from unread.util.prompt import _can_interact

    can_offer_init = _can_interact()
    s_param = settings  # may be None; will be re-resolved each attempt
    client: TelegramClient | None = None
    s: Settings | None = None
    for attempt in range(2):
        # Pre-flight: catch missing creds before `build_client` exits
        # the process, so we can offer inline init instead. Skip the
        # offer entirely in non-TTY environments (CI / piped runs /
        # tests) — those keep the original exit path so behavior +
        # exit codes stay identical to the pre-auto-init release.
        s = s_param or get_settings()
        if not s.telegram.api_id or not s.telegram.api_hash:
            if attempt == 1 or not can_offer_init:
                _exit_missing_telegram_credentials()  # raises typer.Exit
            if not await offer_inline_tg_init("missing_creds"):
                _exit_missing_telegram_credentials()
            s_param = None  # force re-read after init
            continue
        try:
            client = build_client(s)
            await client.connect()
            if require_auth and not await client.is_user_authorized():
                # Disconnect before raising so we don't leak the
                # connection (no `yield` happened, so the caller's
                # `finally` block can't clean up for us).
                await client.disconnect()
                client = None
                raise TelegramSessionExpired(
                    "Telegram session is not authorized. Run `unread tg login --force`."
                )
            break  # success
        except TelegramSessionExpired:
            if attempt == 1 or not can_offer_init:
                # Preserve the original contract — `_run` in cli.py
                # catches this and renders the friendly banner.
                raise
            if not await offer_inline_tg_init("session_expired"):
                # User declined the inline offer. Fall back to the
                # historical exit path so the banner still prints.
                exit_session_expired()
            s_param = None
            continue

    assert client is not None and s is not None  # invariant from the loop
    # Telethon writes the session file on connect; it's auth-equivalent
    # so the file mode matters as much as the DB. Telethon respects
    # umask, so on a 022 system we'd land at 0o644 — readable by every
    # other local user. Tighten on every connect (idempotent).
    from unread.util.fsmode import tighten

    session_path = s.telegram.session_path
    for candidate in (session_path, session_path.with_suffix(session_path.suffix + ".session")):
        if candidate.exists():
            tighten(candidate)
    try:
        yield client
    finally:
        # Encrypted-mode persistence: if Telethon rotated the auth
        # key during the session, snapshot the new StringSession and
        # store it back as ciphertext. We compare against the
        # at-load snapshot to skip re-encrypt on the no-change path
        # (which is most invocations — Telethon only rotates
        # occasionally).
        loaded_at_start = getattr(client, "_unread_session_str_at_load", None)
        if loaded_at_start is not None:
            try:
                from telethon.sessions import StringSession

                if isinstance(client.session, StringSession):
                    current = client.session.save() or ""
                    if current and current != loaded_at_start:
                        from unread.security.passphrase import write_session_string_async

                        await write_session_string_async(s.storage.data_path, current)
            except Exception as e:
                # Failing to persist a rotated session is a degraded
                # state but not fatal — the next start will re-handshake
                # off the previous session. Log and move on.
                log.warning("tg.session_persist_failed", err=str(e)[:200])
        await client.disconnect()
