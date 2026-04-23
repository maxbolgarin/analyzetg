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
        console.print(f"[red]Missing: {', '.join(missing)}.[/]")
        if env_path.exists():
            console.print(f"Checked .env at: [cyan]{env_path}[/]")
            console.print(
                "  Make sure the file has lines like `TELEGRAM_API_ID=123456` "
                "(no quotes, no spaces around `=`)."
            )
        else:
            console.print(
                f"No .env file at [cyan]{env_path}[/]. Copy .env.example to .env and fill in the values."
            )
        # Also show what we actually see in env for debugging
        seen = {
            k: bool(os.environ.get(k)) for k in ("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "OPENAI_API_KEY")
        }
        console.print(f"[dim]env seen: {seen}[/]")
        raise typer.Exit(1)

    # Ensure DB is migrated
    async with open_repo(settings.storage.data_path):
        pass

    # Telegram auth — delegate retries to client.start()
    client = build_client(settings)

    def _phone() -> str:
        return typer.prompt("Phone number (international, e.g. +491711234567)")

    def _code() -> str:
        return typer.prompt("Login code from Telegram")

    def _password() -> str:
        # Telethon retries this callback on PasswordHashInvalidError,
        # so a wrong 2FA password only reprompts the 2FA step.
        return typer.prompt("2FA password", hide_input=True)

    await client.connect()
    try:
        if await client.is_user_authorized():
            console.print("[green]Telegram session already authorized.[/]")
        else:
            await client.start(phone=_phone, code_callback=_code, password=_password)
            console.print("[green]Logged in.[/]")
    finally:
        await client.disconnect()

    # OpenAI smoke test
    console.print("Checking OpenAI API key ...")
    try:
        from openai import AsyncOpenAI

        oai = AsyncOpenAI(api_key=settings.openai.api_key, timeout=settings.openai.request_timeout_sec)
        await asyncio.wait_for(oai.models.list(), timeout=15)
        console.print("[green]OpenAI key OK.[/]")
    except Exception as e:
        console.print(f"[yellow]OpenAI check failed:[/] {e}")


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
        console.print(f"[dim]{shown} row(s)[/]")


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
    console.print("[dim]→ Listing dialogs...[/]")
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
        rows.append((unread, eid, k, t or "", u or "", stats["count"], stats["date_max"]))

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
    for col in ("id", "kind", "title", "username", "unread", "stored", "last_msg"):
        table.add_column(col)
    for unread, eid, k, t, u, stored, dmax in rows:
        table.add_row(
            str(eid),
            k,
            t,
            f"@{u}" if u else "",
            str(unread) if unread else "",
            str(stored) if stored else "",
            dmax.strftime("%Y-%m-%d %H:%M") if dmax else "",
        )
    console.print(table)
    hint_parts = [f"{len(rows)} row(s)"]
    if not show_all:
        hint_parts.append("default filter: unread + forums/groups/supergroups")
        hint_parts.append("pass --all to see everything")
    console.print(f"[dim]{'. '.join(hint_parts)}.[/]")
    console.print("[dim]Use `describe <ref>` for details on one chat.[/]")


async def _describe_one(client, repo, ref: str) -> None:
    resolved = await resolve(client, repo, ref, prompt_choice=_tui_choose)
    chat_id = resolved.chat_id
    kind = resolved.kind

    # Pull live dialog-level state (unread, read marker, last message date).
    unread_count, read_marker = await get_unread_state(client, chat_id)
    last_msg_date = await _fetch_last_msg_date(client, chat_id)

    # Header
    badge = f"[bold]{resolved.title or chat_id}[/]"
    console.print(f"\n{badge} [dim](id={chat_id}, kind={kind})[/]")

    # --- Left/right-ish labeled properties
    def _row(label: str, value: str | None, *, dim_label: bool = True) -> None:
        if value is None or value == "":
            return
        label_fmt = f"[dim]{label:>14}:[/]" if dim_label else f"{label:>14}:"
        console.print(f"  {label_fmt} {value}")

    if resolved.username:
        _row("username", f"@{resolved.username} — https://t.me/{resolved.username}")
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
            console.print("[bold]Top senders[/]:")
            for row in top:
                console.print(f"  {row['sender_name']} — {row['count']}")
    else:
        console.print("\n[dim]Local DB: no messages stored for this chat yet.[/]")


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
            console.print(f"[yellow]{ref.title}[/] is not a forum group.")
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
        console.print(f"[dim]{len(topics)} topic(s)[/]")


# -------------------------------------------------------------------- resolve


async def cmd_resolve(ref: str) -> None:
    settings = get_settings()
    parsed = parse(ref)
    console.print(f"[bold]Parsed:[/] {parsed}")
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        try:
            resolved = await resolve(client, repo, ref, prompt_choice=_tui_choose)
            console.print(f"[bold green]Resolved:[/] {resolved}")
        except Exception as e:
            console.print(f"[red]Resolve failed:[/] {e}")


# --------------------------------------------------------------- channel-info


async def cmd_channel_info(ref: str) -> None:
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        resolved = await resolve(client, repo, ref, prompt_choice=_tui_choose)
        info = await get_full_channel_info(client, resolved.chat_id)
        console.print(f"[bold]{resolved.title}[/] (id={resolved.chat_id}, kind={resolved.kind})")
        console.print(f"  participants: {info['participants_count']}")
        console.print(f"  linked_chat_id: {info['linked_chat_id']}")
        if info.get("about"):
            console.print(f"  about: {info['about']}")


# ------------------------------------------------------------------- chats.*


async def cmd_chats_add(
    *,
    ref: str,
    from_date: str | None,
    from_msg: str | None,
    last: int | None,
    full_history: bool,
    thread: int | None,
    all_topics: bool,
    with_comments: bool,
    join: bool,
    no_transcribe: bool,
) -> None:
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        resolved = await resolve(client, repo, ref, join=join, prompt_choice=_tui_choose)

        from_msg_id = _parse_from_msg(from_msg)
        from_dt = datetime.strptime(from_date, "%Y-%m-%d") if from_date else None
        if full_history:
            from_dt = datetime(1970, 1, 1)
            from_msg_id = None

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
                    )
                )

        if with_comments and resolved.kind == "channel":
            linked = await get_linked_chat_id(client, resolved.chat_id)
            if linked is None:
                console.print(f"[yellow]Channel[/] {resolved.title} has no linked discussion group.")
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
                    )
                )

        for s in subs_to_add:
            await repo.upsert_subscription(s)
        console.print(f"[green]Added[/] {len(subs_to_add)} subscription(s).")
        for s in subs_to_add:
            console.print(f"  - chat={s.chat_id} thread={s.thread_id} kind={s.source_kind} title={s.title}")

        # Note --last: we apply it by pulling last N messages immediately at next sync;
        # we record start_from_msg_id = (top_msg_id - last) after the first sync pass.
        if last is not None:
            console.print(f"[dim]--last {last} will take effect on next sync (start from newest-N).[/]")
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


