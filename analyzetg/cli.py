"""analyzetg CLI (Typer). Commands are wired in later phases; stubs here
declare the final signatures so UX is stable from day one."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from analyzetg.config import get_settings
from analyzetg.db.repo import open_repo
from analyzetg.util.logging import setup_logging

app = typer.Typer(
    name="analyzetg",
    help="Pull Telegram chats, enrich media (voice/images/docs/links), and analyze via OpenAI — all local.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)

PANEL_MAIN = "Main"
PANEL_SYNC = "Sync & subscriptions"
PANEL_MAINT = "Maintenance"

chats_app = typer.Typer(help="Manage subscriptions (what to sync).", no_args_is_help=True)
cache_app = typer.Typer(help="Analysis cache maintenance.", no_args_is_help=True)
app.add_typer(chats_app, name="chats", rich_help_panel=PANEL_SYNC)
app.add_typer(cache_app, name="cache", rich_help_panel=PANEL_MAINT)

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


@app.command(rich_help_panel=PANEL_MAIN)
def init() -> None:
    """Interactive first-time setup: log in to Telegram and verify OpenAI key."""
    from analyzetg.tg.commands import cmd_init

    _run(cmd_init())


@app.command(rich_help_panel=PANEL_MAIN)
def describe(
    ref: str | None = typer.Argument(
        None,
        help=(
            "Chat reference. Without it, prints an overview of dialogs. "
            "For a chat: shows kind, username, stats, and (for forums) topics. "
            "For a channel: shows linked discussion group and subscriber count."
        ),
    ),
    kind: str | None = typer.Option(
        None,
        "--kind",
        help="Filter overview by kind: user | group | supergroup | channel | forum.",
    ),
    search: str | None = typer.Option(None, "--search", help="Substring filter on title/username."),
    limit: int | None = typer.Option(None, "--limit", help="Max rows in overview."),
    show_all: bool = typer.Option(
        False,
        "--all",
        help="Show every dialog, including read ones and all kinds. "
        "Default overview: chats with unread messages in forum/group/supergroup.",
    ),
) -> None:
    """List chats (no ref) or inspect one chat (with ref).

    Default overview shows unread forums/groups/supergroups — the places
    real discussion happens. Use --all to see everything, or narrow with
    --kind / --search / --limit. With a ref, forums get a topics table
    and channels get linked-discussion + subscriber count.
    """
    from analyzetg.tg.commands import cmd_describe

    _run(
        cmd_describe(
            ref,
            kind=kind,
            search=search,
            limit=limit,
            show_all=show_all,
        )
    )


@app.command(rich_help_panel=PANEL_MAINT)
def folders() -> None:
    """List your Telegram folders (for use with `analyze --folder NAME` / `dump --folder NAME`)."""
    _run(_list_folders())


# --- Hidden compatibility aliases: the consolidated `describe` absorbs these.
# Kept callable so existing scripts don't break.


@app.command(hidden=True)
def dialogs(
    search: str | None = typer.Option(None, "--search"),
    kind: str | None = typer.Option(None, "--kind"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """Deprecated: use `describe` instead."""
    from analyzetg.tg.commands import cmd_dialogs

    _run(cmd_dialogs(search=search, kind=kind, limit=limit))


@app.command(hidden=True)
def topics(
    chat_ref: str | None = typer.Argument(None),
    chat: int | None = typer.Option(None, "--chat"),
) -> None:
    """Deprecated: use `describe <ref>` instead."""
    from analyzetg.tg.commands import cmd_topics

    if chat_ref is None and chat is None:
        console.print("[red]Provide a chat reference or --chat <id>.[/]")
        raise typer.Exit(2)
    _run(cmd_topics(chat_ref if chat_ref is not None else str(chat)))


@app.command(hidden=True)
def resolve(anything: str = typer.Argument(...)) -> None:
    """Diagnostic: parse a reference and show the resolution path."""
    from analyzetg.tg.commands import cmd_resolve

    _run(cmd_resolve(anything))


@app.command("channel-info", hidden=True)
def channel_info(ref: str = typer.Argument(...)) -> None:
    """Deprecated: use `describe <channel-ref>` instead."""
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


async def _list_folders() -> None:
    from rich.table import Table

    from analyzetg.tg.client import tg_client
    from analyzetg.tg.folders import list_folders

    settings = get_settings()
    async with tg_client(settings) as client:
        folders = await list_folders(client)

    if not folders:
        console.print("[yellow]No folders defined in this Telegram account.[/]")
        return
    t = Table(title="Telegram folders")
    t.add_column("id", justify="right")
    t.add_column("title")
    t.add_column("icon")
    t.add_column("chats", justify="right")
    t.add_column("kind")
    for f in folders:
        kind = (
            "chatlist"
            if f.is_chatlist
            else ("rule-based" if f.has_rule_based_inclusion and not f.include_chat_ids else "explicit")
        )
        t.add_row(
            str(f.id),
            f.title,
            f.emoticon or "",
            str(len(f.include_chat_ids)),
            kind,
        )
    console.print(t)
    console.print(
        '[dim]Use with:[/] [cyan]atg analyze --folder "Alpha"[/] — batch-analyze unread chats in that folder.'
    )


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


@app.command(rich_help_panel=PANEL_SYNC)
def sync(
    chat: int | None = typer.Option(None, "--chat"),
    thread: int | None = typer.Option(None, "--thread"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Incrementally fetch new messages for all (or one) subscriptions."""
    from analyzetg.tg.commands import cmd_sync

    _run(cmd_sync(chat=chat, thread=thread, dry_run=dry_run))


@app.command(hidden=True)
def backfill(
    chat: int = typer.Option(..., "--chat"),
    from_msg: str = typer.Option(..., "--from-msg"),
    direction: str = typer.Option("back", "--direction", help="back | forward"),
) -> None:
    """One-shot history backfill starting from a specific message.

    Niche helper — most users want `analyze --from-msg <id>` or
    `dump --from-msg <id>` instead.
    """
    from analyzetg.tg.commands import cmd_backfill

    _run(cmd_backfill(chat=chat, from_msg=from_msg, direction=direction))


