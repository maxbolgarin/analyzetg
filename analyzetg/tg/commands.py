"""CLI command implementations for Telegram navigation and subscriptions."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime

import typer
from rich.console import Console
from rich.table import Table

from analyzetg.config import get_settings
from analyzetg.db.repo import open_repo
from analyzetg.i18n import t as _t
from analyzetg.i18n import tf as _tf
from analyzetg.models import Subscription
from analyzetg.tg.client import (
    _chat_kind,
    build_client,
    entity_id,
    entity_title,
    entity_username,
    tg_client,
)
from analyzetg.tg.dialogs import get_unread_state
from analyzetg.tg.links import parse
from analyzetg.tg.resolver import resolve
from analyzetg.tg.topics import (
    get_full_channel_info,
    get_linked_chat_id,
    list_forum_topics,
)
from analyzetg.util.logging import get_logger

console = Console()
log = get_logger(__name__)


# --------------------------------------------------------------------- init


async def cmd_init() -> None:
    """Interactive log-in to Telegram + OpenAI key smoke test."""
    import os
    from pathlib import Path as _Path

    settings = get_settings()
    env_path = _Path(".env").resolve()
    missing = []
    if not settings.telegram.api_id or not settings.telegram.api_hash:
        missing.append("TELEGRAM_API_ID / TELEGRAM_API_HASH")
    if not settings.openai.api_key:
        missing.append("OPENAI_API_KEY")
    if missing:
        console.print(f"[red]{_tf('doctor_missing', missing=', '.join(missing))}[/]")
        if env_path.exists():
            console.print(_tf("doctor_env_seen_at", path=env_path))
        # Also show what we actually see in env for debugging
        seen = {
            k: bool(os.environ.get(k)) for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "OPENAI_API_KEY")
        }
        console.print(f"[dim]{_tf('doctor_env_seen', seen=seen)}[/]")
        raise typer.Exit(1)

    # Ensure DB is migrated
    async with open_repo(settings.storage.data_path):
        pass

    # Telegram auth — delegate retries to client.start()
    client = build_client(settings)

    def _phone() -> str:
        return typer.prompt(_t("init_phone_prompt"))

    def _code() -> str:
        return typer.prompt(_t("init_login_code_prompt"))

    def _password() -> str:
        # Telethon retries this callback on PasswordHashInvalidError,
        # so a wrong 2FA password only reprompts the 2FA step.
        return typer.prompt(_t("init_2fa_prompt"), hide_input=True)

    await client.connect()
    try:
        if await client.is_user_authorized():
            console.print(f"[green]{_t('doctor_session_authorized')}[/]")
        else:
            await client.start(phone=_phone, code_callback=_code, password=_password)
            console.print(f"[green]{_t('doctor_logged_in')}[/]")
    finally:
        await client.disconnect()

    # OpenAI smoke test
    console.print(_t("doctor_check_openai"))
    try:
        from openai import AsyncOpenAI

        oai = AsyncOpenAI(api_key=settings.openai.api_key, timeout=settings.openai.request_timeout_sec)
        await asyncio.wait_for(oai.models.list(), timeout=15)
        console.print(f"[green]{_t('doctor_openai_ok')}[/]")
    except Exception as e:
        console.print(f"[yellow]{_tf('doctor_openai_failed', err=e)}[/]")


# --------------------------------------------------------------------- doctor


async def cmd_doctor() -> None:
    """Run a battery of health checks and print a per-line status report.

    No mutations, no expensive calls: each check has a hard cap on time/cost.
    Designed so a user pasting the output into a bug report is enough for
    triage.
    """
    import os
    import shutil
    from pathlib import Path as _Path

    settings = get_settings()
    ok = "[green]OK[/]"
    warn = "[yellow]WARN[/]"
    fail = "[red]FAIL[/]"
    statuses: list[str] = []

    def _line(status: str, label: str, detail: str = "") -> None:
        console.print(f"  {status:<24} {label}{(' — ' + detail) if detail else ''}")
        statuses.append(status)

    console.print(f"[bold]{_t('tg_doctor_banner')}[/]")

    # 1. Config files
    cwd = _Path.cwd()
    env_path = cwd / ".env"
    cfg_path = _Path(os.environ.get("ANALYZETG_CONFIG_PATH", "config.toml"))
    if env_path.exists():
        _line(ok, ".env present", str(env_path))
    else:
        _line(warn, ".env missing", f"expected at {env_path}")
    if cfg_path.exists():
        _line(ok, "config.toml present", str(cfg_path))
    else:
        _line(warn, "config.toml missing", f"expected at {cfg_path}")

    # 2. Secrets resolved
    if settings.telegram.api_id and settings.telegram.api_hash:
        _line(ok, "telegram credentials", f"api_id={settings.telegram.api_id}")
    else:
        _line(fail, "telegram credentials missing", "set TELEGRAM_API_ID / TELEGRAM_API_HASH in .env")
    if settings.openai.api_key:
        _line(ok, "OPENAI_API_KEY present")
    else:
        _line(fail, "OPENAI_API_KEY missing", "set in .env")

    # 3. ffmpeg
    ffmpeg_path = shutil.which(settings.media.ffmpeg_path) or shutil.which("ffmpeg")
    if ffmpeg_path:
        _line(ok, "ffmpeg on PATH", ffmpeg_path)
    else:
        _line(
            warn,
            "ffmpeg not found",
            "voice/videonote/video enrichment will skip; install ffmpeg or set [media] ffmpeg_path",
        )

    # 3b. yt-dlp (YouTube analysis)
    try:
        import yt_dlp  # type: ignore[import-not-found]

        _line(ok, "yt-dlp installed", getattr(yt_dlp, "__version__", "?"))
    except ImportError:
        _line(
            warn,
            "yt-dlp not installed",
            "`atg analyze <youtube-url>` will fail; run `uv sync` to install",
        )

    # 4. Storage paths + disk
    storage_dir = settings.storage.data_path.parent
    if storage_dir.exists():
        try:
            usage = shutil.disk_usage(storage_dir)
            free_gb = usage.free / 1024**3
            if free_gb < 0.5:
                _line(fail, "disk free", f"{free_gb:.2f} GB at {storage_dir}")
            elif free_gb < 5.0:
                _line(warn, "disk free", f"{free_gb:.2f} GB at {storage_dir}")
            else:
                _line(ok, "disk free", f"{free_gb:.2f} GB at {storage_dir}")
        except OSError as e:
            _line(warn, "disk usage check failed", str(e)[:100])
    else:
        _line(warn, "storage dir missing", f"will be created on first write: {storage_dir}")

    # 5. DB integrity + size
    db_path = settings.storage.data_path
    if db_path.exists():
        try:
            from analyzetg.db.repo import open_repo as _open_repo

            async with _open_repo(db_path) as repo:
                cur = await repo._conn.execute("PRAGMA integrity_check")
                row = await cur.fetchone()
                await cur.close()
                verdict = (row["integrity_check"] if row else "?") if row is not None else "?"
            size_mb = db_path.stat().st_size / 1024**2
            if verdict == "ok":
                _line(ok, "DB integrity", f"{db_path} ({size_mb:.1f} MB)")
            else:
                _line(fail, "DB integrity check failed", str(verdict)[:200])
        except Exception as e:
            _line(fail, "DB open failed", str(e)[:200])
    else:
        _line(warn, "DB not yet created", str(db_path))

    # 6. Telegram session liveness
    session_path = settings.telegram.session_path
    # Telethon appends `.session` when the configured path doesn't already
    # end with it, so check both forms before declaring the file missing.
    session_with_suffix = session_path.with_name(session_path.name + ".session")
    session_present = session_path.exists() or session_with_suffix.exists()
    actual_session = session_path if session_path.exists() else session_with_suffix
    if not session_present:
        _line(warn, "Telegram session missing", f"run `atg init` (expected {session_path})")
    elif settings.telegram.api_id and settings.telegram.api_hash:
        try:
            client = build_client(settings)
            await asyncio.wait_for(client.connect(), timeout=10)
            try:
                authorized = await client.is_user_authorized()
            finally:
                await client.disconnect()
            if authorized:
                _line(ok, "Telegram session", f"authorized ({actual_session})")
            else:
                _line(fail, "Telegram session", "not authorized — run `atg init`")
        except Exception as e:
            _line(warn, "Telegram session check failed", str(e)[:200])

    # 7. OpenAI key liveness
    if settings.openai.api_key:
        try:
            from openai import AsyncOpenAI

            oai = AsyncOpenAI(
                api_key=settings.openai.api_key,
                timeout=settings.openai.request_timeout_sec,
            )
            await asyncio.wait_for(oai.models.list(), timeout=10)
            _line(ok, "OpenAI API reachable")
        except Exception as e:
            _line(warn, "OpenAI API check failed", str(e)[:200])

    # 8. Presets
    try:
        from analyzetg.analyzer.prompts import PRESETS

        if PRESETS:
            _line(ok, "presets loaded", f"{len(PRESETS)} ({', '.join(sorted(PRESETS))})")
        else:
            _line(warn, "no presets loaded", "expected presets/*.md")
    except Exception as e:
        _line(fail, "preset load failed", str(e)[:200])

    # 9. Pricing coverage — chat AND audio. Missing the audio entry was
    # invisible until now: voice transcription would silently drop cost
    # accounting on `atg stats`.
    pricing = settings.pricing
    chat_referenced = {
        settings.openai.chat_model_default,
        settings.openai.filter_model_default,
        settings.enrich.vision_model,
    }
    chat_referenced.discard(None)
    chat_missing = [m for m in chat_referenced if m and m not in pricing.chat]
    audio_model = settings.openai.audio_model_default
    audio_missing = bool(audio_model and audio_model not in pricing.audio)
    if chat_missing or audio_missing:
        bits: list[str] = []
        if chat_missing:
            bits.append(f'[pricing.chat."{chat_missing[0]}"]')
        if audio_missing:
            bits.append(f'[pricing.audio]."{audio_model}"')
        _line(
            warn,
            "pricing entries missing",
            f"add {' / '.join(bits)} to config.toml; cost stats will under-report",
        )
    else:
        _line(ok, "pricing covers default models")

    # Summary
    fails = sum(1 for s in statuses if "FAIL" in s)
    warns = sum(1 for s in statuses if "WARN" in s)
    if fails:
        console.print(f"[bold red]{_tf('doctor_summary_failed', fails=fails, warns=warns)}[/]")
        raise typer.Exit(1)
    if warns:
        console.print(f"[bold yellow]{_tf('doctor_summary_warned', warns=warns)}[/]")
    else:
        console.print(f"[bold green]{_t('doctor_all_ok')}[/]")


# ------------------------------------------------------------------- dialogs


async def cmd_dialogs(search: str | None, kind: str | None, limit: int) -> None:
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        table = Table(title="Telegram dialogs", show_lines=False)
        for col in ("id", "kind", "title", "username", "unread"):
            table.add_column(col)

        shown = 0
        async for d in client.iter_dialogs(limit=None):  # type: ignore[arg-type]
            entity = d.entity
            k = _chat_kind(entity)
            t = entity_title(entity)
            u = entity_username(entity)
            if kind and k != kind:
                continue
            if search:
                hay = f"{t or ''} {u or ''}".lower()
                if search.lower() not in hay:
                    continue
            await repo.upsert_chat(entity_id(entity), k, title=t, username=u)
            table.add_row(
                str(entity_id(entity)),
                k,
                t or "",
                f"@{u}" if u else "",
                str(getattr(d, "unread_count", 0)),
            )
            shown += 1
            if shown >= limit:
                break
        console.print(table)
        console.print(f"[dim]{_tf('tg_n_rows', n=shown)}[/]")


# ------------------------------------------------------------------- describe


DEFAULT_KINDS = ("forum", "supergroup", "group")


async def cmd_describe(
    ref: str | None,
    *,
    kind: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    show_all: bool = False,
) -> None:
    """Overview of dialogs, or details about one chat.

    With no ref and no filter flags, opens an interactive picker so you
    can choose a chat and see its details. With filter flags (--all /
    --kind / --search / --limit) or a ref, behaves non-interactively.
    """
    # No ref and no filters → interactive chat picker.
    has_filters = bool(kind or search or limit or show_all)
    if ref is None and not has_filters:
        from analyzetg.interactive import run_interactive_describe

        await run_interactive_describe()
        return

    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        if ref is None:
            await _describe_overview(
                client,
                repo,
                kind=kind,
                search=search,
                limit=limit,
                show_all=show_all,
            )
            return
        await _describe_one(client, repo, ref)


async def _describe_overview(
    client,
    repo,
    *,
    kind: str | None,
    search: str | None,
    limit: int | None,
    show_all: bool,
) -> None:
    from analyzetg.tg.folders import chat_folder_index

    console.print(f"[dim]{_t('tg_listing_dialogs')}[/]")
    folder_idx = await chat_folder_index(client)
    rows: list[tuple] = []
    async for d in client.iter_dialogs(limit=None):  # type: ignore[arg-type]
        entity = d.entity
        k = _chat_kind(entity)
        t = entity_title(entity)
        u = entity_username(entity)
        eid = entity_id(entity)
        unread = int(getattr(d, "unread_count", 0) or 0)

        # Apply filters BEFORE hitting the DB — saves N queries.
        if kind and k != kind:
            continue
        if search:
            hay = f"{t or ''} {u or ''}".lower()
            if search.lower() not in hay:
                continue
        if not show_all:
            if k not in DEFAULT_KINDS:
                continue
            if unread <= 0:
                continue

        stats = await repo.chat_stats(eid)
        folders_str = ", ".join(folder_idx.get(eid, []))
        rows.append((unread, eid, k, t or "", u or "", stats["count"], stats["date_max"], folders_str))

    rows.sort(key=lambda r: (-r[0], -r[5]))
    if limit:
        rows = rows[:limit]

    # Title hint reflects the filter state.
    desc_parts = []
    if show_all:
        desc_parts.append("all")
    else:
        desc_parts.append("unread")
        if kind is None:
            desc_parts.append("forums/groups/supergroups")
    if kind:
        desc_parts.append(f"kind={kind}")
    if search:
        desc_parts.append(f"search={search!r}")
    title = "Dialogs (" + ", ".join(desc_parts) + ")"

    table = Table(title=title)
    for col in ("id", "kind", "title", "username", "unread", "stored", "last_msg", "folder"):
        table.add_column(col)
    for unread, eid, k, t, u, stored, dmax, folders_str in rows:
        table.add_row(
            str(eid),
            k,
            t,
            f"@{u}" if u else "",
            str(unread) if unread else "",
            str(stored) if stored else "",
            dmax.strftime("%Y-%m-%d %H:%M") if dmax else "",
            folders_str,
        )
    console.print(table)
    hint_parts = [_tf("tg_n_rows", n=len(rows))]
    if not show_all:
        hint_parts.append(_t("tg_dialogs_default_filter"))
        hint_parts.append(_t("tg_dialogs_pass_all"))
    console.print(f"[dim]{'. '.join(hint_parts)}.[/]")
    console.print(f"[dim]{_t('tg_describe_hint')}[/]")


async def _describe_one(client, repo, ref: str) -> None:
    resolved = await resolve(client, repo, ref, prompt_choice=_tui_choose)
    chat_id = resolved.chat_id
    kind = resolved.kind

    # Pull live dialog-level state (unread, read marker, last message date).
    unread_count, read_marker = await get_unread_state(client, chat_id)
    last_msg_date = await _fetch_last_msg_date(client, chat_id)

    # Header
    badge = f"[bold]{resolved.title or chat_id}[/]"
    console.print(f"\n{badge} [dim]{_tf('tg_describe_id_kind', chat_id=chat_id, kind=kind)}[/]")

    # --- Left/right-ish labeled properties
    def _row(label: str, value: str | None, *, dim_label: bool = True) -> None:
        if value is None or value == "":
            return
        label_fmt = f"[dim]{label:>14}:[/]" if dim_label else f"{label:>14}:"
        console.print(f"  {label_fmt} {value}")

    if resolved.username:
        _row("username", f"@{resolved.username} — https://t.me/{resolved.username}")
    # Telegram folders the chat is explicitly listed in (rule-based folders
    # are not expanded — see tg/folders.py).
    try:
        from analyzetg.tg.folders import chat_folder_index

        idx = await chat_folder_index(client)
        folders_for_chat = idx.get(chat_id, [])
        if folders_for_chat:
            _row("folder", ", ".join(folders_for_chat))
    except Exception as e:
        log.debug("describe.folder_lookup_failed", err=str(e)[:100])
    _row("unread", str(unread_count) if unread_count else None)
    _row(
        "read marker",
        f"msg_id > {read_marker}" if read_marker and unread_count else None,
    )
    if last_msg_date:
        _row("last message", last_msg_date.strftime("%Y-%m-%d %H:%M"))

    # Channel/supergroup/forum extended info
    info: dict = {}
    if kind in ("channel", "supergroup", "forum"):
        try:
            info = await get_full_channel_info(client, chat_id)
        except Exception as e:
            log.warning("describe.full_channel_failed", err=str(e)[:200])
            info = {}

        # Kind details
        type_bits = []
        if info.get("broadcast"):
            type_bits.append("broadcast")
        if info.get("megagroup"):
            type_bits.append("megagroup")
        if info.get("forum"):
            type_bits.append("forum")
        if info.get("verified"):
            type_bits.append("[green]verified[/]")
        if info.get("scam"):
            type_bits.append("[red]scam[/]")
        if info.get("restricted"):
            type_bits.append("[yellow]restricted[/]")
        if type_bits:
            _row("type", " ".join(type_bits))

        # Participants & moderation
        parts = info.get("participants_count")
        online = info.get("online_count")
        if parts is not None:
            val = f"{parts:,}"
            if online:
                val += f" ([green]{online}[/] online)"
            _row("participants", val)
        if info.get("admins_count"):
            _row("admins", str(info["admins_count"]))
        if info.get("banned_count"):
            _row("banned", str(info["banned_count"]))

        # Links, discussion, pin, slowmode
        if info.get("invite_link"):
            _row("invite link", info["invite_link"])
        elif resolved.username:
            pass  # already shown above as username link
        if info.get("linked_chat_id"):
            _row("linked chat", str(info["linked_chat_id"]))
        if info.get("pinned_msg_id"):
            pin_link = _msg_link(resolved.username, chat_id, info["pinned_msg_id"])
            _row("pinned msg", pin_link)
        slow = info.get("slowmode_seconds")
        if slow:
            _row("slow mode", f"{slow}s between messages")

        if info.get("about"):
            # Split "about" on blank lines so long descriptions stay readable.
            first = info["about"].splitlines()[0]
            if len(info["about"]) > 200:
                first = first[:200] + "…"
            _row("about", first)

    # Forums → topics table
    if kind == "forum":
        topics = await list_forum_topics(client, chat_id)
        if topics:
            tt = Table(title=f"Topics ({len(topics)})")
            for col in ("id", "title", "unread", "top_msg", "stored", "closed", "pinned"):
                tt.add_column(col)
            for tp in topics:
                st = await repo.chat_stats(chat_id, thread_id=tp.topic_id)
                tt.add_row(
                    str(tp.topic_id),
                    tp.title,
                    str(tp.unread_count) if tp.unread_count else "",
                    str(tp.top_message or ""),
                    str(st["count"]) if st["count"] else "",
                    "yes" if tp.closed else "",
                    "yes" if tp.pinned else "",
                )
            console.print(tt)

    # Local DB stats
    stats = await repo.chat_stats(chat_id)
    if stats["count"]:
        dmin = stats["date_min"].strftime("%Y-%m-%d %H:%M") if stats["date_min"] else "—"
        dmax = stats["date_max"].strftime("%Y-%m-%d %H:%M") if stats["date_max"] else "—"
        console.print(
            f"\n[bold]Local DB[/]: {stats['count']} message(s), from [cyan]{dmin}[/] to [cyan]{dmax}[/]"
        )
        top = await repo.top_senders(chat_id, limit=5)
        if top:
            console.print(f"[bold]{_t('tg_top_senders_label')}[/]:")
            for row in top:
                console.print(f"  {row['sender_name']} — {row['count']}")
    else:
        console.print(f"\n[dim]{_t('tg_no_messages_local')}[/]")


async def _fetch_last_msg_date(client, chat_id: int):
    """Fetch the date of the most recent message in the chat. Returns datetime or None."""
    try:
        async for m in client.iter_messages(chat_id, limit=1):
            return getattr(m, "date", None)
    except Exception as e:
        log.debug("describe.last_msg_failed", chat_id=chat_id, err=str(e)[:200])
    return None


def _msg_link(username: str | None, chat_id: int, msg_id: int) -> str:
    """Render a t.me link to a specific message (prefer @username form)."""
    if username:
        return f"{msg_id} — https://t.me/{username}/{msg_id}"
    if chat_id < 0 and abs(chat_id) > 1_000_000_000_000:
        internal = abs(chat_id) - 1_000_000_000_000
        return f"{msg_id} — https://t.me/c/{internal}/{msg_id}"
    return str(msg_id)


# -------------------------------------------------------------------- topics


async def cmd_topics(chat_ref: str) -> None:
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        ref = await resolve(client, repo, chat_ref, prompt_choice=_tui_choose)
        if ref.kind not in ("forum", "supergroup", "channel"):
            console.print(f"[yellow]{_tf('tg_not_a_forum', title=ref.title)}[/]")
            raise typer.Exit(1)
        topics = await list_forum_topics(client, ref.chat_id)
        t = Table(title=f"Forum topics: {ref.title}")
        t.add_column("id")
        t.add_column("title")
        t.add_column("closed")
        t.add_column("pinned")
        for x in topics:
            t.add_row(str(x.topic_id), x.title, "yes" if x.closed else "", "yes" if x.pinned else "")
        console.print(t)
        console.print(f"[dim]{_tf('tg_n_topics', n=len(topics))}[/]")


# -------------------------------------------------------------------- resolve


async def cmd_resolve(ref: str) -> None:
    settings = get_settings()
    parsed = parse(ref)
    console.print(f"[bold]{_t('tg_resolve_parsed_label')}[/] {parsed}")
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        try:
            resolved = await resolve(client, repo, ref, prompt_choice=_tui_choose)
            console.print(f"[bold green]{_t('tg_resolve_done_label')}[/] {resolved}")
        except Exception as e:
            console.print(f"[red]{_t('tg_resolve_failed_label')}[/] {e}")


# --------------------------------------------------------------- channel-info


async def cmd_channel_info(ref: str) -> None:
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        resolved = await resolve(client, repo, ref, prompt_choice=_tui_choose)
        info = await get_full_channel_info(client, resolved.chat_id)
        console.print(
            f"[bold]{resolved.title}[/] "
            f"{_tf('tg_describe_id_kind_inline', chat_id=resolved.chat_id, kind=resolved.kind)}"
        )
        console.print(_tf("tg_describe_participants", n=info["participants_count"]))
        console.print(_tf("tg_describe_linked_chat_id", id=info["linked_chat_id"]))
        if info.get("about"):
            console.print(_tf("tg_describe_about", text=info["about"]))


# ------------------------------------------------------------------- chats.*


async def cmd_chats_add(
    *,
    ref: str | None,
    from_date: str | None,
    from_msg: str | None,
    last: int | None,
    full_history: bool,
    thread: int | None,
    all_topics: bool,
    with_comments: bool,
    join: bool,
    no_transcribe: bool,
    preset: str | None = None,
    period: str | None = None,
    enrich: str | None = None,
    no_mark_read: bool = False,
    post_to: str | None = None,
) -> None:
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        # No ref → interactive picker (any dialog, not just unread). After
        # picking, ask the kind-specific questions inline so a user with
        # no flags ends up with a sensible subscription.
        if ref is None:
            import questionary

            from analyzetg.interactive import _pick_chat as _picker

            # Pre-load the set of already-subscribed chat ids so the
            # picker can flag them with a `★`. Helps the user see at a
            # glance which dialogs are already in `atg chats list`.
            existing_subs = await repo.list_subscriptions(enabled_only=False)
            subscribed_ids = {int(s.chat_id) for s in existing_subs}

            picked = await _picker(
                client,
                offer_all_unread=False,
                subscribed_ids=subscribed_ids,
            )
            if picked is None or not isinstance(picked, dict):
                console.print(f"[dim]{_t('cancelled')}[/]")
                return
            ref = str(picked["chat_id"])
            if int(picked["chat_id"]) in subscribed_ids:
                console.print(
                    f"[yellow]→ {picked.get('title') or picked['chat_id']} is already "
                    "subscribed.[/] Continuing — re-running `add` is idempotent and lets "
                    "you bolt on extras (e.g. comments / topics) without removing the existing sub."
                )
            # Forum / channel toggles get asked here unless the CLI
            # already set them. Use `questionary.confirm` (prompt_toolkit
            # under the hood) instead of `typer.confirm` — typer's
            # blocking input() leaves the terminal in raw mode after a
            # prior questionary picker, so Enter shows up as `^M` and
            # never submits.
            kind = picked.get("kind")
            if kind == "channel" and not with_comments:
                # Brief explainer so the choice is informed: comments
                # live in a separate Telegram chat (the linked discussion
                # group). Saying yes here creates a SECOND subscription
                # for that group. `atg chats run` then folds the comments
                # into the same report as the channel — one analysis per
                # channel, not two — see `--with-comments` semantics.
                console.print(
                    "[dim]→ Channels store posts; user comments live in a "
                    "linked discussion group (a separate Telegram chat). "
                    "Saying yes adds a sibling subscription for that group; "
                    "`atg chats run` will merge channel posts + comments "
                    "into ONE report (not two).[/]"
                )
                with_comments = bool(
                    await questionary.confirm(
                        "Also subscribe to this channel's linked discussion group (comments)?",
                        default=True,
                    ).ask_async()
                )
            if kind == "forum" and not all_topics and thread is None:
                all_topics = bool(
                    await questionary.confirm(
                        "Forum: subscribe to every topic (recommended)?",
                        default=True,
                    ).ask_async()
                )
            # `atg chats run` settings: preset, period, enrich, mark_read.
            # These get persisted on the subscription so `atg chats run`
            # can walk every enabled sub and analyze each one with
            # its own settings without re-prompting. Each step skips
            # itself when the matching CLI flag was set.
            #
            # Reuse the analyze wizard's pickers wholesale — same
            # labels, same keybindings (arrow-toggle, Enter, ESC),
            # same defaults — so users don't have to relearn anything
            # between `atg analyze` and `atg chats add`.
            from analyzetg.interactive import BACK as _BACK
            from analyzetg.interactive import (
                _pick_enrich,
                _pick_mark_read,
                _pick_period,
                _pick_preset,
            )

            def _bail() -> None:
                console.print(f"[dim]{_t('cancelled')}[/]")

            if preset is None:
                picked_preset = await _pick_preset()
                if picked_preset is None or picked_preset is _BACK:
                    _bail()
                    return
                preset = picked_preset
            if period is None:
                # `static_only=True` hides custom-range / from-msg —
                # neither makes sense as a persisted recurring period.
                period_result = await _pick_period(static_only=True)
                if period_result is None or period_result is _BACK:
                    _bail()
                    return
                period = period_result[0]
            if enrich is None:
                enrich_pick = await _pick_enrich()
                if enrich_pick is None or enrich_pick is _BACK:
                    _bail()
                    return
                # Empty list = explicitly "no enrichment". The persisted
                # column is a CSV; "" represents "off everywhere".
                enrich = ",".join(enrich_pick)
                # Legacy `--no-transcribe` keeps mirroring the
                # voice/videonote choice so older log lines stay
                # consistent with the new picker's outcome.
                no_transcribe = "voice" not in enrich_pick and "videonote" not in enrich_pick
            if not no_mark_read:
                mr_result = await _pick_mark_read(default=True)
                if mr_result is None or mr_result is _BACK:
                    _bail()
                    return
                no_mark_read = not bool(mr_result)
            # post_to: where to deliver the report after `atg chats run`
            # analyzes this sub. Three sensible defaults:
            #   - "No"          → save to reports/<chat>/ only
            #   - "Saved Msgs"  → send to your own Telegram Saved Messages
            #   - "Custom"      → text-input for any chat ref (@channel,
            #                     numeric id, t.me link, fuzzy title)
            # Resolution happens at run time via tg/resolver, so any
            # form `--post-to` accepts works here too.
            if post_to is None:
                post_choice = await questionary.select(
                    "After analyze, post the report to a Telegram chat?",
                    choices=[
                        questionary.Choice("No — save to reports/ only", value="no"),
                        questionary.Choice("Saved Messages (recommended for personal digests)", value="me"),
                        questionary.Choice("Custom chat / channel…", value="custom"),
                    ],
                    default="no",
                ).ask_async()
                if post_choice is None:
                    _bail()
                    return
                if post_choice == "me":
                    post_to = "me"
                elif post_choice == "custom":
                    post_ref = await questionary.text(
                        "Post-to ref (@channel, t.me link, numeric id, or fuzzy title — blank to skip)",
                        default="",
                    ).ask_async()
                    if post_ref is None:
                        _bail()
                        return
                    post_ref = post_ref.strip()
                    post_to = post_ref or None

        resolved = await resolve(client, repo, ref, join=join, prompt_choice=_tui_choose)

        from_msg_id = _parse_from_msg(from_msg)
        from_dt = datetime.strptime(from_date, "%Y-%m-%d") if from_date else None
        if full_history:
            from_dt = datetime(1970, 1, 1)
            from_msg_id = None

        # Settings that apply to every Subscription built below — keep
        # them DRY so adding a new field doesn't require touching three
        # constructors.
        run_settings = {
            "preset": preset or "summary",
            "period": period or "unread",
            "enrich_kinds": enrich,  # None = config defaults; "" = no enrichment
            "mark_read": not no_mark_read,
            "post_to": post_to,
        }

        # Base subscription for the chat timeline
        subs_to_add: list[Subscription] = []
        base_thread = thread or 0
        base_source = _source_kind_for(resolved.kind)
        base = Subscription(
            chat_id=resolved.chat_id,
            thread_id=base_thread,
            title=resolved.title,
            source_kind=base_source,
            start_from_msg_id=from_msg_id,
            start_from_date=from_dt,
            transcribe_voice=not no_transcribe,
            transcribe_videonote=not no_transcribe,
            transcribe_video=False,
            **run_settings,
        )
        subs_to_add.append(base)

        if all_topics and resolved.kind in ("forum", "supergroup"):
            topics = await list_forum_topics(client, resolved.chat_id)
            for t in topics:
                subs_to_add.append(
                    Subscription(
                        chat_id=resolved.chat_id,
                        thread_id=t.topic_id,
                        title=f"{resolved.title} / {t.title}",
                        source_kind="topic",
                        start_from_msg_id=None,
                        start_from_date=from_dt,
                        transcribe_voice=not no_transcribe,
                        transcribe_videonote=not no_transcribe,
                        transcribe_video=False,
                        **run_settings,
                    )
                )

        if with_comments and resolved.kind == "channel":
            linked = await get_linked_chat_id(client, resolved.chat_id)
            if linked is None:
                console.print(
                    f"[yellow]{_t('tg_channel_label')}[/] {_tf('tg_channel_no_linked', title=resolved.title)}"
                )
            else:
                # Record the linked chat id on the channel row, create discussion sub.
                await repo.upsert_chat(
                    resolved.chat_id,
                    resolved.kind,
                    title=resolved.title,
                    username=resolved.username,
                    linked_chat_id=linked,
                )
                try:
                    linked_entity = await client.get_entity(linked)
                    linked_title = entity_title(linked_entity)
                except Exception:
                    linked_title = None
                # Comments sub piggybacks on the channel's settings; the
                # channel's analyze run pulls comments inline via
                # `--with-comments` so the comments sub itself isn't
                # analyzed independently. Storing matching settings keeps
                # the row self-consistent if the user later disables the
                # parent and runs the comments group on its own.
                subs_to_add.append(
                    Subscription(
                        chat_id=linked,
                        thread_id=0,
                        title=linked_title or f"{resolved.title} (comments)",
                        source_kind="comments",
                        start_from_date=from_dt,
                        transcribe_voice=not no_transcribe,
                        transcribe_videonote=not no_transcribe,
                        transcribe_video=False,
                        **run_settings,
                    )
                )

        for s in subs_to_add:
            await repo.upsert_subscription(s)
        console.print(f"[green]{_t('tg_added_label')}[/] {_tf('tg_added_msg', n=len(subs_to_add))}")
        for s in subs_to_add:
            console.print(
                _tf(
                    "tg_added_sub_line",
                    chat_id=s.chat_id,
                    thread_id=s.thread_id,
                    kind=s.source_kind,
                    title=s.title,
                )
            )

        # Note --last: we apply it by pulling last N messages immediately at next sync;
        # we record start_from_msg_id = (top_msg_id - last) after the first sync pass.
        if last is not None:
            console.print(f"[dim]{_tf('tg_last_take_effect', value=last)}[/]")
            _hint_last_sync(subs_to_add, last)


def _hint_last_sync(subs: list[Subscription], last: int) -> None:
    # Marker for sync.py: if start_from_msg_id/date are None and a "last" hint
    # is present, we fetch the latest message id and set start_from_msg_id =
    # top_msg_id - last. Delegated to sync.
    for s in subs:
        if s.start_from_msg_id is None and s.start_from_date is None:
            # Encode hint via negative number; sync.py will interpret this.
            s.start_from_msg_id = -int(last)


def _source_kind_for(kind: str) -> str:
    if kind == "channel":
        return "channel"
    if kind == "forum":
        return "chat"
    return "chat"


def _parse_from_msg(value: str | None) -> int | None:
    """Accept either a bare int or a Telegram link pointing at a message."""
    if not value:
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    p = parse(value)
    return p.msg_id


async def _comments_index(repo, subs: list[Subscription]) -> dict[int, dict]:
    """Return per-(channel chat_id) info about its linked comments sub.

    Output keys per channel chat_id (when applicable):
      - "linked_chat_id": int (the discussion group's id)
      - "linked_sub": Subscription | None — the sub for that group, or
        None if the channel has a linked group but the user hasn't
        subscribed to comments yet.

    For a comments sub (source_kind == "comments"), returns a separate
    "comments_for" map keyed by the comments sub's chat_id pointing back
    at the channel chat_id. Both keyspaces are merged in the caller.
    """
    # Map subscription chat_ids → Subscription so we can look up siblings.
    by_chat = {int(s.chat_id): s for s in subs}
    # Pull the channel rows we care about so we know each channel's
    # linked_chat_id without round-tripping Telegram.
    channel_ids = [int(s.chat_id) for s in subs if s.source_kind == "channel"]
    info: dict[int, dict] = {}
    for cid in channel_ids:
        row = await repo.get_chat(cid)
        linked = (row or {}).get("linked_chat_id")
        if linked is None:
            continue
        info[cid] = {
            "linked_chat_id": int(linked),
            "linked_sub": by_chat.get(int(linked))
            if by_chat.get(int(linked)) and by_chat[int(linked)].source_kind == "comments"
            else None,
        }
    # Reverse map so a comments-row can render a back-reference.
    reverse: dict[int, int] = {}
    for cid, meta in info.items():
        if meta.get("linked_sub") is not None:
            reverse[int(meta["linked_chat_id"])] = cid
    return {"by_channel": info, "by_comments": reverse}


def _comments_label(s: Subscription, idx: dict) -> str:
    """Render the value for the `comments` column for one subscription.

    - Channel sub: "✓ <linked title>" if its discussion group is also
      subscribed; "available" if the channel has a linked group but the
      user hasn't subscribed yet; "—" if the channel has no linked group.
    - Comments sub: "↑ for <channel title>" (back-reference).
    - Other kinds: "—".
    """
    by_channel: dict = idx.get("by_channel", {})
    by_comments: dict = idx.get("by_comments", {})
    if s.source_kind == "channel":
        meta = by_channel.get(int(s.chat_id))
        if meta is None:
            return "—"
        linked_sub = meta.get("linked_sub")
        if linked_sub is not None:
            return f"✓ {(linked_sub.title or '').strip() or 'comments'}"
        return "available"
    if s.source_kind == "comments":
        parent_id = by_comments.get(int(s.chat_id))
        if parent_id is None:
            return "↑ (orphan)"
        return f"↑ for {parent_id}"
    return "—"


# ----------------------------------------------------------- sync / backfill


async def cmd_sync(chat: int | None, thread: int | None, dry_run: bool) -> None:
    from analyzetg.tg.sync import sync_subscription

    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        subs = await repo.list_subscriptions(enabled_only=True)
        if chat is not None:
            subs = [s for s in subs if s.chat_id == chat and (thread is None or s.thread_id == thread)]
        if not subs:
            console.print(f"[yellow]{_t('tg_no_matching_subs')}[/]")
            return
        total = 0
        for s in subs:
            added = await sync_subscription(client, repo, s, dry_run=dry_run)
            console.print(
                f"  [cyan]sync[/] chat={s.chat_id} thread={s.thread_id} -> "
                f"{'would fetch' if dry_run else 'fetched'} {added} new msgs"
            )
            total += added
        console.print(f"[green]{_t('tg_done_label')}[/] {_tf('tg_done_n_msgs', n=total)}")


async def cmd_backfill(chat: int, from_msg: str, direction: str) -> None:
    from analyzetg.tg.sync import backfill as run_backfill

    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        msg_id = _parse_from_msg(from_msg)
        if msg_id is None:
            console.print(f"[red]{_t('tg_from_msg_must_be_link_or_id')}[/]")
            raise typer.Exit(1)
        count = await run_backfill(client, repo, chat_id=chat, from_msg_id=msg_id, direction=direction)
        console.print(
            f"[green]{_t('tg_backfilled_label')}[/] "
            f"{_tf('tg_backfilled_msg', n=count, chat=chat, direction=direction)}"
        )


# -------------------------------------------------------- interactive helpers


def _tui_choose(candidates: list) -> int | None:
    """Callable passed to resolver for ambiguous fuzzy matches."""
    if not sys.stdin.isatty():
        return None
    console.print(f"[yellow]{_t('tg_resolve_multiple_candidates')}[/]")
    for i, c in enumerate(candidates):
        console.print(
            _tf(
                "tg_resolve_candidate_line",
                i=i,
                title=c.title,
                username=c.username or "",
                score=c.score,
                kind=c.kind,
            )
        )
    try:
        raw = typer.prompt(_t("tg_resolve_index_prompt"), default="0")
        return int(raw)
    except (ValueError, EOFError):
        return None


def _sub_detail_panel(sub: Subscription, comments: str) -> Table:
    """Build a vertical key/value table summarizing one subscription.

    Shown after the user picks a subscription in `cmd_chats_manage` so
    they see the full state (preset, period, enrich, mark-read, post-to,
    transcribe flags, start cursor, comments link) before deciding what
    to do with it. Far easier to scan than the wide multi-column list.
    """
    enrich_display = sub.enrich_kinds if sub.enrich_kinds is not None else "(config defaults)"
    if enrich_display == "":
        enrich_display = "none"
    transcribe = ",".join(
        k
        for k, v in [
            ("voice", sub.transcribe_voice),
            ("vnote", sub.transcribe_videonote),
            ("video", sub.transcribe_video),
        ]
        if v
    )
    if sub.start_from_msg_id is not None:
        start = f"msg≥{sub.start_from_msg_id}"
    elif sub.start_from_date is not None:
        start = sub.start_from_date.strftime("%Y-%m-%d")
    else:
        start = "—"
    rows = [
        ("title", sub.title or "—"),
        ("chat_id", str(sub.chat_id)),
        ("thread_id", str(sub.thread_id)),
        ("kind", sub.source_kind),
        ("enabled", "yes" if sub.enabled else "no"),
        ("preset", sub.preset or "summary"),
        ("period", sub.period or "unread"),
        ("enrich", enrich_display),
        ("mark_read", "yes" if sub.mark_read else "no"),
        ("post_to", sub.post_to or "—"),
        ("comments", comments),
        ("transcribe", transcribe or "—"),
        ("start", start),
        ("added_at", sub.added_at.strftime("%Y-%m-%d %H:%M") if sub.added_at else "—"),
    ]
    t = Table(show_header=False, box=None, padding=(0, 2))
    t.add_column(style="dim", justify="right")
    t.add_column()
    for k, v in rows:
        t.add_row(k, v)
    return t


async def cmd_chats_manage() -> None:
    """Single interactive panel: pick a sub, view its details, act on it.

    Opens with just the subscription picker (one line per sub showing
    state + title + kind + comments link). Picking one prints a vertical
    detail panel for that subscription and then offers the action menu
    (toggle on/off, remove keeping messages, remove + purge). Loops
    until `← Done` / Ctrl-C / ESC. The only `chats` subcommands today
    are `add`, `manage`, and `run`.
    """
    import questionary

    from analyzetg.interactive import LIST_STYLE, _expand_printable_for_search

    _expand_printable_for_search()

    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        while True:
            subs = await repo.list_subscriptions(enabled_only=False)
            if not subs:
                console.print(f"[yellow]{_t('tg_chats_no_subs')}[/] {_t('tg_chats_use_add')}")
                return
            idx = await _comments_index(repo, subs)

            def _label(s: Subscription, _idx: dict = idx) -> str:
                state = _t("tg_sub_state_on") if s.enabled else _t("tg_sub_state_off")
                title = s.title or str(s.chat_id)
                kind_bit = s.source_kind
                if s.thread_id:
                    kind_bit += " " + _tf("tg_sub_thread_label", id=s.thread_id)
                comments = _comments_label(s, _idx)
                comments_bit = f"  {comments}" if comments and comments != "—" else ""
                return f"{state}  {title}  ({kind_bit}){comments_bit}"

            # Sentinel for the "← Done" choice. We can't use `value=None`
            # — questionary then falls back to using the choice title as
            # the picked value, which would make the unpack below explode
            # (`too many values to unpack`). A unique object survives the
            # round-trip cleanly.
            _DONE = object()
            choices = [questionary.Choice(_label(s), value=(int(s.chat_id), int(s.thread_id))) for s in subs]
            choices.append(questionary.Choice(_t("tg_chats_done_label"), value=_DONE))

            picked = await questionary.select(
                _tf("tg_chats_manage_q", n=len(subs)),
                choices=choices,
                style=LIST_STYLE,
                use_search_filter=True,
                use_jk_keys=False,
                instruction=_t("wiz_filter_instruction"),
            ).ask_async()
            # Ctrl-C / ESC → questionary returns None. "← Done" → _DONE.
            if picked is None or picked is _DONE:
                return
            chat_id, thread_id = picked
            sub = await repo.get_subscription(chat_id, thread_id)
            if not sub:
                console.print(f"[red]{_t('tg_sub_gone')}[/] chat={chat_id} thread={thread_id}")
                continue

            # Show the per-sub detail panel before the action menu so
            # the user has the full picture (preset / period / enrich /
            # transcribe / start cursor / etc.) instead of squinting at
            # a wide table row.
            console.print()
            console.print(_sub_detail_panel(sub, _comments_label(sub, idx)))
            console.print()

            # Per-sub action menu. Toggle label flips with current state
            # so the choice reads as the verb the user is invoking.
            # `value="back"` (not None) for the back row — same questionary
            # gotcha as above.
            toggle_label = _t("tg_sub_action_disable") if sub.enabled else _t("tg_sub_action_enable")
            action = await questionary.select(
                _tf("tg_sub_what_next_q", title=sub.title or sub.chat_id),
                choices=[
                    questionary.Choice(toggle_label, value="toggle"),
                    questionary.Choice(_t("tg_sub_action_remove_keep"), value="remove_keep"),
                    questionary.Choice(_t("tg_sub_action_remove_purge"), value="remove_purge"),
                    questionary.Choice(_t("tg_sub_back_label"), value="back"),
                ],
                style=LIST_STYLE,
            ).ask_async()
            if action is None or action == "back":
                continue
            if action == "toggle":
                await repo.set_subscription_enabled(chat_id, thread_id, not sub.enabled)
                done_key = "tg_sub_disabled" if sub.enabled else "tg_sub_enabled"
                console.print(f"[green]{_tf(done_key, chat_id=chat_id, thread_id=thread_id)}[/]")
            elif action in ("remove_keep", "remove_purge"):
                purge = action == "remove_purge"
                # Confirmation guard for purge — irreversible.
                if purge:
                    confirmed = bool(
                        await questionary.confirm(
                            _tf("tg_sub_purge_confirm_q", chat_id=chat_id),
                            default=False,
                        ).ask_async()
                    )
                    if not confirmed:
                        console.print(f"[dim]{_t('tg_sub_purge_skipped')}[/]")
                        continue
                await repo.remove_subscription(chat_id, thread_id, purge_messages=purge)
                console.print(
                    f"[green]{_tf('tg_sub_removed', chat_id=chat_id, thread_id=thread_id, purge=purge)}[/]"
                )
