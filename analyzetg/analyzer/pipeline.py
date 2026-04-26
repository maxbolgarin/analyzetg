"""End-to-end analysis pipeline (spec §9)."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
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
from analyzetg.analyzer.hasher import batch_hash, reduce_hash, text_hash
from analyzetg.analyzer.openai_client import build_messages, chat_complete, make_client
from analyzetg.analyzer.prompts import (
    BASE_VERSION,
    Preset,
    compose_system_prompt,
    get_presets,
    load_custom_preset,
)
from analyzetg.config import get_settings
from analyzetg.db.repo import Repo
from analyzetg.enrich.base import EnrichOpts
from analyzetg.enrich.pipeline import enrich_messages
from analyzetg.i18n import t as i18n_t
from analyzetg.util.logging import get_logger

log = get_logger(__name__)


# Rough token estimate per formatted message line (sender + timestamp +
# body). Used for up-front cost previews; the real pipeline counts exactly
# via tiktoken. Cyrillic averages ~60 tokens/msg, Latin-script English
# ~40, autodetect / unknown / mixed → 50 (middle ground).
_AVG_TOKENS_BY_LANG: dict[str, int] = {"ru": 60, "en": 40}


def _avg_tokens_per_msg(content_lang: str | None) -> int:
    """Per-message token estimate keyed by the chat content's language.

    Falls through to 50 (a midpoint) for autodetect / empty / unknown
    codes so the preview is approximately right even without an explicit
    setting.
    """
    if not content_lang:
        return 50
    return _AVG_TOKENS_BY_LANG.get(content_lang, 50)


# Back-compat alias for any external caller still importing the constant.
# Resolves at import time to the EN baseline; if anyone needs language-
# specific accuracy they should call _avg_tokens_per_msg() directly.
AVG_TOKENS_PER_MSG = _AVG_TOKENS_BY_LANG["en"]


def _resolve_content_lang(settings: Any) -> str:
    """`content_language` falls back to `language` when empty."""
    locale = getattr(settings, "locale", None)
    if locale is None:
        return "en"
    return (locale.content_language or locale.language or "en").lower()


def _resolve_language(settings: Any) -> str:
    locale = getattr(settings, "locale", None)
    if locale is None:
        return "en"
    return (locale.language or "en").lower()


def estimate_cost(
    *,
    n_messages: int,
    preset: Preset,
    settings: Any,
) -> tuple[float | None, float | None]:
    """Return (lower, upper) cost estimate in USD for an analyze run.

    Mirrors what `run_analysis` will actually do: builds chunks under the
    same budget formula as `chunker.build_chunks`, charges every chunk for
    the system+user overhead (the pipeline re-sends those), bounds the map
    completion at the preset's `map_output_tokens`, and adds a reduce pass
    only when there's more than one chunk.

    Returns `(None, None)` if pricing is missing for either model — caller
    should treat that as "can't enforce a budget" (used by `--max-cost`).
    """
    import math as _math

    from analyzetg.analyzer.chunker import model_context_window
    from analyzetg.util.pricing import chat_cost
    from analyzetg.util.tokens import count_tokens as _ct

    avg_tok = _avg_tokens_per_msg(_resolve_content_lang(settings))
    total_input_body = max(1, int(n_messages * avg_tok))

    filter_model = preset.filter_model
    final_model = preset.final_model
    if settings.pricing.chat.get(filter_model) is None or settings.pricing.chat.get(final_model) is None:
        return None, None

    system_tokens = _ct(preset.system, filter_model)
    user_overhead_tokens = _ct(preset.user_template, filter_model)
    per_chunk_overhead = system_tokens + user_overhead_tokens

    context = model_context_window(filter_model)
    safety = int(getattr(settings.analyze, "safety_margin_tokens", 4000))
    map_out_cap = preset.map_output_tokens
    budget = max(500, context - per_chunk_overhead - map_out_cap - safety)

    chunks = max(1, _math.ceil(total_input_body / budget))

    map_input_tokens = total_input_body + chunks * per_chunk_overhead
    map_out_lo = int(chunks * map_out_cap * 0.4)
    map_out_hi = int(chunks * map_out_cap)

    if chunks > 1 and preset.needs_reduce:
        reduce_overhead = _ct(preset.system, final_model) + _ct(preset.user_template, final_model)
        reduce_out = preset.output_budget_tokens
        reduce_input_lo = map_out_lo + reduce_overhead
        reduce_input_hi = map_out_hi + reduce_overhead
    else:
        reduce_input_lo = reduce_input_hi = 0
        reduce_out = 0

    def _cost(prompt: int, completion: int, model: str) -> float:
        return float(chat_cost(model, prompt, 0, completion, settings=settings) or 0.0)

    lo = _cost(map_input_tokens, map_out_lo, filter_model) + _cost(
        reduce_input_lo, int(reduce_out * 0.4), final_model
    )
    hi = _cost(map_input_tokens, map_out_hi, filter_model) + _cost(reduce_input_hi, reduce_out, final_model)
    return lo, hi


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
    # Metadata used by the file-writing layer to render a report header —
    # all optional so direct callers (tests) can skip them.
    prompt_version: str = ""
    filter_model: str | None = None
    period: tuple[datetime | None, datetime | None] | None = None
    enrich_kinds: list[str] = field(default_factory=list)
    enrich_cost_usd: float = 0.0
    enrich_summary: str = ""
    raw_msg_count: int = 0  # before filter / dedupe / enrich — shows filtering loss


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
    max_msg_id: int | None = None
    dedupe_forwards: bool | None = None
    enrich: EnrichOpts | None = None  # None → resolved from config at run time.
    # `--by` filter: substring match on sender_name (case-insensitive) OR an
    # exact sender_id. Mutually exclusive at CLI parse time.
    sender_substring: str | None = None
    sender_id: int | None = None
    # Channel + comments: when True the cache key reflects that the input
    # included messages from the linked discussion group, so toggling the
    # flag produces different cached results for the same channel+period.
    with_comments: bool = False
    comments_chat_id: int | None = None

    def options_payload(self, preset: Preset) -> dict[str, Any]:
        """Hash ingredients that must bust cache when toggled."""
        s = get_settings()
        enrich_kinds = sorted(self.enrich.kinds_enabled()) if self.enrich else []
        payload: dict[str, Any] = {
            "min_msg_chars": self.min_msg_chars
            if self.min_msg_chars is not None
            else s.analyze.min_msg_chars,
            "include_transcripts": self.include_transcripts,
            "dedupe_forwards": self.dedupe_forwards
            if self.dedupe_forwards is not None
            else s.analyze.dedupe_forwards,
            # `content_language` selects which presets/<lang>/ tree the
            # analysis uses (and thus the language of the LLM's prompt
            # input AND the LLM's output). `locale.language` is UI-only
            # (saved-report headings, wizard) — it does NOT affect any
            # LLM input, so it is intentionally OMITTED from the cache
            # key to avoid wasted cache misses on UI-only toggles. If
            # you ever make `language` flow into the prompt, add it back.
            "content_language": _resolve_content_lang(s),
            "audio_language": s.openai.audio_language or "",
            "temperature": s.openai.temperature,
            "output_budget": preset.output_budget_tokens,
            "map_output": preset.map_output_tokens,
            "enrich_kinds": enrich_kinds,
            # Sender filter is part of the cache key — toggling it must
            # produce different cached results for the same message set.
            "sender_substring": (self.sender_substring or "").casefold() or None,
            "sender_id": self.sender_id,
            # `with_comments` flips the prompt's input set (channel-only vs
            # channel+linked discussion). Include the linked chat id too so
            # a re-link would invalidate.
            "with_comments": self.with_comments,
            "comments_chat_id": self.comments_chat_id if self.with_comments else None,
        }
        if self.enrich:
            payload["enrich_options"] = {
                "vision_model": self.enrich.vision_model,
                "doc_model": self.enrich.doc_model,
                "link_model": self.enrich.link_model,
                "audio_model": self.enrich.audio_model,
                "max_images_per_run": self.enrich.max_images_per_run,
                "max_link_fetches_per_run": self.enrich.max_link_fetches_per_run,
                "max_doc_bytes": self.enrich.max_doc_bytes,
                "max_doc_chars": self.enrich.max_doc_chars,
                "link_fetch_timeout_sec": self.enrich.link_fetch_timeout_sec,
                "skip_link_domains": sorted(self.enrich.skip_link_domains),
            }
        return payload


def _with_prompt_inputs(
    options_payload: dict[str, Any],
    *,
    system: str,
    static_ctx: str,
    dynamic: str,
) -> dict[str, Any]:
    payload = dict(options_payload)
    payload["prompt_input"] = {
        "system": text_hash(system),
        "static": text_hash(static_ctx),
        "dynamic": text_hash(dynamic),
    }
    return payload


def _load_preset(opts: AnalysisOptions, language: str = "en") -> Preset:
    """Load the requested preset from `presets/<language>/`.

    `language` here is the LLM-input / content language — `run_analysis`
    passes `content_language` to it. The kwarg name is kept generic so
    the helper stays callable from contexts that don't think in terms of
    UI-vs-content (e.g., direct test fixtures).
    """
    if opts.preset == "custom":
        if not opts.prompt_file:
            raise ValueError("--prompt-file is required for preset=custom")
        return load_custom_preset(opts.prompt_file, language=language)
    presets = get_presets(language)
    preset = presets.get(opts.preset)
    if not preset:
        available = ", ".join(sorted(presets.keys())) or "(none)"
        raise ValueError(f"Unknown preset: {opts.preset!r}. Available in language {language!r}: {available}.")
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

    A hit whose row reports `truncated=1` is treated as a miss and re-run —
    the invariant in `cache_put` never stores truncated results today, but
    keeping the guard on the read side protects against any future write path
    that bypasses it (and surfaces legacy truncated rows that slipped in
    before the invariant existed)."""
    if use_cache:
        hit = await repo.cache_get(bhash)
        if hit and not hit.get("truncated"):
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
    client=None,
    topic_titles: dict[int, str] | None = None,
    topic_markers: dict[int, int] | None = None,
    messages: list[Any] | None = None,
    chat_groups: dict[int, dict] | None = None,
    language: str | None = None,
    content_language: str | None = None,
) -> AnalysisResult:
    """Run the end-to-end analysis for a chat/thread/period.

    `client` is required when `opts.enrich` requests media-based enrichment
    (voice/video/image/doc). Callers that only want text analysis can pass
    `client=None`.

    `topic_titles` turns on topic-grouped formatting — used by the
    all-flat forum path so the LLM sees `=== Топик: X ===` separators
    instead of a time-interleaved jumble. Leave as None for non-forum /
    per-topic / single-topic analyses.

    `topic_markers` (dict[topic_id → read_inbox_max_id]) enables per-topic
    unread filtering for flat-forum mode. A single dialog-level `min_msg_id`
    can't express "msg X is unread in topic A, msg Y is unread in topic B"
    — forums carry read state per topic. When this is provided, every
    message is kept only if `msg.msg_id > topic_markers[msg.thread_id]`.
    Leave as None to skip the filter (default for all other paths).

    `messages`: optional pre-prepared list. When supplied, skips the
    iter_messages / per-topic filter / enrichment / filter_messages /
    dedupe pipeline — the consumer has already done all of that (e.g.,
    via `core.pipeline.prepare_chat_run`). When None (default), falls
    back to the legacy path that does it all internally.
    """
    settings = get_settings()
    if language is None:
        language = _resolve_language(settings)
    if content_language is None:
        content_language = _resolve_content_lang(settings)
    # `content_language` selects the prompts tree (presets/<lang>/) and
    # everything LLM-facing. `language` is only the UI / report-heading
    # language. They can differ — e.g. EN UI analyzing a RU chat with
    # native RU prompts. The user picks per `[locale]` config.
    preset = _load_preset(opts, language=content_language)

    final_model = opts.model_override or preset.final_model or settings.openai.chat_model_default
    filter_model = opts.filter_model_override or preset.filter_model or settings.openai.filter_model_default

    thread_param = thread_id if thread_id is not None else 0

    if messages is not None:
        # Consumer (cmd_analyze via prepare_chat_run) has already done
        # backfill, per-topic filter, enrichment, filter+dedupe. Use
        # what they gave us verbatim.
        msgs = messages
        raw_count = len(messages)
        enrich_cost = 0.0
        enrich_summary_str = ""
        enrich_kinds_used: list[str] = []
        if opts.enrich is not None and opts.enrich.any_enabled():
            enrich_kinds_used = list(opts.enrich.kinds_enabled())
    else:
        # --- Load messages
        msgs = await repo.iter_messages(
            chat_id,
            thread_id=thread_id,
            since=opts.since,
            until=opts.until,
            min_msg_id=opts.min_msg_id,
            max_msg_id=opts.max_msg_id,
        )

        # Per-topic unread filter for flat-forum mode. `iter_messages` applies
        # a single `min_msg_id` floor — fine for a non-forum chat, but forums
        # track read state per topic. Drop messages already read in their
        # specific topic. Messages whose thread_id isn't in the map (e.g.
        # topic deleted between marker fetch and analysis) pass through.
        if topic_markers:
            before = len(msgs)
            msgs = [
                m
                for m in msgs
                if m.thread_id is None
                or m.thread_id not in topic_markers
                or m.msg_id > topic_markers[m.thread_id]
            ]
            if before != len(msgs):
                log.info(
                    "analyze.topic_markers.filtered",
                    kept=len(msgs),
                    dropped=before - len(msgs),
                )

        raw_count = len(msgs)

        # --- Enrichment (voice → text, image → description, etc.) runs BEFORE
        # filtering so enrichment can rescue a photo-only or voice-only message
        # from being dropped by min_msg_chars / text_only.
        enrich_opts = opts.enrich
        enrich_cost = 0.0
        enrich_summary_str = ""
        enrich_kinds_used: list[str] = []
        if enrich_opts is not None and enrich_opts.any_enabled() and msgs:
            stats = await enrich_messages(msgs, client=client, repo=repo, opts=enrich_opts)
            enrich_summary_str = stats.summary()
            enrich_cost = float(stats.total_cost_usd)
            enrich_kinds_used = list(enrich_opts.kinds_enabled())
            if enrich_summary_str:
                log.info("analyze.enrich", summary=enrich_summary_str)

        f_opts = FilterOpts(
            min_msg_chars=opts.min_msg_chars
            if opts.min_msg_chars is not None
            else settings.analyze.min_msg_chars,
            include_transcripts=opts.include_transcripts,
            text_only=not opts.include_transcripts,
            sender_substring=opts.sender_substring,
            sender_id=opts.sender_id,
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
            prompt_version=preset.prompt_version,
            filter_model=filter_model,
            period=(opts.since, opts.until),
            enrich_kinds=enrich_kinds_used,
            enrich_cost_usd=enrich_cost,
            enrich_summary=enrich_summary_str,
            raw_msg_count=raw_count,
        )

    period = (opts.since, opts.until)
    link_template = build_link_template(
        chat_username=chat_username,
        chat_internal_id=chat_internal_id,
        thread_id=thread_id,
    )
    # When `chat_groups` is set the formatter renders a header per chat
    # group inline; the preamble's single-template path is suppressed
    # (`chat_header_preamble` knows). format_messages similarly drops
    # the global `=== Чат: … ===` line in chat-groups mode.
    static_ctx = chat_header_preamble(
        title,
        period,
        link_template=link_template,
        topic_titles=topic_titles,
        chat_groups=chat_groups,
        language=content_language,
    )
    # user_overhead: template minus {messages} — static, cacheable
    user_overhead = preset.render_user(
        period=_fmt_period(period),
        title=title or "—",
        msg_count=len(msgs),
        messages="",
    )

    # Compose the full system prompt once (base + optional forum addendum +
    # preset-specific task). Used by chunker AND every OpenAI call so the
    # token accounting and actual prompt stay consistent — feeding
    # preset.system to the chunker but composed_system to the LLM would
    # under-budget each chunk by the base's ~300 tokens.
    composed_system = compose_system_prompt(
        preset.system,
        topic_titles=topic_titles,
        language=content_language,
    )

    # --- Choose chunking strategy
    chunking_model = final_model if not preset.needs_reduce else filter_model
    chunks = build_chunks(
        msgs,
        model=chunking_model,
        system_prompt=composed_system,
        user_overhead=user_overhead,
        output_budget=preset.output_budget_tokens,
        safety_margin=settings.analyze.safety_margin_tokens,
        soft_break_minutes=settings.analyze.chunk_soft_break_minutes,
    )
    log.info("analyze.chunks", preset=preset.name, chunks=len(chunks), msgs=len(msgs))

    oai = make_client()
    options_payload = opts.options_payload(preset)
    # Any change to the shared base system prompt (presets/_base.md) bumps
    # BASE_VERSION, which lands here and busts every preset's cache — one
    # knob instead of per-preset prompt_version bumps.
    options_payload["base_version"] = BASE_VERSION
    # The forum topic set enters the LLM context via compose_system_prompt
    # AND via the preamble's `Форум: …` line. A rename/add/remove must
    # invalidate cache; sorted tuples are deterministic across runs.
    if topic_titles:
        options_payload["forum_topics"] = sorted(topic_titles.items())
    run_ctx = {"preset": preset.name, "chat_id": chat_id}
    total_cost = 0.0
    cache_hits = 0
    cache_misses = 0
    batch_hashes: list[str] = []
    any_truncated = False

    # --- Single pass: one chunk OR preset disables reduce
    if len(chunks) <= 1 or not preset.needs_reduce:
        chunk = chunks[0]
        dynamic = format_messages(
            chunk.messages,
            period=period,
            title=None,
            link_template=link_template,
            topic_titles=topic_titles,
            chat_groups=chat_groups,
            language=content_language,
        )
        user = preset.render_user(
            period=_fmt_period(period),
            title=title or "—",
            msg_count=len(msgs),
            messages=dynamic,
        )
        call_options = _with_prompt_inputs(
            options_payload,
            system=composed_system,
            static_ctx=static_ctx,
            dynamic=user,
        )
        bhash = batch_hash(preset.name, preset.prompt_version, final_model, chunk.msg_ids, call_options)
        batch_hashes.append(bhash)
        text, cost, hit, truncated = await _progress_single(
            label=f"Analyzing ({len(msgs)} msgs, {preset.name}/{final_model})",
            coro=_call_cached(
                repo=repo,
                oai=oai,
                preset=preset,
                model=final_model,
                bhash=bhash,
                system=composed_system,
                static_ctx=static_ctx,
                dynamic=user,
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
            prompt_version=preset.prompt_version,
            filter_model=filter_model,
            period=(opts.since, opts.until),
            enrich_kinds=enrich_kinds_used,
            enrich_cost_usd=enrich_cost,
            enrich_summary=enrich_summary_str,
            raw_msg_count=raw_count,
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
            dynamic = format_messages(
                chunk.messages,
                period=period,
                title=None,
                link_template=link_template,
                topic_titles=topic_titles,
                chat_groups=chat_groups,
                language=content_language,
            )
            user = preset.render_user(
                period=_fmt_period(period),
                title=title or "—",
                msg_count=len(chunk.messages),
                messages=dynamic,
            )
            call_options = _with_prompt_inputs(
                options_payload,
                system=composed_system,
                static_ctx=static_ctx,
                dynamic=user,
            )
            bh = batch_hash(preset.name, preset.prompt_version, filter_model, chunk.msg_ids, call_options)
            try:
                async with map_sem:
                    t, c, hit, tr = await _call_cached(
                        repo=repo,
                        oai=oai,
                        preset=preset,
                        model=filter_model,
                        bhash=bh,
                        system=composed_system,
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

    # Reduce stage prompt is fed to the LLM → labels and instructions in
    # `content_language` so the model sees one coherent language.
    fragment_label = i18n_t("fragment_label", content_language)
    joined = "\n\n---\n\n".join(f"[{fragment_label} {i + 1}]\n{r[1]}" for i, r in enumerate(map_results))
    from analyzetg.analyzer.prompts import _load_reduce_prompt as _load_reduce

    reduce_prompt = _load_reduce(content_language)
    reduce_user = (
        f"{reduce_prompt}\n\n"
        f"{i18n_t('period_label', content_language)}: {_fmt_period(period)}\n"
        f"{i18n_t('chat_label', content_language)}: {title or '—'}\n"
        f"{i18n_t('messages_label', content_language)}: {len(msgs)}\n"
        f"{i18n_t('fragment_count_label', content_language)}: {len(map_results)}\n\n"
        f"{joined}"
    )
    reduce_options = _with_prompt_inputs(
        options_payload,
        system=composed_system,
        static_ctx=static_ctx,
        dynamic=reduce_user,
    )
    reduce_bh = reduce_hash(preset.name, preset.prompt_version, final_model, map_hashes, reduce_options)
    batch_hashes.append(reduce_bh)
    text, cost, hit, truncated = await _progress_single(
        label=f"Merging {len(map_results)} fragments ({final_model})",
        coro=_call_cached(
            repo=repo,
            oai=oai,
            preset=preset,
            model=final_model,
            bhash=reduce_bh,
            system=composed_system,
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
        prompt_version=preset.prompt_version,
        filter_model=filter_model,
        period=(opts.since, opts.until),
        enrich_kinds=enrich_kinds_used,
        enrich_cost_usd=enrich_cost,
        enrich_summary=enrich_summary_str,
        raw_msg_count=raw_count,
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
