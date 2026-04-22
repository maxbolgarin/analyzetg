"""analyzetg CLI (Typer). Commands are wired in later phases; stubs here
declare the final signatures so UX is stable from day one."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console

from analyzetg.config import get_settings
from analyzetg.db.repo import open_repo
from analyzetg.util.logging import setup_logging

app = typer.Typer(
    name="analyzetg",
    help="Pull Telegram chats/channels, transcribe voice, and analyze via OpenAI — all local.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
chats_app = typer.Typer(help="Manage subscriptions (what to sync).", no_args_is_help=True)
cache_app = typer.Typer(help="Analysis cache maintenance.", no_args_is_help=True)
app.add_typer(chats_app, name="chats")
app.add_typer(cache_app, name="cache")

console = Console()


@app.callback()
def _root(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
) -> None:
    """Global options. Runs before every subcommand."""
    setup_logging(verbose=verbose)


def _run(coro) -> None:
    asyncio.run(coro)


# =============================================================== 5.1 Setup & nav


@app.command()
def init() -> None:
    """Interactive first-time setup: log in to Telegram and verify OpenAI key."""
    from analyzetg.tg.commands import cmd_init

    _run(cmd_init())


@app.command()
def dialogs(
    search: str | None = typer.Option(None, "--search", help="Substring filter on title/username."),
    kind: str | None = typer.Option(None, "--kind", help="user | group | channel | forum"),
    limit: int = typer.Option(50, "--limit", help="Max rows."),
) -> None:
    """List available dialogs (chats the user is in)."""
    from analyzetg.tg.commands import cmd_dialogs

    _run(cmd_dialogs(search=search, kind=kind, limit=limit))


@app.command()
def topics(
    chat_ref: str | None = typer.Argument(
        None, help="Chat reference (link/@user/title). For a numeric id use --chat."
    ),
    chat: int | None = typer.Option(None, "--chat", help="Numeric chat_id (-100-prefixed)."),
) -> None:
    """List forum topics for a supergroup with topics enabled."""
    from analyzetg.tg.commands import cmd_topics

    if chat_ref is None and chat is None:
        console.print("[red]Provide a chat reference or --chat <id>.[/]")
        raise typer.Exit(2)
    _run(cmd_topics(chat_ref if chat_ref is not None else str(chat)))


@app.command()
def resolve(anything: str = typer.Argument(..., help="Any link/username/id/fuzzy string.")) -> None:
    """Diagnostic: parse a reference, resolve it, and show what we'd do with it."""
    from analyzetg.tg.commands import cmd_resolve

    _run(cmd_resolve(anything))


@app.command("channel-info")
def channel_info(ref: str = typer.Argument(..., help="Channel reference.")) -> None:
    """Show channel's linked discussion group and subscriber count."""
    from analyzetg.tg.commands import cmd_channel_info

    _run(cmd_channel_info(ref))


# =========================================================== 5.2 Subscriptions


@chats_app.command("add")
def chats_add(
    ref: str = typer.Argument(..., help="Chat reference."),
    from_date: str | None = typer.Option(None, "--from-date", help="YYYY-MM-DD"),
    from_msg: str | None = typer.Option(None, "--from-msg", help="Message link or msg_id."),
    last: int | None = typer.Option(None, "--last", help="Backfill last N messages."),
    full_history: bool = typer.Option(False, "--full-history", help="Sync the whole chat (danger)."),
    thread: int | None = typer.Option(None, "--thread", help="Specific forum topic id."),
    all_topics: bool = typer.Option(False, "--all-topics", help="Subscribe to every forum topic."),
    with_comments: bool = typer.Option(False, "--with-comments", help="Channel + discussion group."),
    join: bool = typer.Option(False, "--join", help="Auto-join via invite link if required."),
    no_transcribe: bool = typer.Option(False, "--no-transcribe", help="Disable transcription for this sub."),
) -> None:
    """Add a subscription (chat / topic / channel with comments)."""
    from analyzetg.tg.commands import cmd_chats_add

    _run(
        cmd_chats_add(
            ref=ref,
            from_date=from_date,
            from_msg=from_msg,
            last=last,
            full_history=full_history,
            thread=thread,
            all_topics=all_topics,
            with_comments=with_comments,
            join=join,
            no_transcribe=no_transcribe,
        )
    )


@chats_app.command("list")
def chats_list(enabled_only: bool = typer.Option(False, "--enabled-only")) -> None:
    """List all subscriptions."""
    from analyzetg.tg.commands import cmd_chats_list

    _run(cmd_chats_list(enabled_only=enabled_only))


@chats_app.command("enable")
def chats_enable(
    chat_id: int = typer.Argument(...),
    thread: int = typer.Option(0, "--thread"),
) -> None:
    """Enable a subscription."""
    _run(_set_enabled(chat_id, thread, True))


@chats_app.command("disable")
def chats_disable(
    chat_id: int = typer.Argument(...),
    thread: int = typer.Option(0, "--thread"),
) -> None:
    """Disable a subscription (keeps data)."""
    _run(_set_enabled(chat_id, thread, False))


