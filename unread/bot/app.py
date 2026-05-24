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

        # `events.CallbackQuery` has no `from_users=` (unlike NewMessage)
        # — it accepts `chats=` only. For a single-owner private bot the
        # owner's user_id IS the chat_id of the 1:1 conversation, so
        # `chats=[owner_id]` filters out callbacks from any other chat
        # (groups, other DMs). The `event.sender_id != owner_id` check
        # inside `_handle_callback` is the defense-in-depth fallback.
        @self.bot_client.on(events.CallbackQuery(chats=[owner_id]))
        async def _on_owner_callback(event: events.CallbackQuery.Event) -> None:
            if event.sender_id != owner_id:
                return
            task = asyncio.create_task(self._handle_callback(event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _handle(self, event: events.NewMessage.Event) -> None:
        """Per-message worker. Classify → show confirm OR execute → reply.

        Wrapped in a top-level try/except so a handler raising never
        kills the event loop or leaves the bot silent.

        Two paths for analysis-shaped messages:
        * Default: show the confirm panel (cheap, no semaphore) and
          stash a `PendingRun`. The actual analyze runs later when the
          user taps ▶ Run on the panel (semaphore-gated in
          `_handle_callback`).
        * `/confirm off` chat state: skip the panel and run analyze
          immediately (today's pre-panel behavior, semaphore-gated
          here in `_handle`).
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

        if chat_state.get("confirm_disabled"):
            # No panel — straight to execute. Same path the original
            # pre-confirm bot took. Semaphore gates the analyze work.
            from unread.bot.confirm import default_options

            options = default_options(kind, self.settings)
            async with self._semaphore:
                try:
                    await self._run_execute(event, kind, payload, options, progress_msg=None)
                except Exception as e:
                    if _is_clean_exit(e):
                        return
                    log.exception("bot.handler_failed", kind=kind)
                    await _safe_reply(event, f"⚠️ {type(e).__name__}: {e}")
            return

        # Default: append to the chat's burst and let the debounce
        # timer flush it into one consolidated `▶ Run separately /
        # ▶ Run combined` panel. Multiple links pasted in quick
        # succession produce ONE panel, not N.
        from unread.bot.burst import add_to_burst

        try:
            await add_to_burst(self, event, kind, payload)
        except Exception:
            log.exception("bot.add_to_burst_failed", kind=kind)
            await _safe_reply(event, "⚠️ Couldn't queue the message.")

    async def _handle_cmd(self, event: events.NewMessage.Event, payload: dict) -> None:
        """Trivial-reply slash commands. Imported lazily."""
        from unread.bot.handlers import cmds

        await cmds.handle(event, payload, app=self)

    async def _run_execute(
        self,
        event: events.NewMessage.Event,
        kind: str,
        payload: dict,
        options,
        *,
        progress_msg=None,
    ) -> None:
        """Dispatch to the kind-specific `execute`. Lazy-imports the module."""
        if kind == "file":
            from unread.bot.handlers import file as file_handler

            await file_handler.execute(event, payload, options, app=self, progress_msg=progress_msg)
        elif kind == "youtube":
            from unread.bot.handlers import youtube as yt_handler

            await yt_handler.execute(event, payload, options, app=self, progress_msg=progress_msg)
        elif kind == "url":
            from unread.bot.handlers import url as url_handler

            await url_handler.execute(event, payload, options, app=self, progress_msg=progress_msg)
        elif kind == "tg":
            from unread.bot.handlers import tg as tg_handler

            await tg_handler.execute(event, payload, options, app=self, progress_msg=progress_msg)
        else:
            await _safe_reply(event, f"⚠️ Unknown message kind: {kind!r}")

    # ------------------------------------------------------------------
    # Callback handling (inline-keyboard taps)
    # ------------------------------------------------------------------

    async def _handle_callback(self, event: events.CallbackQuery.Event) -> None:
        """Route a confirm-panel button tap.

        The only action is `R` (Run) — the panel exists solely to gate
        analyze on an explicit tap. Per-run tuning is via slash
        commands (`/preset <name>`), not buttons.

        Stale panels (TTL-expired or post-restart) reply with a
        "session expired" toast — user sends the link again.
        """
        from unread.bot.confirm import parse_callback, prune_pending_runs

        if event.sender_id != self.owner_id:
            return
        chat_state = self._chat_state.setdefault(event.chat_id, {})
        prune_pending_runs(chat_state)
        try:
            action, panel_msg_id, _arg = parse_callback(event.data)
        except ValueError:
            log.warning("bot.callback.bad_data", data=event.data)
            with contextlib.suppress(Exception):
                await event.answer("Invalid request.", alert=True)
            return

        pending_runs = chat_state.get("pending_runs") or {}
        pending = pending_runs.get(panel_msg_id)
        if pending is None:
            with contextlib.suppress(Exception):
                await event.answer("Session expired — send again.", alert=True)
            with contextlib.suppress(Exception):
                await event.edit("✖ Session expired.", buttons=None)
            return

        # Drop the pending before kicking off — prevents a double-tap
        # from running twice while the first is in flight.
        from unread.bot.confirm import tg_window_for_action

        is_tg_window = tg_window_for_action(action) is not None
        is_forward = action in ("F_FULL", "F_TXT", "F_FROM", "F_DAY", "F_WK", "F_MO")
        if action in ("R", "A", "M") or is_tg_window or is_forward:
            pending_runs.pop(panel_msg_id, None)
            with contextlib.suppress(Exception):
                await event.answer("Running…")
            try:
                panel_msg = await event.get_message()
            except Exception:
                panel_msg = None

        # Every panel coming out of the burst flow is `kind="batch"`,
        # whether the burst held 1 item or N. R and A both mean "run
        # each item under its own handler"; the only difference is the
        # button label in build_batch_panel. Route both through the
        # same loop so a single-item batch doesn't trip the kind
        # dispatch in _run_execute.
        if action in ("R", "A"):
            await self._run_batch_separately(pending, panel_msg)
            return

        if action == "M":
            await self._run_batch_combined(pending, panel_msg)
            return

        if is_tg_window:
            # Stamp the chosen window onto pending.options so the TG
            # handler's execute() reads it and overrides its default
            # from_msg / last_days computation, then go through the
            # normal single-item run path.
            pending.options.tg_window = tg_window_for_action(action)
            await self._run_batch_separately(pending, panel_msg)
            return

        if is_forward:
            await self._run_forward_action(action, pending, panel_msg)
            return

        # Unknown action — log and ignore so a single bad button doesn't
        # leave the user staring at a frozen panel.
        log.warning("bot.callback.unknown_action", action=action)
        with contextlib.suppress(Exception):
            await event.answer()

    # ------------------------------------------------------------------
    # Batch (burst) execution
    # ------------------------------------------------------------------

    async def _run_batch_separately(self, pending, panel_msg) -> None:
        """Loop items, run each through its kind-specific `execute`.

        Sequential — the analyze pipeline is heavy enough that fanning
        out N parallel runs would just thrash the semaphore + the AI
        provider's rate limit.

        For a single-item batch, the panel itself becomes the progress
        message — avoids spawning a second "⏳ Working…" reply right
        next to the panel. For N≥2, the panel is edited to a
        "⏳ Running k/N …" status line between items and each item's
        execute() spawns its own progress reply.
        """
        from unread.bot.confirm import default_options

        items = pending.payload.get("items") or []
        total = len(items)
        if total == 0:
            return

        if total == 1:
            item = items[0]
            options = default_options(item.kind, self.settings)
            async with self._semaphore:
                try:
                    await self._run_execute(
                        item.event,
                        item.kind,
                        item.payload,
                        options,
                        progress_msg=panel_msg,
                    )
                except Exception as e:
                    if _is_clean_exit(e):
                        return
                    log.exception("bot.batch.item_failed", kind=item.kind)
                    await _safe_reply(item.event, f"⚠️ {type(e).__name__}: {e}")
            return

        from unread.bot.progress import edit_progress

        for idx, item in enumerate(items, start=1):
            await edit_progress(panel_msg, f"⏳ Running {idx}/{total}: {_burst_item_label(item)}")
            options = default_options(item.kind, self.settings)
            async with self._semaphore:
                try:
                    await self._run_execute(
                        item.event,
                        item.kind,
                        item.payload,
                        options,
                        progress_msg=None,
                    )
                except Exception as e:
                    if _is_clean_exit(e):
                        continue
                    log.exception("bot.batch.item_failed", kind=item.kind, idx=idx)
                    await _safe_reply(item.event, f"⚠️ Item {idx}/{total} failed: {type(e).__name__}: {e}")
        await edit_progress(panel_msg, f"✓ Finished {total} items.")

    async def _run_forward_action(self, action: str, pending, panel_msg) -> None:
        """Execute a forward-picker button tap.

        F_FULL → analyze the forwarded message in place. File handler
        already reads `payload["caption"]` to combine image extract +
        caption text when both are present.
        F_TXT  → analyze just the caption / inner text (skip vision).
        F_DAY/F_WK/F_MO → synthesize a `t.me/c/<channel_id>` ref and
        dispatch to the TG handler with the matching window override.
        """
        from unread.bot.burst import BurstItem
        from unread.bot.confirm import RunOptions

        items = pending.payload.get("items") or []
        if not items:
            return
        item = items[0]
        payload = item.payload

        if action == "F_FULL":
            # Existing burst-separately path handles this perfectly —
            # the payload already carries `caption` for file.execute to
            # combine with the image extraction.
            await self._run_batch_separately(pending, panel_msg)
            return

        from unread.bot.progress import edit_progress

        if action == "F_TXT":
            # Synthesize a text-only file payload from the caption (for
            # media+caption forwards) or from the inner text (for
            # text-only forwards), then run as a fresh file item.
            text_content = (payload.get("caption") or payload.get("text") or "").strip()
            if not text_content:
                await edit_progress(panel_msg, "✖ Nothing to analyze (no caption).")
                return
            text_payload = {
                "source": "text",
                "text": text_content,
                "name": "forwarded",
            }
            text_item = BurstItem(kind="file", payload=text_payload, event=item.event)
            options = RunOptions()
            async with self._semaphore:
                try:
                    await self._run_execute(
                        text_item.event,
                        "file",
                        text_payload,
                        options,
                        progress_msg=panel_msg,
                    )
                except Exception as e:
                    if _is_clean_exit(e):
                        return
                    log.exception("bot.forward.text_failed")
                    await _safe_reply(text_item.event, f"⚠️ {type(e).__name__}: {e}")
            return

        # F_FROM / F_DAY / F_WK / F_MO → open the source channel.
        channel_id = payload.get("fwd_channel_id")
        if not channel_id:
            await edit_progress(panel_msg, "✖ No source channel ID on this forward.")
            return
        if not self.user_session_ready:
            await edit_progress(
                panel_msg,
                "I don't have your Telegram user session — needed to read "
                "private channels. Send `/upload_session` first.",
            )
            return

        # F_FROM additionally anchors on the forwarded msg's id in the
        # source channel — analyze "what was posted from here forward"
        # without a time window. The other window actions ignore msg id
        # and apply last_days only.
        if action == "F_FROM":
            fwd_msg_id = payload.get("fwd_msg_id")
            if not fwd_msg_id:
                await edit_progress(
                    panel_msg,
                    "✖ No msg id on this forward — can't anchor 'from this message'.",
                )
                return
            tg_payload = {"url": f"https://t.me/c/{int(channel_id)}/{int(fwd_msg_id)}"}
            options = RunOptions(tg_window="from_msg")
        else:
            window_by_action = {"F_DAY": "1d", "F_WK": "7d", "F_MO": "30d"}
            tg_payload = {"url": f"https://t.me/c/{int(channel_id)}"}
            options = RunOptions(tg_window=window_by_action[action])

        async with self._semaphore:
            try:
                await self._run_execute(
                    item.event,
                    "tg",
                    tg_payload,
                    options,
                    progress_msg=panel_msg,
                )
            except Exception as e:
                if _is_clean_exit(e):
                    return
                log.exception("bot.forward.channel_failed", action=action)
                await _safe_reply(item.event, f"⚠️ {type(e).__name__}: {e}")

    async def _run_batch_combined(self, pending, panel_msg) -> None:
        """Concat extracted text from every combinable item → one analyze."""
        from unread.bot.combined import run_combined
        from unread.bot.progress import edit_progress

        items = pending.payload.get("items") or []
        if not items:
            return
        async with self._semaphore:
            try:
                await run_combined(self, items=items, panel_msg=panel_msg, original_event=pending.event)
            except Exception as e:
                if _is_clean_exit(e):
                    return
                log.exception("bot.batch.combined_failed")
                await edit_progress(panel_msg, f"⚠️ Combined run failed: {type(e).__name__}: {e}")


def _burst_item_label(item) -> str:
    """One-line description for the in-progress edit. Mirrors burst.summary_line
    but available here without importing the burst module up-top (avoids a
    circular import — burst imports confirm which is fine, but app already
    imports burst lazily inside `_handle`)."""
    from unread.bot.burst import summary_line

    return summary_line(item)


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
