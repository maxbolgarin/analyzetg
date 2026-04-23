"""End-to-end analysis pipeline (spec §9)."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from analyzetg.analyzer.chunker import build_chunks
from analyzetg.analyzer.filters import FilterOpts, dedupe, filter_messages
from analyzetg.analyzer.formatter import (
    build_link_template,
    chat_header_preamble,
    format_messages,
)
from analyzetg.analyzer.hasher import batch_hash, reduce_hash
from analyzetg.analyzer.openai_client import build_messages, chat_complete, make_client
from analyzetg.analyzer.prompts import PRESETS, REDUCE_PROMPT, Preset, load_custom_preset
from analyzetg.config import get_settings
from analyzetg.db.repo import Repo
from analyzetg.util.logging import get_logger

log = get_logger(__name__)


def _pipeline_console():
    """Shared Rich Console for progress displays in this module."""
    from rich.console import Console

    return Console()


async def _progress_single(*, label: str, coro):
    """Run a single awaitable under a transient Rich spinner.

    Gives the user something to look at while an OpenAI call is pending,
    instead of dead silence for 5–20 seconds.
    """
    from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

    with Progress(
        SpinnerColumn(),
        TextColumn(f"[dim]{label}[/]"),
        TimeElapsedColumn(),
        transient=True,
        console=_pipeline_console(),
    ) as p:
        p.add_task("call", total=None)
        return await coro


@dataclass(slots=True)
class AnalysisResult:
    preset: str
    model: str
    chat_id: int
    thread_id: int
    msg_count: int
    chunk_count: int
    batch_hashes: list[str]
    final_result: str
    total_cost_usd: float
    cache_hits: int
    cache_misses: int
    run_id: int | None = None
    truncated: bool = False  # any stage hit max_completion_tokens


@dataclass(slots=True)
class AnalysisOptions:
    preset: str = "summary"
    prompt_file: Path | None = None
    model_override: str | None = None
    filter_model_override: str | None = None
    use_cache: bool = True
    include_transcripts: bool = True
    min_msg_chars: int | None = None
    since: datetime | None = None
    until: datetime | None = None
    min_msg_id: int | None = None
    dedupe_forwards: bool | None = None

    def options_payload(self, preset: Preset) -> dict[str, Any]:
        """Hash ingredients that must bust cache when toggled."""
        s = get_settings()
        return {
            "min_msg_chars": self.min_msg_chars
            if self.min_msg_chars is not None
            else s.analyze.min_msg_chars,
            "include_transcripts": self.include_transcripts,
            "dedupe_forwards": self.dedupe_forwards
            if self.dedupe_forwards is not None
            else s.analyze.dedupe_forwards,
            "language": s.openai.audio_language,
            "temperature": s.openai.temperature,
            "output_budget": preset.output_budget_tokens,
            "map_output": preset.map_output_tokens,
        }


def _load_preset(opts: AnalysisOptions) -> Preset:
    if opts.preset == "custom":
        if not opts.prompt_file:
            raise ValueError("--prompt-file is required for preset=custom")
        return load_custom_preset(opts.prompt_file)
    preset = PRESETS.get(opts.preset)
    if not preset:
        raise ValueError(f"Unknown preset: {opts.preset}")
    return preset


async def _call_cached(
    *,
    repo: Repo,
    oai,
    preset: Preset,
    model: str,
    bhash: str,
    system: str,
    static_ctx: str,
    dynamic: str,
    max_tokens: int,
    run_context: dict[str, Any],
    use_cache: bool,
) -> tuple[str, float, bool, bool]:
    """Return (text, cost, was_cache_hit, truncated). Writes cache and usage log on miss.

    `truncated` is only reliable for cache misses — cached rows predate the
    flag and are assumed not-truncated (re-run with --no-cache if in doubt)."""
    if use_cache:
        hit = await repo.cache_get(bhash)
        if hit:
            log.debug("cache.hit", batch=bhash[:10])
            return hit["result"], 0.0, True, False
    messages = build_messages(system, static_ctx, dynamic)
    res = await chat_complete(
        oai,
        repo=repo,
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        context={**run_context, "batch_hash": bhash},
    )
    if use_cache and not res.truncated:
        # Don't cache truncated results — caching a partial summary would
        # silently poison every future run of the same query.
        await repo.cache_put(
            bhash,
            preset.name,
            model,
            preset.prompt_version,
            res.text,
            res.prompt_tokens,
            res.cached_tokens,
            res.completion_tokens,
            res.cost_usd,
        )
    return res.text, float(res.cost_usd or 0.0), False, res.truncated


async def run_analysis(
    *,
    repo: Repo,
    chat_id: int,
    thread_id: int | None,
    title: str | None,
    opts: AnalysisOptions,
    chat_username: str | None = None,
    chat_internal_id: int | None = None,
) -> AnalysisResult:
    settings = get_settings()
    preset = _load_preset(opts)

    final_model = opts.model_override or preset.final_model or settings.openai.chat_model_default
    filter_model = opts.filter_model_override or preset.filter_model or settings.openai.filter_model_default

    # --- Load + filter + dedupe
    thread_param = thread_id if thread_id is not None else 0
    msgs = await repo.iter_messages(
        chat_id,
        thread_id=thread_param,
        since=opts.since,
        until=opts.until,
        min_msg_id=opts.min_msg_id,
    )
    f_opts = FilterOpts(
        min_msg_chars=opts.min_msg_chars
        if opts.min_msg_chars is not None
        else settings.analyze.min_msg_chars,
        include_transcripts=opts.include_transcripts,
        text_only=not opts.include_transcripts,
    )
    msgs = filter_messages(msgs, f_opts)
    if opts.dedupe_forwards if opts.dedupe_forwards is not None else settings.analyze.dedupe_forwards:
        msgs = dedupe(msgs)

    if not msgs:
        return AnalysisResult(
            preset=preset.name,
            model=final_model,
            chat_id=chat_id,
            thread_id=thread_param,
            msg_count=0,
            chunk_count=0,
            batch_hashes=[],
            final_result="_No messages matched the filters._",
            total_cost_usd=0.0,
            cache_hits=0,
            cache_misses=0,
        )

    period = (opts.since, opts.until)
    link_template = build_link_template(
        chat_username=chat_username,
        chat_internal_id=chat_internal_id,
        thread_id=thread_id,
    )
    static_ctx = chat_header_preamble(title, period, link_template=link_template)
    # user_overhead: template minus {messages} — static, cacheable
    user_overhead = preset.render_user(
        period=_fmt_period(period),
        title=title or "—",
        msg_count=len(msgs),
        messages="",
    )

    # --- Choose chunking strategy
    chunking_model = final_model if not preset.needs_reduce else filter_model
    chunks = build_chunks(
        msgs,
        model=chunking_model,
        system_prompt=preset.system,
        user_overhead=user_overhead,
        output_budget=preset.output_budget_tokens,
        safety_margin=settings.analyze.safety_margin_tokens,
        soft_break_minutes=settings.analyze.chunk_soft_break_minutes,
    )
    log.info("analyze.chunks", preset=preset.name, chunks=len(chunks), msgs=len(msgs))

    oai = make_client()
    options_payload = opts.options_payload(preset)
    run_ctx = {"preset": preset.name, "chat_id": chat_id}
    total_cost = 0.0
    cache_hits = 0
    cache_misses = 0
    batch_hashes: list[str] = []
    any_truncated = False

    # --- Single pass: one chunk OR preset disables reduce
    if len(chunks) <= 1 or not preset.needs_reduce:
        chunk = chunks[0]
        bhash = batch_hash(preset.name, preset.prompt_version, final_model, chunk.msg_ids, options_payload)
        batch_hashes.append(bhash)
        dynamic = format_messages(chunk.messages, period=period, title=None, link_template=link_template)
        text, cost, hit, truncated = await _progress_single(
            label=f"Analyzing ({len(msgs)} msgs, {preset.name}/{final_model})",
            coro=_call_cached(
                repo=repo,
                oai=oai,
                preset=preset,
                model=final_model,
                bhash=bhash,
                system=preset.system,
                static_ctx=static_ctx,
                dynamic=preset.render_user(
                    period=_fmt_period(period),
                    title=title or "—",
                    msg_count=len(msgs),
                    messages=dynamic,
                ),
                max_tokens=preset.output_budget_tokens,
                run_context=run_ctx,
                use_cache=opts.use_cache,
            ),
        )
        total_cost += cost
        cache_hits += int(hit)
        cache_misses += int(not hit)
        any_truncated = any_truncated or truncated
        run_id = await _record_run(
            repo,
            chat_id,
            thread_param,
            preset.name,
            period,
            len(msgs),
            len(chunks),
            batch_hashes,
            text,
            total_cost,
        )
        return AnalysisResult(
            preset=preset.name,
            model=final_model,
            chat_id=chat_id,
            thread_id=thread_param,
            msg_count=len(msgs),
            chunk_count=len(chunks),
            batch_hashes=batch_hashes,
            final_result=text,
            total_cost_usd=total_cost,
            cache_hits=cache_hits,
            cache_misses=cache_misses,
            run_id=run_id,
            truncated=any_truncated,
        )

    # --- Map-reduce branch
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    map_sem = asyncio.Semaphore(settings.analyze.map_concurrency)

    with Progress(
        SpinnerColumn(),
        TextColumn("[dim]Analyzing chunks ({task.fields[model]})[/]"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        transient=True,
        console=_pipeline_console(),
    ) as _map_progress:
        _map_task = _map_progress.add_task("map", total=len(chunks), model=filter_model)

        async def _map(chunk) -> tuple[str, str, float, bool, bool]:
            bh = batch_hash(preset.name, preset.prompt_version, filter_model, chunk.msg_ids, options_payload)
            dynamic = format_messages(chunk.messages, period=period, title=None, link_template=link_template)
            user = preset.render_user(
                period=_fmt_period(period),
                title=title or "—",
                msg_count=len(chunk.messages),
                messages=dynamic,
            )
            try:
                async with map_sem:
                    t, c, hit, tr = await _call_cached(
                        repo=repo,
                        oai=oai,
                        preset=preset,
                        model=filter_model,
                        bhash=bh,
                        system=preset.system,
                        static_ctx=static_ctx,
                        dynamic=user,
                        max_tokens=min(preset.output_budget_tokens, preset.map_output_tokens),
                        run_context={**run_ctx, "phase": "map"},
                        use_cache=opts.use_cache,
                    )
                return bh, t, c, hit, tr
            finally:
                _map_progress.advance(_map_task)

        map_results = await asyncio.gather(*[_map(c) for c in chunks])
    map_hashes = [mh for mh, _, _, _, _ in map_results]
    batch_hashes.extend(map_hashes)
    for _, _, cost, hit, tr in map_results:
        total_cost += cost
        cache_hits += int(hit)
        cache_misses += int(not hit)
        any_truncated = any_truncated or tr

    reduce_bh = reduce_hash(preset.name, preset.prompt_version, final_model, map_hashes, options_payload)
    batch_hashes.append(reduce_bh)

    joined = "\n\n---\n\n".join(f"[Фрагмент {i + 1}]\n{r[1]}" for i, r in enumerate(map_results))
    reduce_user = (
        f"{REDUCE_PROMPT}\n\n"
        f"Период: {_fmt_period(period)}\n"
        f"Чат: {title or '—'}\n"
        f"Число сообщений: {len(msgs)}\n"
        f"Число фрагментов: {len(map_results)}\n\n"
        f"{joined}"
    )
    text, cost, hit, truncated = await _progress_single(
        label=f"Merging {len(map_results)} fragments ({final_model})",
        coro=_call_cached(
            repo=repo,
            oai=oai,
            preset=preset,
            model=final_model,
            bhash=reduce_bh,
            system=preset.system,
            static_ctx=static_ctx,
            dynamic=reduce_user,
            max_tokens=preset.output_budget_tokens,
            run_context={**run_ctx, "phase": "reduce"},
            use_cache=opts.use_cache,
        ),
    )
    total_cost += cost
    cache_hits += int(hit)
    cache_misses += int(not hit)
    any_truncated = any_truncated or truncated

    run_id = await _record_run(
        repo,
        chat_id,
        thread_param,
        preset.name,
        period,
        len(msgs),
        len(chunks),
        batch_hashes,
        text,
        total_cost,
    )
    return AnalysisResult(
        preset=preset.name,
        model=final_model,
        chat_id=chat_id,
        thread_id=thread_param,
        msg_count=len(msgs),
        chunk_count=len(chunks),
        batch_hashes=batch_hashes,
        final_result=text,
        total_cost_usd=total_cost,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        run_id=run_id,
        truncated=any_truncated,
    )


def _fmt_period(period: tuple[datetime | None, datetime | None]) -> str:
    a = period[0].strftime("%Y-%m-%d") if period[0] else "…"
    b = period[1].strftime("%Y-%m-%d") if period[1] else "…"
    return f"{a} — {b}"


async def _record_run(
    repo: Repo,
    chat_id: int,
    thread_id: int,
    preset: str,
    period: tuple[datetime | None, datetime | None],
    msg_count: int,
    chunk_count: int,
    hashes: list[str],
    result: str,
    cost: float,
) -> int:
    return await repo.record_run(
        chat_id=chat_id,
        thread_id=thread_id,
        preset=preset,
        from_date=period[0],
        to_date=period[1],
        msg_count=msg_count,
        chunk_count=chunk_count,
        batch_hashes=hashes,
        final_result=result,
        total_cost_usd=cost,
    )


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