# =================================================================== 5.4 Analyze


@app.command(rich_help_panel=PANEL_MAIN)
def analyze(
    ref: str | None = typer.Argument(
        None,
        help=(
            "Chat reference: @user, t.me link, title (fuzzy), or numeric id. "
            "A message link like t.me/c/ID/MSG is treated as single-message "
            "mode (analyze just that one message, auto-transcribing voice/video). "
            "For a negative numeric id use `--` to separate from flags, e.g. "
            "`analyzetg analyze -- -1001234567890`. Omit to pick every dialog "
            "with unread messages (interactive)."
        ),
    ),
    thread: int | None = typer.Option(None, "--thread", help="Forum-topic id."),
    msg: str | None = typer.Option(
        None,
        "--msg",
        help="Analyze just one message (id or link). Auto-transcribes voice/video if needed.",
    ),
    from_msg: str | None = typer.Option(None, "--from-msg", help="Start at this msg_id (or a message link)."),
    full_history: bool = typer.Option(
        False, "--full-history", help="Analyze the whole chat, not just unread."
    ),
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD"),
    until: str | None = typer.Option(None, "--until", help="YYYY-MM-DD"),
    last_days: int | None = typer.Option(None, "--last-days"),
    preset: str | None = typer.Option(
        None,
        "--preset",
        help="Analysis preset (default: 'summary' for chats, 'single_msg' when analyzing one message).",
    ),
    prompt_file: Path | None = typer.Option(None, "--prompt-file"),
    model: str | None = typer.Option(None, "--model"),
    filter_model: str | None = typer.Option(None, "--filter-model"),
    output: Path | None = typer.Option(None, "--output", "-o"),
    console_out: bool = typer.Option(
        False,
        "--console",
        "-c",
        help="Render the result in the terminal (pretty-printed markdown) instead of saving a file.",
    ),
    save: bool = typer.Option(
        False,
        "--save",
        "-s",
        help="Save to the default reports/ path (skips the interactive output picker).",
    ),
    mark_read: bool | None = typer.Option(
        None,
        "--mark-read/--no-mark-read",
        help="Tri-state: --mark-read advances Telegram's marker; --no-mark-read explicitly keeps unread and skips the prompt; no flag → ask interactively.",
    ),
    all_flat: bool = typer.Option(
        False,
        "--all-flat",
        help="Forum only: analyze the whole forum as one chat. Needs an explicit period flag.",
    ),
    all_per_topic: bool = typer.Option(
        False,
        "--all-per-topic",
        help="Forum only: one report per topic. Reports land in reports/{chat}/.",
    ),
    no_cache: bool = typer.Option(False, "--no-cache"),
    include_transcripts: bool = typer.Option(True, "--include-transcripts/--text-only"),
    min_msg_chars: int | None = typer.Option(None, "--min-msg-chars"),
    enrich: str | None = typer.Option(
        None,
        "--enrich",
        help=(
            "Comma-separated media enrichments to enable: "
            "voice, videonote, video, image, doc, link. "
            "Overrides config defaults for this run. "
            "Example: --enrich=voice,image,link"
        ),
    ),
    enrich_all: bool = typer.Option(
        False,
        "--enrich-all",
        help="Enable every enrichment (voice/videonote/video/image/doc/link). Spendy; use for exploratory runs.",
    ),
    no_enrich: bool = typer.Option(
        False,
        "--no-enrich",
        help="Disable all enrichments for this run, even those that would default on.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip interactive confirmations (per-topic Y/n, batch-of-N-chats Y/n). Useful for scripting or when the prompt-toolkit → typer.confirm handoff acts up in your terminal.",
    ),
    folder: str | None = typer.Option(
        None,
        "--folder",
        help=(
            "Batch-analyze all unread chats inside this Telegram folder "
            "(dialog filter). Case-insensitive match on folder title. "
            "Only meaningful without <ref>."
        ),
    ),
    max_cost: float | None = typer.Option(
        None,
        "--max-cost",
        help=(
            "Abort if the upper-bound estimated USD cost of this run exceeds "
            "N (estimate uses preset models, message count, and your pricing "
            "table). Pass with --yes to abort silently; without --yes you'll "
            "be asked to confirm an over-budget run."
        ),
    ),
    post_saved: bool = typer.Option(
        False,
        "--post-saved",
        help=(
            "After analysis finishes, also post the result to your Telegram "
            "Saved Messages chat (split into 4096-char chunks if needed). "
            "Markdown-friendly: rendered as monospace by Telegram."
        ),
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Resolve the chat, run backfill, count messages, print the cost estimate, "
            "and exit before any LLM call. Useful before --enrich-all / --full-history."
        ),
    ),
    cite_context: int = typer.Option(
        0,
        "--cite-context",
        help=(
            "After analysis, append a `## Источники` section to the saved report "
            "with N messages of context around every cited [#msg_id](url). "
            "0 (default) = off; 3 = three before + three after. Capped at 30 citations."
        ),
    ),
    self_check: bool = typer.Option(
        False,
        "--self-check",
        help=(
            "After analysis, run a cheap-model audit pass that lists unsupported "
            "claims under `## Verification`. Adds ~10% to cost. Useful when you'll "
            "act on the report without re-reading the source messages."
        ),
    ),
    by: str | None = typer.Option(
        None,
        "--by",
        help=(
            "Filter to messages from one sender. Substring match on sender_name "
            "(case-insensitive) or numeric sender_id. Composes with all other filters."
        ),
    ),
    post_to: str | None = typer.Option(
        None,
        "--post-to",
        help=(
            "After analysis, post the result to this chat (any chat ref: @user, "
            "t.me link, fuzzy title, numeric id, or 'me' for Saved Messages). "
            "Generalization of --post-saved (which is now sugar for --post-to=me)."
        ),
    ),
    repeat_last: bool = typer.Option(
        False,
        "--repeat-last",
        help=(
            "Look up the saved flags from the most recent successful analyze on "
            "<ref> and re-use them. Explicit CLI flags on this run still win "
            "(e.g. `--repeat-last --no-cache` to bust the cache while keeping "
            "everything else)."
        ),
    ),
) -> None:
    """Analyze a chat. Default window = messages since your Telegram read marker.

    For forum chats: `--thread N` targets one topic, `--all-flat` treats
    the forum as one chat (needs --last-days / --full-history),
    `--all-per-topic` runs one analysis per topic.

    Without `<ref>` and with `--folder NAME`: batch-analyzes every chat in
    that Telegram folder that has unread messages (skips the wizard).
    """
    from analyzetg.analyzer.commands import cmd_analyze

    _run(
        cmd_analyze(
            ref=ref,
            thread=thread,
            msg=msg,
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
            console_out=console_out,
            save_default=save,
            mark_read=mark_read,
            no_cache=no_cache,
            include_transcripts=include_transcripts,
            min_msg_chars=min_msg_chars,
            enrich=enrich,
            enrich_all=enrich_all,
            no_enrich=no_enrich,
            yes=yes,
            all_flat=all_flat,
            all_per_topic=all_per_topic,
            folder=folder,
            max_cost=max_cost,
            post_saved=post_saved,
            dry_run=dry_run,
            cite_context=cite_context,
            self_check=self_check,
            by=by,
            post_to=post_to,
            repeat_last=repeat_last,
        )
    )


