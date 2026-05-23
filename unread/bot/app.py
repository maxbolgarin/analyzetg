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
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from unread.config import Settings

log = structlog.get_logger(__name__)


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
        """Start the bot client, resolve the allowlist, run until SIGINT."""
        await self._start_bot_client()
        await self._verify_user_session()
        if self.owner_id == 0:
            # No env var AND no usable session — refuse to wire
            # handlers. Without an allowlist the first message would
            # establish trust-on-first-use; we never want that for a
            # token that could leak.
            log.error("bot.no_owner_allowlist")
            raise RuntimeError(
                "Bot has no owner allowlist: set UNREAD_BOT_OWNER_ID or "
                "mount/upload an authorized user session before starting."
            )
        self._wire_handlers()
        log.info(
            "bot.ready",
            owner_id=self.owner_id,
            user_session_ready=self.user_session_ready,
            concurrency=self.settings.bot.concurrency,
        )
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

        Does NOT keep the client connected.
        """
        path = self.settings.telegram.session_path
        if not path.exists():
            log.warning(
                "bot.user_session.missing",
                path=str(path),
                hint="send /upload_session via Telegram or SCP the file in",
            )
            return
        derived = await _probe_session_owner_id(path, self.settings)
        if derived is None:
            log.warning("bot.user_session.unauthorized", path=str(path))
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


async def _probe_session_owner_id(session_path: Path, settings: Settings) -> int | None:
    """Open a Telethon session, return `me.id` iff authorized, else None.

    Combines the authorization check with the owner-ID derivation so
    we only pay one connect/disconnect cycle. Always disconnects
    before returning so the SQLite file handle releases.
    """
    client = TelegramClient(
        str(session_path),
        api_id=settings.telegram.api_id,
        api_hash=settings.telegram.api_hash,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return None
        me = await client.get_me()
        # Telethon's get_me() can return None on a degenerate session
        # (e.g. mid-revocation). Treat that the same as unauthorized.
        if me is None or not getattr(me, "id", 0):
            return None
        return int(me.id)
    except Exception:
        log.exception("bot.session.probe_failed")
        return None
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


async def _safe_reply(event: events.NewMessage.Event, text: str) -> None:
    """Reply, swallowing transport errors so the bot loop keeps running."""
    try:
        await event.reply(text)
    except Exception:
        log.exception("bot.reply_failed")
