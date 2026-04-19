"""CLI commands for analyze + stats."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from rich.console import Console
from rich.table import Table

from analyzetg.analyzer.pipeline import AnalysisOptions, run_analysis
from analyzetg.config import get_settings
from analyzetg.db.repo import open_repo
from analyzetg.util.logging import get_logger

console = Console()
log = get_logger(__name__)


def _parse_ymd(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d")


async def cmd_analyze(
    *,
    chat: int,
    thread: int | None,
    since: str | None,
    until: str | None,
    last_days: int | None,
    preset: str,
    prompt_file: Path | None,
    model: str | None,
    filter_model: str | None,
    output: Path | None,
    no_cache: bool,
    include_transcripts: bool,
    min_msg_chars: int | None,
) -> None:
    settings = get_settings()
    if last_days:
        until_dt = datetime.now()
        since_dt = until_dt - timedelta(days=last_days)
    else:
        since_dt = _parse_ymd(since)
        until_dt = _parse_ymd(until)

    async with open_repo(settings.storage.data_path) as repo:
        sub = await repo.get_subscription(chat, thread or 0)
        title = sub.title if sub else (await repo.get_chat(chat) or {}).get("title")
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
        )
        result = await run_analysis(
            repo=repo, chat_id=chat, thread_id=thread, title=title, opts=opts
        )

    console.print(
        f"[bold cyan]Run[/] preset={result.preset} msgs={result.msg_count} "
        f"chunks={result.chunk_count} cache_hits={result.cache_hits}/"
        f"{result.cache_hits + result.cache_misses} cost=${result.total_cost_usd:.4f}"
    )

    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result.final_result, encoding="utf-8")
        console.print(f"[green]Written:[/] {output}")
    else:
        console.print(result.final_result)


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