async def cmd_chats_list(enabled_only: bool) -> None:
    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        subs = await repo.list_subscriptions(enabled_only=enabled_only)
        t = Table(title="Subscriptions")
        for col in ("chat_id", "thread", "kind", "title", "enabled", "transcribe", "start"):
            t.add_column(col)
        for s in subs:
            transcribe = ",".join(
                k
                for k, v in [
                    ("voice", s.transcribe_voice),
                    ("vnote", s.transcribe_videonote),
                    ("video", s.transcribe_video),
                ]
                if v
            )
            start = ""
            if s.start_from_msg_id is not None:
                start = f"msg≥{s.start_from_msg_id}"
            elif s.start_from_date is not None:
                start = s.start_from_date.strftime("%Y-%m-%d")
            t.add_row(
                str(s.chat_id),
                str(s.thread_id),
                s.source_kind,
                s.title or "",
                "yes" if s.enabled else "no",
                transcribe or "-",
                start,
            )
        console.print(t)
        console.print(f"[dim]{len(subs)} subscription(s)[/]")


# ----------------------------------------------------------- sync / backfill


async def cmd_sync(chat: int | None, thread: int | None, dry_run: bool) -> None:
    from analyzetg.tg.sync import sync_subscription

    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        subs = await repo.list_subscriptions(enabled_only=True)
        if chat is not None:
            subs = [s for s in subs if s.chat_id == chat and (thread is None or s.thread_id == thread)]
        if not subs:
            console.print("[yellow]No matching subscriptions.[/]")
            return
        total = 0
        for s in subs:
            added = await sync_subscription(client, repo, s, dry_run=dry_run)
            console.print(
                f"  [cyan]sync[/] chat={s.chat_id} thread={s.thread_id} -> "
                f"{'would fetch' if dry_run else 'fetched'} {added} new msgs"
            )
            total += added
        console.print(f"[green]Done.[/] {total} message(s).")


async def cmd_backfill(chat: int, from_msg: str, direction: str) -> None:
    from analyzetg.tg.sync import backfill as run_backfill

    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        msg_id = _parse_from_msg(from_msg)
        if msg_id is None:
            console.print("[red]--from-msg must be a message link or msg_id.[/]")
            raise typer.Exit(1)
        count = await run_backfill(client, repo, chat_id=chat, from_msg_id=msg_id, direction=direction)
        console.print(f"[green]Backfilled[/] {count} message(s) chat={chat} direction={direction}.")


# -------------------------------------------------------- interactive helpers


def _tui_choose(candidates: list) -> int | None:
    """Callable passed to resolver for ambiguous fuzzy matches."""
    if not sys.stdin.isatty():
        return None
    console.print("[yellow]Multiple candidates, pick one:[/]")
    for i, c in enumerate(candidates):
        console.print(f"  [{i}] {c.title} @{c.username or ''} (score {c.score}, {c.kind})")
    try:
        raw = typer.prompt("Index (Enter = top match)", default="0")
        return int(raw)
    except (ValueError, EOFError):
        return None
