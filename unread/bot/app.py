"""`BotApp` — the long-running bot process.

Two-phase setup: start the bot-mode Telethon client (authed via
`bot_token`), then verify that the owner's user-mode session at
`settings.telegram.session_path` exists and is authorized. The user
client is NOT held open across the bot's lifetime — each TG-handler
invocation opens its own short-lived client through the existing
`unread.tg.client.tg_client` context manager, the same path the CLI
takes. This keeps the bot a thin layer over `cmd_analyze_*` and avoids
two-clients-one-SQLite-file lifetime headaches.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import structlog
from rich.console import Console
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from unread.config import Settings

log = structlog.get_logger(__name__)
console = Console()


class BotApp:
    """Bot-mode Telegram client + per-message dispatcher.

    The single long-running Telethon connection here is the bot client
    (authed via `bot_token`). Every TG-link analyze request opens a
    transient user-mode client via `tg_client()` for the duration of
    that one request. The bot only checks that the user session is
    *present* and authorized at startup so it can surface a focused
    error before the first request fails.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.bot.concurrency)
        self.bot_client: TelegramClient | None = None
        # True iff the owner's user session at
        # settings.telegram.session_path was authorized at startup or
        # after a successful /upload_session.
        self.user_session_ready: bool = False
        # Effective allowlist. Seeded from `settings.bot.owner_id` so
        # the env var works as a bootstrap allowlist when no session
        # is mounted yet; `_verify_user_session` overrides this with
        # the session-derived ID when available (and logs a warning
        # if the env var and the session disagree). One of:
        #   - 0 → no allowlist resolved yet (bot will refuse to wire
        #     handlers and exit).
        #   - >0 → the single Telegram user ID we serve.
        self.owner_id: int = settings.bot.owner_id
        # Per-chat ephemeral state (sticky `/preset`, pending
        # `/upload_session`, etc.). Keyed by chat_id. Reset on restart.
        self._chat_state: dict[int, dict] = {}
        # In-flight task set so a graceful shutdown can await them.
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Start the bot client, resolve the allowlist, run until SIGINT.

        User-visible status uses `console.print` (always shown, even
        at the default "normal" log mode which filters INFO). The
        parallel `log.info` calls survive for `-v / --verbose` debug
        traces.
        """
        console.print("[grey70]→ starting Telegram bot client…[/]")
        log.info("bot.startup.begin", owner_id_from_env=self.settings.bot.owner_id)
        await self._start_bot_client()
        log.info("bot.startup.bot_client_ready")

        console.print("[grey70]→ checking your Telegram user session…[/]")
        log.info("bot.startup.verifying_user_session")
        await self._verify_user_session()
        log.info(
            "bot.startup.user_session_done",
            user_session_ready=self.user_session_ready,
            owner_id=self.owner_id,
        )

        if self.owner_id == 0:
            console.print(
                "[red]Bot has no owner allowlist.[/] Set UNREAD_BOT_OWNER_ID "
                "or mount/upload an authorized user session before starting."
            )
            log.error("bot.no_owner_allowlist")
            raise RuntimeError("no owner allowlist")
        self._wire_handlers()
        session_state = (
            "[green]ready[/]"
            if self.user_session_ready
            else "[yellow]missing[/] — TG-chat analysis disabled until /upload_session"
        )
        console.print(
            f"[green]✓ bot ready[/] · owner=[cyan]{self.owner_id}[/]"
            f" · session={session_state}"
            f" · concurrency={self.settings.bot.concurrency}"
        )
        console.print("[grey70]Listening for messages. Ctrl-C to stop.[/]")
        log.info(
            "bot.ready",
            owner_id=self.owner_id,
            user_session_ready=self.user_session_ready,
            concurrency=self.settings.bot.concurrency,
        )
        # PDF availability probe deferred to first request — it spawns
        # a subprocess that on a misconfigured macOS Pango can take
        # ~10s. Keeping it off the startup path means the bot is
        # accepting messages as soon as the `bot ready` line appears.
        try:
            assert self.bot_client is not None
            await self.bot_client.run_until_disconnected()
        finally:
            await self._shutdown()

    async def _start_bot_client(self) -> None:
        """Authenticate the bot-mode Telethon client.

        Uses an in-memory `StringSession()` — the bot's session is
        regenerable from the token alone, so persisting it would just
        be one more file to mount.
        """
        s = self.settings
        client = TelegramClient(
            StringSession(),
            api_id=s.telegram.api_id,
            api_hash=s.telegram.api_hash,
        )
        await client.start(bot_token=s.bot.token)
        self.bot_client = client
        log.info("bot.client.started")

    async def _verify_user_session(self) -> None:
        """One-shot probe of the owner's user-mode session.

        Two outputs:

        * `self.user_session_ready` — gates the TG-link handler so it
          can reply "send /upload_session" instead of letting the
          first analyze attempt blow up with a confusing Telethon
          error.
        * `self.owner_id` — when the session is authorized, the
          allowlist is overridden by the session's own user ID
          (via `get_me()`). A `UNREAD_BOT_OWNER_ID` env var that
          disagrees with the session is logged as a warning; the
          session wins.

        Uses `unread.tg.client.build_client` so the bot picks up the
        same session the CLI uses regardless of backend (on-disk
        SQLite, system keychain, or passphrase-encrypted StringSession
        in the secrets DB). Does NOT keep the client connected.
        """
        if not _has_session_blob(self.settings):
            log.warning(
                "bot.user_session.missing",
                session_path=str(self.settings.telegram.session_path),
                hint=(
                    "no session file or DB blob found — send /upload_session via "
                    "Telegram, or SCP your existing session into "
                    f"{self.settings.telegram.session_path}.session "
                    "(Telethon appends `.session` to the path)."
                ),
            )
            return
        derived = await _probe_session_owner_id(self.settings)
        if derived is None:
            log.warning(
                "bot.user_session.unauthorized",
                session_path=str(self.settings.telegram.session_path),
                hint="session blob exists but isn't authorized — re-export from a logged-in host.",
            )
            return
        self.user_session_ready = True
        if self.owner_id and self.owner_id != derived:
            log.warning(
                "bot.owner_id.env_conflict",
                env_owner_id=self.owner_id,
                session_owner_id=derived,
                action="using session owner_id; ignoring env override",
            )
        self.owner_id = derived
        log.info("bot.user_session.ready", owner_id=self.owner_id)

    async def _shutdown(self) -> None:
        """Cancel in-flight tasks and disconnect the bot client cleanly."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.bot_client is not None:
            with contextlib.suppress(Exception):
                await self.bot_client.disconnect()

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def _wire_handlers(self) -> None:
        """Attach the single allowlisted-owner dispatch handler.

        Uses the resolved `self.owner_id` (env var OR session-derived,
        with session winning when both are present). `run_forever`
        already refuses to call this when `owner_id == 0`.
        """
        assert self.bot_client is not None
        assert self.owner_id != 0, "wire_handlers called without owner_id"
        owner_id = self.owner_id

        @self.bot_client.on(events.NewMessage(from_users=[owner_id]))
        async def _on_owner_message(event: events.NewMessage.Event) -> None:
            # Defense in depth — never serve a non-owner under any
            # circumstance, even if a future filter change lets one
            # past the `from_users` gate above.
            if event.sender_id != owner_id:
                return
            task = asyncio.create_task(self._handle(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _handle(self, event: events.NewMessage.Event) -> None:
        """Per-message worker. Classify → run handler → reply.

        Wrapped in a top-level try/except so a handler raising never
        kills the event loop or leaves the bot silent. The semaphore
        gates the *real work* (analyze pipeline) but not the
        classification step, so quick replies like `/help` go out
        immediately even when 2 analyses are running.
        """
        # Pending `/upload_session`: the very next document from the
        # owner is consumed by the upload state machine, never routed
        # to the file handler. Check before classification so the
        # session sqlite blob doesn't get classified as a generic file
        # and accidentally analyzed.
        chat_state = self._chat_state.get(event.chat_id) or {}
        if chat_state.get("pending_session_upload") and event.message.media is not None:
            from unread.bot import session_upload

            try:
                await session_upload.handle_uploaded_file(event, app=self)
            except Exception:
                log.exception("bot.session_upload_failed")
                await _safe_reply(event, "⚠️ Session install failed; see bot logs.")
            return

        from unread.bot.dispatcher import classify

        try:
            kind, payload = classify(event)
        except Exception:
            log.exception("bot.classify_failed")
            await _safe_reply(event, "⚠️ Couldn't read that message.")
            return

        # Quick paths — never block on the semaphore.
        if kind == "cmd":
            await self._handle_cmd(event, payload)
            return

        async with self._semaphore:
            try:
                await self._run_analysis_handler(event, kind, payload)
            except Exception as e:
                # `typer.Exit(0)` is a graceful "nothing to do" bail
                # from inside the analyze pipeline (e.g. "no unread
                # messages since your read marker"). Handlers that
                # know about it surface a friendly progress-message
                # edit; the outer catch here intentionally swallows
                # it without a "⚠️ Exit: 0" reply that would confuse
                # the user.
                if _is_clean_exit(e):
                    return
                log.exception("bot.handler_failed", kind=kind)
                await _safe_reply(event, f"⚠️ {type(e).__name__}: {e}")

    async def _handle_cmd(self, event: events.NewMessage.Event, payload: dict) -> None:
        """Trivial-reply slash commands. Imported lazily."""
        from unread.bot.handlers import cmds

        await cmds.handle(event, payload, app=self)

    async def _run_analysis_handler(self, event: events.NewMessage.Event, kind: str, payload: dict) -> None:
        """Dispatch to the kind-specific handler module.

        Imports are lazy: a bot that only ever sees web URLs never
        pulls the youtube transcript machinery into memory.
        """
        if kind == "file":
            from unread.bot.handlers import file as file_handler

            await file_handler.handle(event, payload, app=self)
        elif kind == "youtube":
            from unread.bot.handlers import youtube as yt_handler

            await yt_handler.handle(event, payload, app=self)
        elif kind == "url":
            from unread.bot.handlers import url as url_handler

            await url_handler.handle(event, payload, app=self)
        elif kind == "tg":
            from unread.bot.handlers import tg as tg_handler

            await tg_handler.handle(event, payload, app=self)
        else:
            # Should be unreachable — classifier covers every branch.
            await _safe_reply(event, f"⚠️ Unknown message kind: {kind!r}")


# ----------------------------------------------------------------------
# Module helpers (free functions; no `self` needed)
# ----------------------------------------------------------------------


def _has_session_blob(settings: Settings) -> bool:
    """Cheap, synchronous check: is there any plausible session source?

    Used by the startup gate AND `_verify_user_session` so the bot
    can decide before paying a network round-trip whether there's
    something for `build_client()` to load. Three branches mirror
    `unread.tg.client.build_client`:

    1. `db` / `keychain` backend → Telethon's SQLiteSession lives on
       disk. Telethon appends `.session` to the path you give it, so
       the actual file is `<session_path>.session`; older saves and
       the `default_session_path()` constant both write the bare
       name, so we accept either form.
    2. `passphrase` backend → session string lives in
       `data.sqlite::secrets` as `telegram.session_string`.
    """
    p = Path(settings.telegram.session_path)
    if p.exists() or Path(str(p) + ".session").exists():
        return True
    try:
        from unread.secrets_backend import (
            BACKEND_PASSPHRASE,
            read_active_backend_sync,
        )

        backend = read_active_backend_sync(settings.storage.data_path)
        if backend == BACKEND_PASSPHRASE:
            from unread.db.repo import read_data_db_secrets_sync

            secrets = read_data_db_secrets_sync(settings.storage.data_path)
            return bool(secrets.get("telegram.session_string"))
    except Exception:
        log.exception("bot.session_blob_check_failed")
    return False


async def _probe_session_owner_id(settings: Settings) -> int | None:
    """Open the user-mode client via build_client(), return `me.id` if authorized.

    Going through `build_client` is what reconciles the bot with the
    CLI: same backend resolution (db/keychain/passphrase), same path
    semantics (Telethon's `.session` suffix handling), same secrets
    DB lookup for the encrypted-session case. If the CLI can log in
    on this machine, the bot picks up the same session.

    Hard-capped at 15s total — a flaky network or wedged Telegram
    datacenter shouldn't hang bot startup forever. On timeout the
    probe behaves like "unauthorized": bot keeps running with
    `user_session_ready=False`, TG-link handlers reply "send
    /upload_session", and the operator sees a clear timeout warning.

    Returns the user's Telegram ID on success, None on missing /
    unauthorized / timeout / any error. Always disconnects.
    """
    from unread.tg.client import build_client

    try:
        client = build_client(settings)
    except SystemExit:
        # build_client calls `_exit_missing_telegram_credentials()`
        # via typer.Exit when api_id/api_hash are missing. The bot's
        # `cmd_bot_run` gate catches that earlier — but defensively
        # treat it as "no session" rather than letting it tear down
        # the bot startup.
        return None
    except Exception:
        log.exception("bot.session.build_client_failed")
        return None
    try:
        return await asyncio.wait_for(_probe_inner(client), timeout=15.0)
    except TimeoutError:
        log.warning(
            "bot.session.probe_timeout",
            hint="user-session probe took >15s; continuing without TG-chat support",
        )
        return None
    except Exception:
        log.exception("bot.session.probe_failed")
        return None
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


async def _probe_inner(client) -> int | None:
    """Inner body of `_probe_session_owner_id` — wrapped in `wait_for` above."""
    await client.connect()
    if not await client.is_user_authorized():
        return None
    me = await client.get_me()
    if me is None or not getattr(me, "id", 0):
        return None
    return int(me.id)


def _is_clean_exit(exc: BaseException) -> bool:
    """True iff `exc` is a `typer.Exit(0)` / `SystemExit(0)` graceful bail.

    Used to suppress the "⚠️ Exit: 0" reply that would otherwise fire
    when `cmd_analyze*` legitimately exits with no work to do (empty
    window, already-read chat, etc.). Non-zero exit codes still fall
    through to the warning path so a real failure stays visible.
    """
    import typer as _typer

    if isinstance(exc, _typer.Exit | SystemExit):
        return getattr(exc, "exit_code", getattr(exc, "code", None)) in (0, None)
    return False


async def _safe_reply(event: events.NewMessage.Event, text: str) -> None:
    """Reply, swallowing transport errors so the bot loop keeps running."""
    try:
        await event.reply(text)
    except Exception:
        log.exception("bot.reply_failed")