@chats_app.command("remove")
def chats_remove(
    chat_id: int = typer.Argument(...),
    thread: int = typer.Option(0, "--thread"),
    purge_messages: bool = typer.Option(False, "--purge-messages"),
) -> None:
    """Remove a subscription (optionally delete stored messages)."""
    _run(_remove_sub(chat_id, thread, purge_messages))


async def _set_enabled(chat_id: int, thread: int, enabled: bool) -> None:
    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        sub = await repo.get_subscription(chat_id, thread)
        if not sub:
            console.print(f"[red]No subscription for chat={chat_id} thread={thread}[/]")
            raise typer.Exit(1)
        await repo.set_subscription_enabled(chat_id, thread, enabled)
        console.print(f"[green]OK[/] subscription chat={chat_id} thread={thread} enabled={enabled}")


async def _remove_sub(chat_id: int, thread: int, purge: bool) -> None:
    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        await repo.remove_subscription(chat_id, thread, purge_messages=purge)
        console.print(f"[green]Removed[/] chat={chat_id} thread={thread} purged={purge}")


# ================================================================ 5.3 Sync


@app.command()
def sync(
    chat: int | None = typer.Option(None, "--chat"),
    thread: int | None = typer.Option(None, "--thread"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Incrementally fetch new messages for all (or one) subscriptions."""
    from analyzetg.tg.commands import cmd_sync

    _run(cmd_sync(chat=chat, thread=thread, dry_run=dry_run))


@app.command()
def backfill(
    chat: int = typer.Option(..., "--chat"),
    from_msg: str = typer.Option(..., "--from-msg"),
    direction: str = typer.Option("back", "--direction", help="back | forward"),
) -> None:
    """One-shot history backfill starting from a specific message."""
    from analyzetg.tg.commands import cmd_backfill

    _run(cmd_backfill(chat=chat, from_msg=from_msg, direction=direction))


# ========================================================= 5.3 Transcriptions


@app.command()
def transcribe(
    chat: int | None = typer.Option(None, "--chat"),
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD"),
    until: str | None = typer.Option(None, "--until", help="YYYY-MM-DD"),
    model: str | None = typer.Option(None, "--model"),
    max_duration: int | None = typer.Option(None, "--max-duration"),
    limit: int | None = typer.Option(None, "--limit"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Transcribe pending voice / videonote / video messages via OpenAI Audio."""
    from analyzetg.media.commands import cmd_transcribe

    _run(
        cmd_transcribe(
            chat=chat,
            since=since,
            until=until,
            model=model,
            max_duration=max_duration,
            limit=limit,
            dry_run=dry_run,
        )
    )


# =================================================================== 5.4 Analyze


@app.command()
def analyze(
    ref: str | None = typer.Argument(
        None,
        help=(
            "Chat reference: @user, t.me link, title (fuzzy), or numeric id. "
            "For a negative numeric id use `--` to separate from flags, e.g. "
            "`analyzetg analyze -- -1001234567890`. Omit to pick every dialog "
            "with unread messages (interactive)."
        ),
    ),
    thread: int | None = typer.Option(None, "--thread", help="Forum-topic id."),
    from_msg: str | None = typer.Option(None, "--from-msg", help="Start at this msg_id (or a message link)."),
    full_history: bool = typer.Option(
        False, "--full-history", help="Analyze the whole chat, not just unread."
    ),
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD"),
    until: str | None = typer.Option(None, "--until", help="YYYY-MM-DD"),
    last_days: int | None = typer.Option(None, "--last-days"),
    preset: str = typer.Option("summary", "--preset"),
    prompt_file: Path | None = typer.Option(None, "--prompt-file"),
    model: str | None = typer.Option(None, "--model"),
    filter_model: str | None = typer.Option(None, "--filter-model"),
    output: Path | None = typer.Option(None, "--output", "-o"),
    no_cache: bool = typer.Option(False, "--no-cache"),
    include_transcripts: bool = typer.Option(True, "--include-transcripts/--text-only"),
    min_msg_chars: int | None = typer.Option(None, "--min-msg-chars"),
) -> None:
    """Analyze a chat. Default window = messages since your Telegram read marker."""
    from analyzetg.analyzer.commands import cmd_analyze

    _run(
        cmd_analyze(
            ref=ref,
            thread=thread,
            from_msg=from_msg,
            full_history=full_history,
            since=since,
            until=until,
            last_days=last_days,
            preset=preset,
            prompt_file=prompt_file,
            model=model,
            filter_model=filter_model,
            output=output,
            no_cache=no_cache,
            include_transcripts=include_transcripts,
            min_msg_chars=min_msg_chars,
        )
    )


# ============================================================== 5.5 Maintenance


@app.command()
def stats(
    since: str | None = typer.Option(None, "--since"),
    by: str = typer.Option("preset", "--by", help="chat | preset | model | day | kind"),
) -> None:
    """Aggregate API spend, cache hit rate and run counts."""
    from analyzetg.analyzer.commands import cmd_stats

    _run(cmd_stats(since=since, by=by))


@cache_app.command("purge")
def cache_purge(
    older_than: str = typer.Option("30d", "--older-than", help="Nd"),
    preset: str | None = typer.Option(None, "--preset"),
    model: str | None = typer.Option(None, "--model"),
) -> None:
    """Delete cached analysis results by age and filters."""
    _run(_cache_purge(older_than, preset, model))


async def _cache_purge(older_than: str, preset: str | None, model: str | None) -> None:
    settings = get_settings()
    days = _parse_duration_days(older_than)
    async with open_repo(settings.storage.data_path) as repo:
        removed = await repo.cache_purge(older_than_days=days, preset=preset, model=model)
        console.print(f"[green]Purged[/] {removed} analysis_cache rows older than {days} days.")


def _parse_duration_days(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("d"):
        return int(s[:-1])
    if s.endswith("w"):
        return int(s[:-1]) * 7
    return int(s)


@app.command()
def cleanup(
    retention: str = typer.Option("90d", "--retention"),
    chat: int | None = typer.Option(None, "--chat"),
    keep_transcripts: bool = typer.Option(True, "--keep-transcripts/--no-keep-transcripts"),
) -> None:
    """Null-out old message texts; keep transcripts/analysis cache."""
    _run(_cleanup(retention, chat, keep_transcripts))


async def _cleanup(retention: str, chat: int | None, keep_transcripts: bool) -> None:
    settings = get_settings()
    days = _parse_duration_days(retention)
    async with open_repo(settings.storage.data_path) as repo:
        redacted = await repo.redact_old_messages(
            retention_days=days,
            chat_id=chat,
            keep_transcripts=keep_transcripts,
        )
        console.print(
            f"[green]Redacted[/] {redacted} messages older than {days} days"
            f"{' (transcripts kept)' if keep_transcripts else ''}."
        )


@app.command()
def export(
    chat: int = typer.Option(..., "--chat"),
    fmt: str = typer.Option("md", "--format", help="jsonl | csv | md"),
    output: Path = typer.Option(..., "--output"),
    since: str | None = typer.Option(None, "--since"),
    until: str | None = typer.Option(None, "--until"),
) -> None:
    """Export already-synced messages from the local DB to jsonl / csv / md."""
    from analyzetg.export.commands import cmd_export

    _run(cmd_export(chat=chat, fmt=fmt, output=output, since=since, until=until))


@app.command()
def dump(
    ref: str | None = typer.Argument(
        None,
        help=(
            "Chat reference: @user, t.me link, title (fuzzy), or numeric id. "
            "For a negative numeric id use `--` to separate from flags, e.g. "
            "`analyzetg dump -- -1001234567890 -o out.md`. Omit to pick every "
            "dialog with unread messages (interactive)."
        ),
    ),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file (single chat) or directory (no-ref mode).",
    ),
    fmt: str = typer.Option("md", "--format", help="md | jsonl | csv"),
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD"),
    until: str | None = typer.Option(None, "--until", help="YYYY-MM-DD"),
    last_days: int | None = typer.Option(None, "--last-days", help="Shortcut for --since now-N."),
    full_history: bool = typer.Option(False, "--full-history", help="Pull the whole chat."),
    thread: int | None = typer.Option(
        None,
        "--thread",
        help="Forum-topic id. Run `analyzetg topics <ref>` first to list topic ids.",
    ),
    from_msg: str | None = typer.Option(None, "--from-msg", help="Start at this msg_id (or a message link)."),
    join: bool = typer.Option(False, "--join", help="Join via invite link if required."),
    with_transcribe: bool = typer.Option(
        False, "--with-transcribe", help="Transcribe voice/videonote before export (OpenAI Audio)."
    ),
    include_transcripts: bool = typer.Option(
        True,
        "--include-transcripts/--text-only",
        help="Include transcripts in the output (default on).",
    ),
) -> None:
    """Dump chat history to a file. Default window = messages since your Telegram read marker.

    Precedence of start-point flags: --full-history > --from-msg >
    --since/--until/--last-days > (default: unread). Use --with-transcribe
    to fill voice/videonote transcripts before writing the file.
    """
    from analyzetg.export.commands import cmd_dump

    _run(
        cmd_dump(
            ref=ref,
            output=output,
            fmt=fmt,
            since=since,
            until=until,
            last_days=last_days,
            full_history=full_history,
            thread=thread,
            from_msg=from_msg,
            join=join,
            with_transcribe=with_transcribe,
            include_transcripts=include_transcripts,
        )
    )


# --------------------------------------------------------------- shared utilities


def parse_ymd(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d")


def compute_period(
    since: str | None, until: str | None, last_days: int | None
) -> tuple[datetime | None, datetime | None]:
    if last_days:
        until_dt = datetime.now()
        since_dt = until_dt - timedelta(days=last_days)
        return since_dt, until_dt
    return parse_ymd(since), parse_ymd(until)


if __name__ == "__main__":
    app()