# ============================================================== 5.4b Download media


@app.command("download-media", hidden=True)
def download_media(
    ref: str = typer.Argument(
        ...,
        help=(
            "Chat reference: @user, t.me link, title (fuzzy), or numeric id. "
            "Saves photos/voice/video/documents from this chat to disk."
        ),
    ),
    thread: int | None = typer.Option(None, "--thread", help="Forum-topic id."),
    types: str | None = typer.Option(
        None,
        "--types",
        help=("Comma-separated subset: voice, videonote, video, photo, doc. Default: all five."),
    ),
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD"),
    until: str | None = typer.Option(None, "--until", help="YYYY-MM-DD"),
    last_days: int | None = typer.Option(None, "--last-days"),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Base output dir (default: reports/). Files land under reports/<chat-slug>/media/.",
    ),
    limit: int | None = typer.Option(None, "--limit", help="Max files to download this run."),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Re-download even if a file for the same msg_id already exists.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview counts + sample without writing."),
) -> None:
    """Download raw media files (photos, voice, video, documents) from a chat.

    Works off messages already in the local DB — run [cyan]atg sync[/] or
    [cyan]atg analyze[/] first if you need the latest messages. Safe to
    re-run: files are skipped when they already exist on disk (pass
    [cyan]--overwrite[/] to force). No OpenAI calls; no cost beyond
    Telegram download bandwidth.
    """
    from analyzetg.media.commands import cmd_download_media

    _run(
        cmd_download_media(
            ref=ref,
            thread=thread,
            types=types,
            since=since,
            until=until,
            last_days=last_days,
            output=output,
            limit=limit,
            overwrite=overwrite,
            dry_run=dry_run,
        )
    )


# ============================================================== 5.5 Maintenance


@app.command(rich_help_panel=PANEL_MAINT)
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
    vacuum: bool = typer.Option(False, "--vacuum", help="Run VACUUM after purge to reclaim disk."),
) -> None:
    """Delete cached analysis results by age and filters."""
    _run(_cache_purge(older_than, preset, model, vacuum))


async def _cache_purge(
    older_than: str,
    preset: str | None,
    model: str | None,
    vacuum: bool,
) -> None:
    settings = get_settings()
    days = _parse_duration_days(older_than)
    if days <= 0:
        console.print("[yellow]Skipped[/] cache purge: --older-than must be greater than 0 days.")
        return
    async with open_repo(settings.storage.data_path) as repo:
        removed = await repo.cache_purge(older_than_days=days, preset=preset, model=model)
        console.print(f"[green]Purged[/] {removed} analysis_cache rows older than {days} days.")
        if vacuum:
            reclaimed = await repo.vacuum()
            console.print(f"[green]Vacuumed[/] DB — reclaimed {_fmt_bytes(reclaimed)}.")


@cache_app.command("effectiveness")
def cache_effectiveness_cmd(
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD"),
) -> None:
    """Per-(chat, preset) OpenAI prompt-cache hit rate from usage_log.

    Surfaces "what's actually saving money": the server-side prompt cache
    only kicks in when the stable prefix (system + static_context) is
    1024+ tokens AND byte-identical across calls. Low hit rate on a
    high-volume row → check the prompt for entropy in its prefix.
    """
    _run(_cache_effectiveness(since))


async def _cache_effectiveness(since: str | None) -> None:
    from rich.table import Table

    settings = get_settings()
    since_dt = parse_ymd(since) if since else None
    async with open_repo(settings.storage.data_path) as repo:
        rows = await repo.cache_effectiveness(since=since_dt)
    if not rows:
        console.print("[yellow]No usage logged yet[/] — run an analyze first.")
        return
    t = Table(title=f"Cache effectiveness{' since ' + since if since else ''}")
    t.add_column("chat_id")
    t.add_column("preset")
    t.add_column("calls", justify="right")
    t.add_column("hit calls", justify="right")
    t.add_column("hit rate", justify="right")
    t.add_column("prompt tok", justify="right")
    t.add_column("cached tok", justify="right")
    t.add_column("cost $", justify="right")
    for r in rows:
        prompt_tok = int(r["prompt_tokens"] or 0)
        cached_tok = int(r["cached_tokens"] or 0)
        rate_pct = (100.0 * cached_tok / prompt_tok) if prompt_tok else 0.0
        t.add_row(
            str(r["chat_id"]),
            str(r["preset"]),
            str(r["total_calls"]),
            str(r["hit_calls"]),
            f"{rate_pct:.1f}%",
            f"{prompt_tok:,}",
            f"{cached_tok:,}",
            f"${float(r['cost_usd']):.4f}",
        )
    console.print(t)
    console.print(
        "[dim]Hit rate counts OpenAI server-side prompt-cache reuse "
        "(`cached_tokens / prompt_tokens`). Local analysis_cache hits aren't "
        "logged (they cost zero) — see `atg cache stats` for that table.[/]"
    )


