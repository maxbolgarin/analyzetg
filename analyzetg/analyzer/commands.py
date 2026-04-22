"""CLI commands for analyze + stats.

`cmd_analyze` resolves a chat reference, pulls messages fresh from Telegram
(no subscription row, no sync_state writes), and hands off to the existing
analysis pipeline. Default start-point is the dialog's unread marker.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from analyzetg.analyzer.pipeline import AnalysisOptions, AnalysisResult, run_analysis
from analyzetg.config import get_settings
from analyzetg.db.repo import Repo, open_repo
from analyzetg.tg.client import tg_client
from analyzetg.tg.dialogs import (
    UnreadDialog,
    get_unread_state,
    list_unread_dialogs,
    mark_as_read,
)
from analyzetg.tg.resolver import resolve
from analyzetg.tg.sync import backfill
from analyzetg.util.logging import get_logger

console = Console()
log = get_logger(__name__)


def _parse_ymd(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d")


def _parse_from_msg(value: str | None) -> int | None:
    if not value:
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    from analyzetg.tg.links import parse

    return parse(value).msg_id


async def cmd_analyze(
    *,
    ref: str | None,
    thread: int | None,
    from_msg: str | None,
    full_history: bool,
    since: str | None,
    until: str | None,
    last_days: int | None,
    preset: str,
    prompt_file: Path | None,
    model: str | None,
    filter_model: str | None,
    output: Path | None,
    console_out: bool = False,
    mark_read: bool = False,
    no_cache: bool = False,
    include_transcripts: bool = True,
    min_msg_chars: int | None = None,
) -> None:
    settings = get_settings()
    since_dt, until_dt = _compute_window(since, until, last_days)
    from_msg_id = _parse_from_msg(from_msg)

    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        if ref is None:
            await _run_no_ref(
                client=client,
                repo=repo,
                preset=preset,
                prompt_file=prompt_file,
                model=model,
                filter_model=filter_model,
                output=output,
                console_out=console_out,
                mark_read=mark_read,
                no_cache=no_cache,
                include_transcripts=include_transcripts,
                min_msg_chars=min_msg_chars,
            )
            return

        console.print(f"[dim]→ Resolving[/] {ref}")
        resolved = await resolve(client, repo, ref)
        chat_id = resolved.chat_id
        thread_id = thread if thread is not None else (resolved.thread_id or 0)
        title = resolved.title
        console.print(
            f"[dim]→ Resolved[/] {title or chat_id} "
            f"[dim](id={chat_id}, kind={resolved.kind}"
            f"{', thread=' + str(thread_id) if thread_id else ''})[/]"
        )
        # A link like /group/100/5000 carries a msg_id; treat it as start
        # unless an explicit time window / full-history flag was passed.
        if (
            from_msg_id is None
            and not full_history
            and resolved.msg_id is not None
            and since_dt is None
            and until_dt is None
        ):
            from_msg_id = resolved.msg_id

        start_msg_id = await _determine_start(
            client=client,
            chat_id=chat_id,
            thread_id=thread_id,
            full_history=full_history,
            from_msg_id=from_msg_id,
            time_window=(since_dt, until_dt),
        )

        console.print("[dim]→ Fetching new messages from Telegram...[/]")
        await _pull_history(
            client=client,
            repo=repo,
            chat_id=chat_id,
            thread_id=thread_id,
            start_msg_id=start_msg_id,
            since_dt=since_dt,
        )
        console.print("[dim]→ Running analysis...[/]")

        opts = AnalysisOptions(
            preset=preset,
            prompt_file=prompt_file,
            model_override=model,
            filter_model_override=filter_model,
            use_cache=not no_cache,
            include_transcripts=include_transcripts,
            min_msg_chars=min_msg_chars,
            since=since_dt,
            until=until_dt,
            # start_msg_id is inclusive of the next message (msg_id > start).
            # When full_history / time-window drives selection, keep it None.
            min_msg_id=start_msg_id if start_msg_id and start_msg_id > 0 else None,
        )
        result = await run_analysis(repo=repo, chat_id=chat_id, thread_id=thread_id, title=title, opts=opts)

        if mark_read and result.msg_count > 0:
            latest = await repo.get_max_msg_id(chat_id, thread_id or None)
            if latest:
                ok = await mark_as_read(client, chat_id, latest, thread_id=thread_id or None)
                if ok:
                    console.print(f"[dim]→ Marked read up to msg_id={latest}[/]")

    _print_and_write(result, output=output, title=title, console_out=console_out)


async def _determine_start(
    *,
    client,
    chat_id: int,
    thread_id: int,
    full_history: bool,
    from_msg_id: int | None,
    time_window: tuple[datetime | None, datetime | None],
) -> int | None:
    """Return a msg_id lower bound (exclusive) or None for time-window / full mode."""
    if full_history:
        return None
    if from_msg_id is not None:
        # from-msg is inclusive: the analyzer filters with msg_id > (n-1) = n.
        return max(from_msg_id - 1, 0)
    if time_window[0] is not None or time_window[1] is not None:
        return None
    # Default: unread marker.
    if thread_id:
        console.print(
            "[red]Per-topic unread is not exposed by Telegram's high-level API.[/]\n"
            "Pass [cyan]--last-days N[/], [cyan]--from-msg <id>[/], or [cyan]--full-history[/]."
        )
        raise typer.Exit(2)
    console.print("[dim]→ Reading unread marker...[/]")
    unread_count, read_marker = await get_unread_state(client, chat_id)
    if unread_count == 0:
        console.print(
            f"[yellow]No unread messages in chat {chat_id}.[/] "
            "Pass --last-days / --from-msg / --full-history to analyze anyway."
        )
        raise typer.Exit(0)
    console.print(f"[dim]→ {unread_count} unread message(s) after msg_id={read_marker}[/]")
    return read_marker


async def _pull_history(
    *,
    client,
    repo: Repo,
    chat_id: int,
    thread_id: int,
    start_msg_id: int | None,
    since_dt: datetime | None,
) -> None:
    """Fetch messages from Telegram into the local DB for the target window.

    Consults the local DB first — if we already have messages above
    `start_msg_id`, we only fetch what's newer than our local tail. This
    avoids re-downloading the same range on repeat runs.
    """
    thread_param = thread_id if thread_id else None
    if start_msg_id is not None or (since_dt is None):
        floor = start_msg_id if start_msg_id is not None else 0
        local_max = await repo.get_max_msg_id(chat_id, thread_param, min_msg_id=floor)
        effective = max(floor, local_max or 0)
        if local_max and local_max > floor:
            console.print(f"[dim]→ Have up to msg_id={local_max} locally, fetching only newer[/]")
        await backfill(
            client,
            repo,
            chat_id=chat_id,
            thread_id=thread_param,
            from_msg_id=effective + 1,
            direction="forward",
        )
    else:
        # Pure time-window pull without a msg_id anchor.
        await backfill(
            client,
            repo,
            chat_id=chat_id,
            thread_id=thread_param,
            since_date=since_dt,
        )


async def _run_no_ref(
    *,
    client,
    repo: Repo,
    preset: str,
    prompt_file: Path | None,
    model: str | None,
    filter_model: str | None,
    output: Path | None,
    console_out: bool,
    mark_read: bool,
    no_cache: bool,
    include_transcripts: bool,
    min_msg_chars: int | None,
) -> None:
    """No <ref>: list dialogs with unread messages, confirm, analyze each."""
    unread = await list_unread_dialogs(client)
    if not unread:
        console.print("[yellow]No dialogs with unread messages.[/]")
        return
    _print_unread_table(unread)
    total = sum(d.unread_count for d in unread)
    if not typer.confirm(
        f"Analyze {len(unread)} chat(s) with {total} total unread message(s)?",
        default=False,
    ):
        console.print("[dim]Aborted.[/]")
        return

    if console_out:
        out_dir = None
    else:
        out_dir = _resolve_output_dir(output, len(unread))
        if out_dir is None:
            # Default — save every chat's report, don't spam the terminal.
            out_dir = Path("reports")
            out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    for u in unread:
        console.print(f"\n[bold cyan]>>[/] {u.title or u.chat_id} ({u.unread_count} unread)")
        try:
            local_max = await repo.get_max_msg_id(u.chat_id, min_msg_id=u.read_inbox_max_id)
            floor = max(u.read_inbox_max_id, local_max or 0)
            await backfill(
                client,
                repo,
                chat_id=u.chat_id,
                from_msg_id=floor + 1,
                direction="forward",
            )
            opts = AnalysisOptions(
                preset=preset,
                prompt_file=prompt_file,
                model_override=model,
                filter_model_override=filter_model,
                use_cache=not no_cache,
                include_transcripts=include_transcripts,
                min_msg_chars=min_msg_chars,
                min_msg_id=u.read_inbox_max_id,
            )
            result = await run_analysis(repo=repo, chat_id=u.chat_id, thread_id=0, title=u.title, opts=opts)
            per_file = (
                out_dir / f"{_slugify(u.title or str(u.chat_id))}-{preset}-{stamp}.md" if out_dir else None
            )
            _print_and_write(result, output=per_file, title=u.title, console_out=console_out)
            if mark_read and result.msg_count > 0:
                latest = await repo.get_max_msg_id(u.chat_id)
                if latest:
                    ok = await mark_as_read(client, u.chat_id, latest)
                    if ok:
                        console.print(f"[dim]→ Marked read up to msg_id={latest}[/]")
        except Exception as e:
            log.error("analyze.no_ref.chat_error", chat_id=u.chat_id, err=str(e)[:200])
            console.print(f"[red]Failed:[/] {e}")


def _print_unread_table(dialogs: list[UnreadDialog]) -> None:
    t = Table(title="Dialogs with unread messages")
    for col in ("id", "kind", "title", "username", "unread"):
        t.add_column(col)
    for d in dialogs:
        t.add_row(
            str(d.chat_id),
            d.kind,
            d.title or "",
            f"@{d.username}" if d.username else "",
            str(d.unread_count),
        )
    console.print(t)


def _resolve_output_dir(output: Path | None, n_chats: int) -> Path | None:
    """In no-ref mode we write one file per chat; output must be a directory."""
    if output is None:
        return None
    if output.exists() and output.is_dir():
        return output
    if output.suffix:
        console.print(
            f"[red]--output {output} is a single file, but {n_chats} chats need per-chat files.[/]\n"
            "Pass a directory path or drop --output."
        )
        raise typer.Exit(2)
    output.mkdir(parents=True, exist_ok=True)
    return output


_SLUG_RE = re.compile(r"[^A-Za-z0-9_\-]+")


def _slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text).strip("-").lower()
    return slug[:40] or "chat"


def _print_and_write(
    result: AnalysisResult,
    *,
    output: Path | None,
    title: str | None,
    console_out: bool = False,
) -> None:
    console.print(
        f"[bold cyan]Run[/] preset={result.preset} msgs={result.msg_count} "
        f"chunks={result.chunk_count} cache_hits={result.cache_hits}/"
        f"{result.cache_hits + result.cache_misses} cost=${result.total_cost_usd:.4f}"
    )
    if console_out:
        from rich.markdown import Markdown
        from rich.rule import Rule

        console.print(Rule(title or "result", style="cyan"))
        console.print(Markdown(result.final_result))
        console.print(Rule(style="cyan"))
        # If user *also* passed -o, save the file alongside the console output.
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(result.final_result, encoding="utf-8")
            console.print(f"[green]Also saved:[/] {output}")
        return

    if output is None:
        output = _default_output_path(title, result.preset)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.final_result, encoding="utf-8")
    console.print(f"[green]Written:[/] {output}")


def _default_output_path(title: str | None, preset: str) -> Path:
    slug = _slugify(title or "chat")
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    return Path("reports") / f"{slug}-{preset}-{stamp}.md"


def _compute_window(
    since: str | None, until: str | None, last_days: int | None
) -> tuple[datetime | None, datetime | None]:
    if last_days:
        until_dt = datetime.now()
        since_dt = until_dt - timedelta(days=last_days)
        return since_dt, until_dt
    return _parse_ymd(since), _parse_ymd(until)


async def cmd_stats(since: str | None, by: str) -> None:
    settings = get_settings()
    since_dt = _parse_ymd(since)
    async with open_repo(settings.storage.data_path) as repo:
        rows = await repo.stats_by(group_by=by, since=since_dt)
        hit_rate = await repo.cache_hit_rate(since=since_dt)
        total_cost = sum(float(r["cost_usd"] or 0) for r in rows)

        t = Table(title=f"Usage (by {by}){' since ' + since if since else ''}")
        cols = ("bucket", "calls", "prompt", "cached", "completion", "audio_s", "cost_usd")
        for c in cols:
            t.add_column(c)
        for r in rows:
            t.add_row(
                str(r["bucket"]) if r["bucket"] is not None else "-",
                str(r["calls"]),
                str(r["prompt_tokens"] or 0),
                str(r["cached_tokens"] or 0),
                str(r["completion_tokens"] or 0),
                str(r["audio_seconds"] or 0),
                f"${float(r['cost_usd'] or 0):.4f}",
            )
        console.print(t)
        console.print(f"[bold]Total cost:[/] ${total_cost:.4f}")
        console.print(f"[bold]Cache hit rate:[/] {hit_rate:.1%}")