@cache_app.command("stats")
def cache_stats_cmd() -> None:
    """Show analysis cache size, age range and per-(preset, model) breakdown."""
    _run(_cache_stats())


async def _cache_stats() -> None:
    from rich.table import Table

    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        s = await repo.cache_stats()
    if s["rows"] == 0:
        console.print("[yellow]analysis_cache is empty.[/]")
        return
    console.print(
        f"[bold]analysis_cache[/] — {s['rows']} rows, "
        f"{_fmt_bytes(s['result_bytes'])} of result text, "
        f"saved ~${s['saved_cost_usd']:.4f} in re-runs.\n"
        f"Age range: {s['oldest']}  →  {s['newest']}"
    )
    t = Table(title="By (preset, model)", show_lines=False)
    t.add_column("preset")
    t.add_column("model")
    t.add_column("rows", justify="right")
    t.add_column("size", justify="right")
    t.add_column("saved $", justify="right")
    for r in s["by_group"]:
        t.add_row(
            str(r["preset"]),
            str(r["model"]),
            str(r["rows"]),
            _fmt_bytes(int(r["result_bytes"])),
            f"${float(r['saved_cost_usd']):.4f}",
        )
    console.print(t)


@cache_app.command("ls")
def cache_ls_cmd(
    preset: str | None = typer.Option(None, "--preset"),
    model: str | None = typer.Option(None, "--model"),
    older_than: str | None = typer.Option(None, "--older-than", help="Nd / Nw"),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """List cache entries (newest first). No result body — use `show` for that."""
    _run(_cache_ls(preset, model, older_than, limit))


async def _cache_ls(
    preset: str | None,
    model: str | None,
    older_than: str | None,
    limit: int,
) -> None:
    from rich.table import Table

    settings = get_settings()
    days = _parse_duration_days(older_than) if older_than else None
    async with open_repo(settings.storage.data_path) as repo:
        rows = await repo.cache_list(preset=preset, model=model, older_than_days=days, limit=limit)
    if not rows:
        console.print("[yellow]No matching entries.[/]")
        return
    t = Table(show_lines=False)
    t.add_column("hash")
    t.add_column("preset")
    t.add_column("model")
    t.add_column("ver")
    t.add_column("size", justify="right")
    t.add_column("cost", justify="right")
    t.add_column("created_at")
    for r in rows:
        t.add_row(
            str(r["batch_hash"])[:10],
            str(r["preset"]),
            str(r["model"]),
            str(r["prompt_version"]),
            _fmt_bytes(int(r["result_bytes"] or 0)),
            f"${float(r['cost_usd'] or 0):.4f}",
            str(r["created_at"]),
        )
    console.print(t)


@cache_app.command("show")
def cache_show_cmd(
    batch_hash: str = typer.Argument(..., help="Full hash or unique prefix."),
) -> None:
    """Print a stored analysis result."""
    _run(_cache_show(batch_hash))


async def _cache_show(batch_hash: str) -> None:
    settings = get_settings()
    async with open_repo(settings.storage.data_path) as repo:
        row = await repo.cache_get(batch_hash)
        if row is None:
            # Prefix match fallback — unique prefix only.
            matches = [
                r for r in await repo.cache_list(limit=10_000) if str(r["batch_hash"]).startswith(batch_hash)
            ]
            if len(matches) == 0:
                console.print(f"[red]No entry matching[/] {batch_hash}.")
                raise typer.Exit(1)
            if len(matches) > 1:
                console.print(f"[red]Ambiguous prefix[/] — {len(matches)} matches. Use a longer prefix.")
                raise typer.Exit(2)
            row = await repo.cache_get(matches[0]["batch_hash"])
            assert row is not None
    console.print(
        f"[bold]{row['batch_hash']}[/]  preset={row['preset']}  model={row['model']}  "
        f"ver={row['prompt_version']}  cost=${float(row['cost_usd'] or 0):.4f}  "
        f"created={row['created_at']}\n"
    )
    console.print(row["result"])


@cache_app.command("export")
def cache_export_cmd(
    output: Path = typer.Option(
        ..., "--output", "-o", help="File path. Extension picks format if --format omitted."
    ),
    fmt: str | None = typer.Option(None, "--format", help="jsonl | md"),
    preset: str | None = typer.Option(None, "--preset"),
    model: str | None = typer.Option(None, "--model"),
    older_than: str | None = typer.Option(None, "--older-than", help="Export entries OLDER than this age."),
) -> None:
    """Export cached analyses to jsonl or md before (optionally) purging."""
    _run(_cache_export(output, fmt, preset, model, older_than))


async def _cache_export(
    output: Path,
    fmt: str | None,
    preset: str | None,
    model: str | None,
    older_than: str | None,
) -> None:
    import json

    if fmt is None:
        suffix = output.suffix.lower().lstrip(".")
        fmt = suffix if suffix in {"jsonl", "md"} else "jsonl"
    if fmt not in {"jsonl", "md"}:
        console.print(f"[red]Unknown format[/] {fmt}. Use jsonl or md.")
        raise typer.Exit(2)

    settings = get_settings()
    days = _parse_duration_days(older_than) if older_than else None
    async with open_repo(settings.storage.data_path) as repo:
        rows = await repo.cache_iter_full(preset=preset, model=model, older_than_days=days)

    if not rows:
        console.print("[yellow]No matching entries — nothing written.[/]")
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "jsonl":
        with output.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    else:  # md
        with output.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(
                    f"## {r['batch_hash']}\n\n"
                    f"- preset: `{r['preset']}`\n"
                    f"- model: `{r['model']}`\n"
                    f"- prompt_version: `{r['prompt_version']}`\n"
                    f"- cost_usd: {r['cost_usd']}\n"
                    f"- created_at: {r['created_at']}\n\n"
                    f"{r['result']}\n\n---\n\n"
                )
    console.print(f"[green]Wrote[/] {len(rows)} entries → {output} ({fmt}).")


def _fmt_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"


def _parse_duration_days(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("d"):
        return int(s[:-1])
    if s.endswith("w"):
        return int(s[:-1]) * 7
    return int(s)


@app.command(rich_help_panel=PANEL_MAINT)
def cleanup(
    retention: str = typer.Option("90d", "--retention"),
    chat: int | None = typer.Option(None, "--chat"),
    keep_transcripts: bool = typer.Option(True, "--keep-transcripts/--no-keep-transcripts"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Null-out old message texts; keep transcripts/analysis cache."""
    _run(_cleanup(retention, chat, keep_transcripts, yes))


async def _cleanup(retention: str, chat: int | None, keep_transcripts: bool, yes: bool) -> None:
    settings = get_settings()
    days = _parse_duration_days(retention)
    async with open_repo(settings.storage.data_path) as repo:
        preview = await repo.count_redactable_messages(
            retention_days=days,
            chat_id=chat,
            keep_transcripts=keep_transcripts,
        )
        if preview["to_redact"] == 0:
            if preview["messages"] == 0:
                console.print(f"[yellow]Nothing to redact[/] older than {days} days.")
            else:
                console.print(
                    f"[yellow]Already clean[/] — {preview['messages']} matching rows "
                    f"older than {days} days, but nothing left to null "
                    f"(text already NULL{'; transcripts kept' if keep_transcripts else ''})."
                )
            return

        scope = f"chat={chat}" if chat is not None else "all chats"
        transcript_line = "0 [dim](kept)[/]" if keep_transcripts else str(preview["with_transcript"])
        console.print(
            f"[bold]Cleanup preview[/] ({scope}, older than {days} days):\n"
            f"  messages matched:        {preview['messages']}\n"
            f"  [red]rows to redact[/]:          {preview['to_redact']}\n"
            f"  [red]text to null-out[/]:        {preview['with_text']}\n"
            f"  transcripts to null-out: {transcript_line}\n"
            f"[dim]Row metadata (ids, dates, authors) is preserved.[/]"
        )
        if not yes and not typer.confirm("Proceed with redaction?", default=False):
            console.print("[yellow]Aborted.[/]")
            return

        redacted = await repo.redact_old_messages(
            retention_days=days,
            chat_id=chat,
            keep_transcripts=keep_transcripts,
        )
        console.print(
            f"[green]Redacted[/] {redacted} messages older than {days} days"
            f"{' (transcripts kept)' if keep_transcripts else ''}."
        )


@app.command(rich_help_panel=PANEL_MAIN)
def ask(
    question: str = typer.Argument(..., help="Free-form question, in any language."),
    chat: str | None = typer.Option(
        None,
        "--chat",
        help="Restrict search to one chat (@user / link / fuzzy title / numeric id).",
    ),
    thread: int | None = typer.Option(
        None,
        "--thread",
        help="Forum-topic id (only meaningful with --chat).",
    ),
    folder: str | None = typer.Option(
        None,
        "--folder",
        help="Restrict search to chats in this Telegram folder (case-insensitive substring).",
    ),
    since: str | None = typer.Option(None, "--since", help="YYYY-MM-DD"),
    until: str | None = typer.Option(None, "--until", help="YYYY-MM-DD"),
    last_days: int | None = typer.Option(None, "--last-days"),
    limit: int = typer.Option(
        200,
        "--limit",
        help="Max messages to retrieve. Higher = better recall, more cost.",
    ),
    model: str | None = typer.Option(None, "--model", help="Override the answering model."),
    output: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Save the answer to a file (markdown). Without --output the answer prints to terminal.",
    ),
    console_out: bool = typer.Option(
        False,
        "--console",
        "-c",
        help="Force terminal rendering even when --output is also set.",
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help=(
            "Pull new messages from Telegram (incremental from each chat's local "
            "max msg_id) before retrieval. Requires --chat or --folder."
        ),
    ),
    show_retrieved: bool = typer.Option(
        False,
        "--show-retrieved",
        help="Print the retrieved messages with their scores before the LLM call (debug).",
    ),
    rerank: bool | None = typer.Option(
        None,
        "--rerank/--no-rerank",
        help=(
            "Two-stage retrieval: keyword pool → cheap-model rerank → flagship answer. "
            "Default from [ask].rerank_enabled in config (true). Saves ~5-10× per question "
            "on media-heavy chats by feeding the flagship a smaller, better-ranked set."
        ),
    ),
    interactive: bool = typer.Option(
        False,
        "--interactive",
        "-i",
        help=(
            "After the first answer, drop into a follow-up prompt. Each follow-up "
            "re-retrieves under the same scope and includes prior turns as context "
            "(so 'tell me more' just works). Blank line or Ctrl-D exits."
        ),
    ),
    semantic: bool = typer.Option(
        False,
        "--semantic",
        help=(
            "Use OpenAI-embeddings retrieval (cosine over a precomputed index) "
            "instead of keyword LIKE. Run `--build-index` first per chat/folder. "
            "Catches paraphrase ('the DB' → migration discussion) that keyword misses."
        ),
    ),
    build_index: bool = typer.Option(
        False,
        "--build-index",
        help=(
            "Embed every not-yet-indexed message in the scoped chat(s) and exit. "
            "Idempotent — re-runs only fill gaps. Required once per chat before "
            "`--semantic`. Cheap: ~$0.02 per 1M tokens at text-embedding-3-small."
        ),
    ),
    max_cost: float | None = typer.Option(
        None,
        "--max-cost",
        help=(
            "Abort if the estimated USD cost exceeds N. The estimate counts the "
            "exact prompt tokens (no _AVG_TOKENS_PER_MSG rounding) so it tracks "
            "media-heavy chats. Pass with --yes to abort silently."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the over-budget confirmation prompt (combined with --max-cost).",
    ),
) -> None:
    """Answer a question about your synced Telegram archive.

    Retrieves the top relevant messages from the local DB by keyword match
    (no embeddings; no Telegram or OpenAI calls during retrieval), then
    asks one LLM call to answer with citations. Scope defaults to every
    synced chat — narrow with `--chat`, `--folder`, `--thread`, or a
    period flag for sharper answers (and lower cost).

    Examples:
      atg ask "what did Bob say about migration?"
      atg ask "any open questions on the API?" --folder Work --last-days 7
      atg ask "когда дедлайн по проекту?" --chat @somegroup --refresh
    """
    from analyzetg.ask.commands import cmd_ask

    _run(
        cmd_ask(
            question=question,
            chat=chat,
            thread=thread,
            folder=folder,
            since=since,
            until=until,
            last_days=last_days,
            limit=limit,
            model=model,
            output=output,
            console_out=console_out,
            refresh=refresh,
            show_retrieved=show_retrieved,
            rerank=rerank,
            interactive=interactive,
            semantic=semantic,
            build_index=build_index,
            max_cost=max_cost,
            yes=yes,
        )
    )


reports_app = typer.Typer(help="Manage saved reports/", no_args_is_help=True)
app.add_typer(reports_app, name="reports", rich_help_panel=PANEL_MAINT)


@reports_app.command("prune")
def reports_prune(
    older_than: str = typer.Option("30d", "--older-than", help="Nd / Nw"),
    root: Path = typer.Option(Path("reports"), "--root", help="Reports root directory (default: ./reports)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List what would be pruned, take no action."),
    purge: bool = typer.Option(
        False,
        "--purge",
        help="Hard-delete instead of moving to reports/.trash/<ts>/. Irreversible.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Move (or delete) report files older than --older-than to reports/.trash/.

    Default behavior: trash them by moving to `reports/.trash/<ts>/`. The
    `.trash/` subtree is itself ignored when scanning. Run with `--purge`
    to hard-delete (after confirmation, unless `--yes`).
    """
    _run(_reports_prune(older_than, root, dry_run, purge, yes))


async def _reports_prune(
    older_than: str,
    root: Path,
    dry_run: bool,
    purge: bool,
    yes: bool,
) -> None:
    import shutil
    import time

    days = _parse_duration_days(older_than)
    if days <= 0:
        console.print("[yellow]Skipped[/] — --older-than must be > 0 days.")
        return
    if not root.exists():
        console.print(f"[yellow]No reports root[/] at {root} — nothing to prune.")
        return
    cutoff = time.time() - days * 86400
    trash_root = root / ".trash"
    candidates: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        # Don't prune the trash, the slash, or hidden dotfiles inside the
        # tree (e.g. .gitkeep — the user may want those preserved).
        if trash_root in p.parents or p.name.startswith("."):
            continue
        try:
            if p.stat().st_mtime < cutoff:
                candidates.append(p)
        except OSError:
            continue
    if not candidates:
        console.print(f"[dim]Nothing older than {days} days under {root}.[/]")
        return
    total_bytes = sum(p.stat().st_size for p in candidates if p.exists())
    verb = (
        "Would delete"
        if dry_run and purge
        else ("Would trash" if dry_run else ("Delete" if purge else "Trash"))
    )
    console.print(
        f"[bold]{verb}[/] {len(candidates)} file(s) ({_fmt_bytes(total_bytes)}) "
        f"older than {days} days under {root}."
    )
    for p in candidates[:20]:
        console.print(f"  {p.relative_to(root)}")
    if len(candidates) > 20:
        console.print(f"  [dim]… and {len(candidates) - 20} more[/]")
    if dry_run:
        return
    if not yes and not typer.confirm("Proceed?", default=False):
        console.print("[yellow]Aborted.[/]")
        return
    if purge:
        for p in candidates:
            try:
                p.unlink()
            except OSError as e:
                console.print(f"[red]Failed to delete[/] {p}: {e}")
        console.print(f"[green]Deleted[/] {len(candidates)} file(s).")
        return
    # Trash mode: move to reports/.trash/<ts>/, preserving relative subtree.
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    bin_dir = trash_root / stamp
    bin_dir.mkdir(parents=True, exist_ok=True)
    for p in candidates:
        rel = p.relative_to(root)
        target = bin_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(p), str(target))
        except OSError as e:
            console.print(f"[red]Failed to move[/] {p}: {e}")
    console.print(f"[green]Trashed[/] {len(candidates)} file(s) → {bin_dir}")


@app.command(
    rich_help_panel=PANEL_MAINT,
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def watch(
    ctx: typer.Context,
    interval: str = typer.Option(
        "1h",
        "--interval",
        help="How often to fire the inner command. Accepts Nm / Nh / Nd / Nw.",
    ),
    max_runs: int | None = typer.Option(
        None,
        "--max-runs",
        help="Stop after N successful runs (handy for testing). None = run forever.",
    ),
) -> None:
    """Run an inner `atg` command on a fixed cadence.

    `atg watch --interval 1h analyze --folder Work --post-saved` walks the
    wall clock: runs the inner command, sleeps for the interval, repeats.
    Foreground only — run it under `tmux` / `nohup` if you need
    persistence. Ctrl-C exits cleanly between iterations.

    The inner command runs in a fresh subprocess each time (so an internal
    crash doesn't poison subsequent runs); exit codes are surfaced but
    don't abort the loop unless `--max-runs` is hit.
    """
    inner = ctx.args
    if not inner:
        console.print("[red]Pass an inner command, e.g.[/] atg watch --interval 1h analyze --folder Work")
        raise typer.Exit(2)
    _run(_watch_loop(interval, max_runs, inner))


async def _watch_loop(interval: str, max_runs: int | None, inner: list[str]) -> None:
    import asyncio as _asyncio
    import shlex
    import subprocess
    import sys as _sys

    seconds = _parse_duration_seconds(interval)
    if seconds <= 0:
        console.print("[red]--interval must be > 0.[/]")
        raise typer.Exit(2)

    runs = 0
    cmd = ["atg", *inner]
    pretty = " ".join(shlex.quote(c) for c in cmd)
    console.print(f"[bold cyan]Watching[/] [dim]every {interval}: {pretty}[/]")
    # Single Ctrl-C handler covers both phases (subprocess.run / sleep).
    # subprocess.run inherits stdin so child sees the SIGINT first; if
    # the child handles it cleanly, control returns here and we just
    # continue. If the user mashes Ctrl-C again during sleep, it
    # propagates as KeyboardInterrupt and we exit.
    try:
        while True:
            runs += 1
            console.print(f"\n[bold]── Run {runs}[/] [dim]{datetime.now().isoformat(timespec='seconds')}[/]")
            try:
                # subprocess.run blocks the event loop; that's fine — we're
                # not racing anything here, and the inner command may itself
                # spin up its own asyncio loop.
                proc = subprocess.run(cmd, check=False)
                if proc.returncode != 0:
                    console.print(f"[yellow]Inner exited with code {proc.returncode}[/]")
            except FileNotFoundError:
                console.print(f"[red]`{cmd[0]}` not on PATH.[/]")
                raise typer.Exit(2) from None
            if max_runs is not None and runs >= max_runs:
                console.print(f"[dim]Hit --max-runs {max_runs}; exiting.[/]")
                return
            console.print(f"[dim]Sleeping {interval}...[/]")
            await _asyncio.sleep(seconds)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted; exiting.[/]")
    finally:
        _sys.stdout.flush()


def _parse_duration_seconds(s: str) -> int:
    """Parse `45s`/`5m`/`2h`/`3d`/`1w` into seconds. Raises on garbage."""
    s = s.strip().lower()
    if not s:
        return 0
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if s[-1] in units:
        try:
            return int(s[:-1]) * units[s[-1]]
        except ValueError as e:
            raise typer.BadParameter(f"Invalid duration: {s!r}") from e
    # Bare integer = seconds.
    try:
        return int(s)
    except ValueError as e:
        raise typer.BadParameter(f"Invalid duration: {s!r}") from e


@app.command(rich_help_panel=PANEL_MAINT)
def doctor() -> None:
    """Preflight check: Telegram session, OpenAI key, ffmpeg, DB integrity, presets, disk."""
    from analyzetg.tg.commands import cmd_doctor

    _run(cmd_doctor())


@app.command(rich_help_panel=PANEL_MAINT)
def backup(
    output: Path | None = typer.Argument(
        None,
        help="Destination file (default: storage/backups/data-YYYY-MM-DD_HHMMSS.sqlite).",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Replace the destination file if it already exists.",
    ),
) -> None:
    """Snapshot storage/data.sqlite to a single compact file (uses VACUUM INTO).

    Safe to run while atg is in the middle of a sync — SQLite makes the
    copy consistent without blocking the writer for more than a moment.
    Restore with `atg restore <file>`.
    """
    _run(_backup(output, overwrite))


async def _backup(output: Path | None, overwrite: bool) -> None:
    settings = get_settings()
    src = settings.storage.data_path
    if not src.exists():
        console.print(f"[red]No DB at {src} — nothing to back up.[/]")
        raise typer.Exit(1)
    if output is None:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        output = Path("storage/backups") / f"data-{stamp}.sqlite"
    output = output.resolve()
    if output.exists():
        if not overwrite:
            console.print(f"[red]{output} already exists.[/] Pass --overwrite or pick a different path.")
            raise typer.Exit(2)
        output.unlink()
    async with open_repo(src) as repo:
        size = await repo.backup_to(output)
    console.print(f"[green]Backed up[/] {src} → {output} [dim]({_fmt_bytes(size)})[/]")


@app.command(rich_help_panel=PANEL_MAINT)
def restore(
    backup_file: Path = typer.Argument(..., help="Path to a previously-created backup file."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the destructive-action prompt."),
) -> None:
    """Replace storage/data.sqlite with a backup. The current DB is moved aside.

    The current DB is renamed to `data-replaced-YYYY-MM-DD_HHMMSS.sqlite`
    next to the original — undo by swapping the names back.
    """
    _run(_restore(backup_file, yes))


async def _restore(backup_file: Path, yes: bool) -> None:
    import shutil

    settings = get_settings()
    dst = settings.storage.data_path
    if not backup_file.exists():
        console.print(f"[red]Backup not found:[/] {backup_file}")
        raise typer.Exit(2)
    if not yes and not typer.confirm(
        f"Replace {dst} with {backup_file}? Current DB will be moved aside.",
        default=False,
    ):
        console.print("[yellow]Aborted.[/]")
        raise typer.Exit(0)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        moved = dst.with_name(f"{dst.stem}-replaced-{stamp}{dst.suffix}")
        dst.rename(moved)
        console.print(f"[dim]Moved current DB to {moved}[/]")
    # Also clear -wal / -shm sidecars so the restored DB doesn't pick up
    # transactions from the replaced DB on next open.
    for sidecar in (dst.with_suffix(dst.suffix + "-wal"), dst.with_suffix(dst.suffix + "-shm")):
        if sidecar.exists():
            sidecar.unlink()
    shutil.copy2(backup_file, dst)
    console.print(f"[green]Restored[/] {backup_file} → {dst}")


@app.command(hidden=True)
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


@app.command(rich_help_panel=PANEL_MAIN)
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
    console_out: bool = typer.Option(
        False,
        "--console",
        "-c",
        help="Print the dump to the terminal (pretty markdown) instead of saving a file.",
    ),
    save: bool = typer.Option(
        False,
        "--save",
        "-s",
        help="Save to the default reports/ path (skips the interactive output picker).",
    ),
    mark_read: bool | None = typer.Option(
        None,
        "--mark-read/--no-mark-read",
        help="Tri-state: --mark-read advances Telegram's marker; --no-mark-read keeps unread and skips the prompt; no flag → ask interactively.",
    ),
    all_flat: bool = typer.Option(
        False,
        "--all-flat",
        help="Forum only: dump whole forum as one file. Needs an explicit period flag.",
    ),
    all_per_topic: bool = typer.Option(
        False,
        "--all-per-topic",
        help="Forum only: one file per topic. Reports land in reports/{chat}/.",
    ),
    enrich: str | None = typer.Option(
        None,
        "--enrich",
        help=(
            "Comma-separated media enrichments to enable before writing the dump: "
            "voice, videonote, video, image, doc, link. Mirrors analyze's flag."
        ),
    ),
    enrich_all: bool = typer.Option(
        False,
        "--enrich-all",
        help="Enable every enrichment before writing the dump.",
    ),
    no_enrich: bool = typer.Option(
        False,
        "--no-enrich",
        help="Disable all enrichments for this dump (raw message text only).",
    ),
    save_media: bool = typer.Option(
        False,
        "--save-media",
        help=(
            "Save raw media files (photo / voice / video / doc) alongside "
            "the text dump in reports/<chat>/[topic]/media/. Same effect "
            "as atg download-media but bundled with the dump run."
        ),
    ),
    save_media_types: str | None = typer.Option(
        None,
        "--save-media-types",
        help=(
            "Comma-separated subset to save (voice, videonote, video, photo, doc). "
            "Default: all. Only meaningful with --save-media."
        ),
    ),
    folder: str | None = typer.Option(
        None,
        "--folder",
        help=(
            "Batch-dump every chat in this Telegram folder (case-insensitive "
            "substring match on folder title). Only meaningful without <ref>. "
            "Currently unread-only — pass period flags only with a single ref."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip interactive confirmations (per-topic / batch prompts).",
    ),
) -> None:
    """Dump chat history to a file. Default window = messages since your Telegram read marker.

    Precedence of start-point flags: --full-history > --from-msg >
    --since/--until/--last-days > (default: unread). `--enrich=...`
    runs the same media pipeline as analyze (voice→transcript,
    photo→description, doc→text, link→summary) and embeds results into
    the saved file. Legacy `--with-transcribe` still works for
    audio-only; it's suppressed when `--enrich` is set. `--save-media`
    additionally saves the raw media bytes next to the text dump.

    Without `<ref>` and with `--folder NAME`: batch-dumps every chat in
    that Telegram folder that has unread messages.
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
            console_out=console_out,
            save_default=save,
            mark_read=mark_read,
            all_flat=all_flat,
            all_per_topic=all_per_topic,
            enrich=enrich,
            enrich_all=enrich_all,
            no_enrich=no_enrich,
            save_media=save_media,
            save_media_types=save_media_types,
            folder=folder,
            yes=yes,
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
    # Delegate to the canonical implementation to keep UTC-awareness
    # consistent with how `messages.date` is stored (ISO-UTC strings).
    from analyzetg.core.paths import compute_window

    return compute_window(since, until, last_days)


_NEG_NUM_RE = __import__("re").compile(r"^-\d+$")


def _preprocess_argv(argv: list[str] | None = None) -> list[str]:
    """Let users type bare negative numeric chat ids as positional args.

    `atg analyze -1003865481227` normally fails because Click sees
    `-1003865481227` as a short-option token. Older versions of this
    preprocessor injected `--` in place — which fixed the bare case but
    broke `atg analyze -1003865481227 --all-flat`, because `--` closes
    option parsing and `--all-flat` then becomes an unexpected second
    positional.

    The fix: pull negative-number **positionals** out of the arg list
    and re-append them at the end, prefixed by `--`. Flags in between
    stay in place and get parsed normally. A negative number is
    considered a positional when the token before it is NOT a flag
    (so `--chat -1001234` leaves `-1001234` in place as the value of
    `--chat`, but `analyze -1003… --all-flat` pulls the id to the end).

    If the user already used `--` explicitly, we don't touch argv —
    that's a load-bearing user choice.

    Pure function for testability; `main()` passes `sys.argv` in.
    """
    if argv is None:
        import sys as _sys

        argv = list(_sys.argv)
    if not argv:
        return argv
    rest = argv[1:]
    if "--" in rest:
        return list(argv)  # user supplied explicit separator, respect it

    negs: list[str] = []
    kept: list[str] = []
    for i, tok in enumerate(rest):
        if _NEG_NUM_RE.match(tok):
            prev = rest[i - 1] if i > 0 else ""
            # If the previous token is an option (starts with "-"), this
            # negative number is likely its value (e.g. `--chat -1001234`).
            # Leave it in place. Otherwise it's a positional — move it.
            if prev.startswith("-"):
                kept.append(tok)
            else:
                negs.append(tok)
        else:
            kept.append(tok)
    if not negs:
        return list(argv)
    return [argv[0], *kept, "--", *negs]


def main() -> None:
    """Entry point — preprocesses argv, then hands off to Typer."""
    import sys as _sys

    _sys.argv = _preprocess_argv(list(_sys.argv))
    app()


if __name__ == "__main__":
    main()
