"""CLI commands for analyze + stats.

`cmd_analyze` resolves a chat reference, pulls messages fresh from Telegram
(no subscription row, no sync_state writes), and hands off to the existing
analysis pipeline. Default start-point is the dialog's unread marker.

Forum chats are first-class: `--thread N` targets one topic; `--all-flat`
collapses the whole forum into one analysis; `--all-per-topic` runs one
analysis per topic with unread. Without any mode flag in a TTY, a picker
prompts for choice.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from analyzetg.analyzer.pipeline import AnalysisOptions, AnalysisResult, run_analysis
from analyzetg.analyzer.prompts import PRESETS, Preset, load_custom_preset
from analyzetg.config import get_settings
from analyzetg.db.repo import Repo, open_repo
from analyzetg.enrich.base import EnrichOpts
from analyzetg.tg.client import tg_client
from analyzetg.tg.resolver import resolve
from analyzetg.tg.topics import ForumTopic, list_forum_topics
from analyzetg.util.logging import get_logger

console = Console()
log = get_logger(__name__)


def _parse_ymd(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d")


_ENRICH_KINDS = ("voice", "videonote", "video", "image", "doc", "link")


def _load_preset_for_commands(preset_name: str, prompt_file: Path | None) -> Preset | None:
    """Best-effort preset load — returns None if the preset isn't resolvable
    yet (e.g. `custom` without a prompt_file). Used only to read
    `enrich_kinds` for opts merging; the pipeline does its own strict load.
    """
    if preset_name == "custom":
        if prompt_file is None:
            return None
        try:
            return load_custom_preset(prompt_file)
        except Exception:
            return None
    return PRESETS.get(preset_name)


def build_enrich_opts(
    *,
    cli_enrich: str | None,
    cli_enrich_all: bool,
    cli_no_enrich: bool,
    preset: Preset | None,
) -> EnrichOpts:
    """Merge CLI flags → preset → config into a single EnrichOpts for a run.

    Precedence (first wins): --no-enrich, --enrich-all, --enrich=<csv> (union
    with preset.enrich_kinds), preset.enrich_kinds alone, config defaults.
    """
    s = get_settings()
    cfg = s.enrich

    if cli_no_enrich:
        return EnrichOpts(
            vision_model=cfg.vision_model,
            doc_model=cfg.doc_model,
            link_model=cfg.link_model,
            max_images_per_run=cfg.max_images_per_run,
            max_link_fetches_per_run=cfg.max_link_fetches_per_run,
            max_doc_bytes=cfg.max_doc_bytes,
            max_doc_chars=cfg.max_doc_chars,
            link_fetch_timeout_sec=cfg.link_fetch_timeout_sec,
            skip_link_domains=list(cfg.skip_link_domains),
            concurrency=cfg.concurrency,
        )

    if cli_enrich_all:
        enabled = set(_ENRICH_KINDS)
    elif cli_enrich:
        requested = {k.strip() for k in cli_enrich.split(",") if k.strip()}
        unknown = requested - set(_ENRICH_KINDS)
        if unknown:
            raise typer.BadParameter(
                f"Unknown --enrich kinds: {sorted(unknown)}. Valid: {', '.join(_ENRICH_KINDS)}"
            )
        # Union with whatever the preset considers essential.
        preset_kinds = set(preset.enrich_kinds) if preset else set()
        enabled = requested | preset_kinds
    else:
        # No CLI hint: take config defaults, union with preset's declared needs.
        enabled = {
            "voice" if cfg.voice else None,
            "videonote" if cfg.videonote else None,
            "video" if cfg.video else None,
            "image" if cfg.image else None,
            "doc" if cfg.doc else None,
            "link" if cfg.link else None,
        }
        enabled.discard(None)
        if preset:
            enabled |= set(preset.enrich_kinds)

    return EnrichOpts(
        voice="voice" in enabled,
        videonote="videonote" in enabled,
        video="video" in enabled,
        image="image" in enabled,
        doc="doc" in enabled,
        link="link" in enabled,
        vision_model=cfg.vision_model,
        doc_model=cfg.doc_model,
        link_model=cfg.link_model,
        max_images_per_run=cfg.max_images_per_run,
        max_link_fetches_per_run=cfg.max_link_fetches_per_run,
        max_doc_bytes=cfg.max_doc_bytes,
        max_doc_chars=cfg.max_doc_chars,
        link_fetch_timeout_sec=cfg.link_fetch_timeout_sec,
        skip_link_domains=list(cfg.skip_link_domains),
        concurrency=cfg.concurrency,
    )


def _derive_internal_id(chat_id: int) -> int | None:
    """Strip the `-100` prefix Telethon uses for channels/supergroups.

    Returns None for regular users / small groups where the id isn't
    suitable for a t.me/c/ link.
    """
    if chat_id >= 0:
        return None
    abs_id = abs(chat_id)
    if abs_id > 1_000_000_000_000:
        return abs_id - 1_000_000_000_000
    return None


def _parse_from_msg(value: str | None) -> int | None:
    if not value:
        return None
    if value.lstrip("-").isdigit():
        return int(value)
    from analyzetg.tg.links import parse

    return parse(value).msg_id


def _has_explicit_period(
    since_dt: datetime | None,
    until_dt: datetime | None,
    from_msg_id: int | None,
    full_history: bool,
) -> bool:
    return bool(since_dt or until_dt or from_msg_id is not None or full_history)


async def cmd_analyze(
    *,
    ref: str | None,
    thread: int | None,
    msg: str | None = None,
    from_msg: str | None,
    full_history: bool,
    since: str | None,
    until: str | None,
    last_days: int | None,
    preset: str | None,
    prompt_file: Path | None,
    model: str | None,
    filter_model: str | None,
    output: Path | None,
    console_out: bool = False,
    save_default: bool = False,
    mark_read: bool | None = None,
    no_cache: bool = False,
    include_transcripts: bool = True,
    min_msg_chars: int | None = None,
    enrich: str | None = None,
    enrich_all: bool = False,
    no_enrich: bool = False,
    all_flat: bool = False,
    all_per_topic: bool = False,
    folder: str | None = None,
    yes: bool = False,
) -> None:
    # Default preset — overridden later for single-msg mode.
    effective_preset = preset or "summary"
    resolved_preset = _load_preset_for_commands(effective_preset, prompt_file)
    enrich_opts = build_enrich_opts(
        cli_enrich=enrich,
        cli_enrich_all=enrich_all,
        cli_no_enrich=no_enrich,
        preset=resolved_preset,
    )

    # No ref but --folder → batch-analyze unread chats in that folder; skip wizard.
    if ref is None and folder:
        await run_all_unread_analyze(
            preset=effective_preset,
            prompt_file=prompt_file,
            model=model,
            filter_model=filter_model,
            output=output,
            console_out=console_out,
            mark_read=bool(mark_read),
            no_cache=no_cache,
            include_transcripts=include_transcripts,
            min_msg_chars=min_msg_chars,
            enrich_opts=enrich_opts,
            yes=yes,
            folder=folder,
        )
        return
    # No ref → interactive wizard (pick chat → thread → preset → period → run).
    # Wizard opens its own tg_client; return before this function tries to.
    if ref is None:
        from analyzetg.interactive import run_interactive_analyze

        await run_interactive_analyze(
            console_out=console_out,
            output=output,
            save_default=save_default,
            mark_read=mark_read,
        )
        return
    # Direct path: treat mark_read=None as False (CLI tri-state default).
    mark_read_bool = bool(mark_read)

    settings = get_settings()
    since_dt, until_dt = _compute_window(since, until, last_days)
    from_msg_id = _parse_from_msg(from_msg)
    msg_id = _parse_from_msg(msg)

    # If --msg is a full link with a chat identifier, it's authoritative —
    # use it as the ref so an ambiguous/stale text ref can't misdirect us.
    effective_ref = ref
    if msg:
        from analyzetg.tg.links import parse as _parse_link

        msg_parsed = _parse_link(msg)
        if msg_parsed.chat_id is not None or msg_parsed.username:
            if ref and ref != msg:
                console.print(f"[dim]→ Using chat from --msg link; ignoring ref '{ref}'[/]")
            effective_ref = msg

    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        console.print(f"[dim]→ Resolving[/] {effective_ref}")
        resolved = await resolve(client, repo, effective_ref)
        chat_id = resolved.chat_id
        thread_id = thread if thread is not None else (resolved.thread_id or 0)
        title = resolved.title
        console.print(
            f"[dim]→ Resolved[/] {title or chat_id} "
            f"[dim](id={chat_id}, kind={resolved.kind}"
            f"{', thread=' + str(thread_id) if thread_id else ''})[/]"
        )

        # A link like /group/100/5000 carries a msg_id. When no other period
        # flags were given, default to single-msg mode. Pass --from-msg /
        # --last-days / --full-history for the "from this point forward" intent.
        if (
            msg_id is None
            and from_msg_id is None
            and not full_history
            and resolved.msg_id is not None
            and since_dt is None
            and until_dt is None
        ):
            msg_id = resolved.msg_id

        if msg_id is not None:
            # User didn't specify a preset? Pick the single-msg one rather
            # than forcing a 3-topics/key-messages layout onto one message.
            single_preset_name = preset or "single_msg"
            single_preset = _load_preset_for_commands(single_preset_name, prompt_file)
            single_enrich = build_enrich_opts(
                cli_enrich=enrich,
                cli_enrich_all=enrich_all,
                cli_no_enrich=no_enrich,
                preset=single_preset,
            )
            await _run_single_msg(
                client=client,
                repo=repo,
                chat_id=chat_id,
                thread_id=thread_id or None,
                title=title,
                chat_username=resolved.username,
                chat_internal_id=_derive_internal_id(chat_id),
                msg_id=msg_id,
                preset=single_preset_name,
                prompt_file=prompt_file,
                model=model,
                filter_model=filter_model,
                output=output,
                console_out=console_out,
                no_cache=no_cache,
                include_transcripts=include_transcripts,
                min_msg_chars=min_msg_chars,
                enrich_opts=single_enrich,
            )
            return

        # --- Forum routing
        is_forum = resolved.kind == "forum"
        if is_forum and thread_id == 0 and not all_flat and not all_per_topic:
            # No explicit topic / mode. Decide via interactive picker or bail.
            all_flat, all_per_topic, thread_id = await _forum_pick_mode(client, chat_id, title)

        if is_forum and all_per_topic:
            await _run_forum_per_topic(
                client=client,
                repo=repo,
                chat_id=chat_id,
                chat_title=title,
                chat_username=resolved.username,
                chat_internal_id=_derive_internal_id(chat_id),
                since_dt=since_dt,
                until_dt=until_dt,
                from_msg_id=from_msg_id,
                full_history=full_history,
                preset=effective_preset,
                prompt_file=prompt_file,
                model=model,
                filter_model=filter_model,
                output=output,
                console_out=console_out,
                mark_read=mark_read_bool,
                no_cache=no_cache,
                include_transcripts=include_transcripts,
                min_msg_chars=min_msg_chars,
                enrich_opts=enrich_opts,
                yes=yes,
            )
            return

        topic_titles: dict[int, str] | None = None
        topic_markers: dict[int, int] | None = None

        if is_forum and all_flat:
            # thread_id=None → iter_messages skips the thread filter entirely.
            thread_id = None
            # Fetch topics once: we need both titles (for the formatter's
            # topic groups + LLM context) and per-topic read markers (to
            # correctly compute unread across a forum — see below). Same
            # API call used by _run_forum_per_topic.
            from analyzetg.tg.topics import list_forum_topics

            console.print("[dim]→ Listing forum topics for flat-forum grouping...[/]")
            topics_for_flat = await list_forum_topics(client, chat_id)
            topic_titles = {t.topic_id: t.title for t in topics_for_flat if t.title}
            topic_markers = {t.topic_id: int(t.read_inbox_max_id or 0) for t in topics_for_flat}

            # Unread semantics for a forum ≠ dialog-level read marker.
            # Telegram tracks read state **per topic**. Using the dialog
            # marker (what get_unread_state returns) would silently miss
            # unread messages in topics whose own marker is lower than the
            # dialog's. Instead:
            #   - Floor the backfill at min(per-topic markers) so we pull
            #     enough history.
            #   - Post-filter in run_analysis via topic_markers so each
            #     message survives only if msg_id > its own topic's marker.
            # Skipped when the user passed an explicit period.
            if not _has_explicit_period(since_dt, until_dt, from_msg_id, full_history):
                non_zero = [m for m in topic_markers.values() if m > 0]
                if non_zero:
                    from_msg_id = min(non_zero)
                    unread_across = sum(t.unread_count for t in topics_for_flat)
                    console.print(
                        f"[dim]→ Forum unread: {unread_across} across "
                        f"{len(topic_markers)} topics "
                        f"(floor msg_id={from_msg_id} from oldest per-topic marker)[/]"
                    )
                # If every topic marker is 0 (fresh account, never read
                # anything), leave from_msg_id=None — _determine_start will
                # fall back to the dialog unread state, which at that point
                # genuinely is the right answer.

        # --- Single topic in a forum + unread-default: resolve the topic's
        # own read marker so the unread-default path has a usable anchor.
        # Also captures the topic title so the default report path lands in
        # reports/{chat}/{topic-title}/analyze/ instead of /topic-{id}/.
        thread_title: str | None = None
        if is_forum and thread_id and thread_id > 0:
            from analyzetg.tg.topics import list_forum_topics

            unread_default = not _has_explicit_period(since_dt, until_dt, from_msg_id, full_history)
            if unread_default:
                console.print("[dim]→ Looking up topic's unread marker...[/]")
            topics = await list_forum_topics(client, chat_id)
            matched = next((t for t in topics if t.topic_id == thread_id), None)
            if matched is None:
                console.print(f"[red]Topic {thread_id} not found in this forum.[/]")
                raise typer.Exit(2)
            thread_title = matched.title
            if unread_default:
                if matched.unread_count == 0:
                    console.print(
                        f"[yellow]No unread messages in topic '{matched.title}'.[/] "
                        "Pass --last-days / --full-history to analyze anyway."
                    )
                    raise typer.Exit(0)
                from_msg_id = matched.read_inbox_max_id + 1
                console.print(
                    f"[dim]→ {matched.unread_count} unread in '{matched.title}' "
                    f"after msg_id={matched.read_inbox_max_id}[/]"
                )

        # --- Single-chat / single-topic / flat-forum path
        await _run_single(
            client=client,
            repo=repo,
            chat_id=chat_id,
            thread_id=thread_id,
            title=title,
            thread_title=thread_title,
            chat_username=resolved.username,
            chat_internal_id=_derive_internal_id(chat_id),
            since_dt=since_dt,
            until_dt=until_dt,
            from_msg_id=from_msg_id,
            full_history=full_history,
            preset=effective_preset,
            prompt_file=prompt_file,
            model=model,
            filter_model=filter_model,
            output=output,
            console_out=console_out,
            mark_read=mark_read_bool,
            no_cache=no_cache,
            include_transcripts=include_transcripts,
            min_msg_chars=min_msg_chars,
            enrich_opts=enrich_opts,
            topic_titles=topic_titles,
            topic_markers=topic_markers,
        )


async def _run_single_msg(
    *,
    client,
    repo: Repo,
    chat_id: int,
    thread_id: int | None,
    title: str | None,
    chat_username: str | None,
    chat_internal_id: int | None,
    msg_id: int,
    preset: str,
    prompt_file: Path | None,
    model: str | None,
    filter_model: str | None,
    output: Path | None,
    console_out: bool,
    no_cache: bool,
    include_transcripts: bool,
    min_msg_chars: int | None,
    enrich_opts: EnrichOpts | None = None,
) -> None:
    """Analyze exactly one message.

    Fetches it via Telethon if missing from the DB, enriches media when
    `enrich_opts` is set (or falls back to the legacy voice/videonote
    auto-transcribe when it isn't), then runs the selected preset bounded
    to that single msg_id.
    """
    from analyzetg.models import Subscription
    from analyzetg.tg.sync import normalize

    # Try local DB first (use thread_id=None so topic filter doesn't miss it).
    existing = await repo.iter_messages(chat_id, thread_id=None, min_msg_id=msg_id - 1, max_msg_id=msg_id)
    if not existing:
        console.print(f"[dim]→ Fetching message {msg_id} from Telegram...[/]")
        tel_msg = await client.get_messages(chat_id, ids=msg_id)
        if tel_msg is None:
            console.print(f"[red]Message {msg_id} not found in chat {chat_id}.[/]")
            raise typer.Exit(2)
        sub = await repo.get_subscription(chat_id, thread_id or 0) or Subscription(
            chat_id=chat_id,
            thread_id=thread_id or 0,
            title=title,
            source_kind="topic" if thread_id else "chat",
        )
        await repo.upsert_messages([normalize(tel_msg, sub)])
        existing = await repo.iter_messages(chat_id, thread_id=None, min_msg_id=msg_id - 1, max_msg_id=msg_id)
        if not existing:
            console.print(f"[red]Failed to persist message {msg_id}.[/]")
            raise typer.Exit(2)

    loaded = existing[0]

    # The enrichment stage inside run_analysis will handle voice/video
    # transcription, image description, etc. — whatever `enrich_opts` has on.
    # We still do a pre-flight sanity check: if the message has no text and
    # no enrichment is enabled that would rescue it, bail with a hint rather
    # than letting the pipeline silently return "msgs=0".
    has_text = bool((loaded.text or "").strip())
    has_transcript = bool((loaded.transcript or "").strip())
    enrich_would_rescue = False
    if enrich_opts is not None:
        mt = loaded.media_type
        enrich_map = {
            "voice": enrich_opts.voice,
            "videonote": enrich_opts.videonote,
            "video": enrich_opts.video,
            "photo": enrich_opts.image,
            "doc": enrich_opts.doc,
        }
        enrich_would_rescue = bool(enrich_map.get(mt or "", False))

    if not has_text and not has_transcript and not enrich_would_rescue:
        hint = ""
        if loaded.media_type in {"voice", "videonote", "video"}:
            hint = (
                f" The {loaded.media_type} has no transcript — "
                "enable with --enrich=voice (or --enrich-all), "
                "or check ffmpeg/OPENAI_API_KEY/max_media_duration."
            )
        elif loaded.media_type == "photo":
            hint = " It's a photo with no caption; try --enrich=image to describe it."
        elif loaded.media_type == "doc":
            hint = " It's a document; try --enrich=doc to extract text."
        console.print(f"[yellow]Nothing to analyze for msg {msg_id}.[/]{hint}")
        raise typer.Exit(0)

    console.print("[dim]→ Running analysis...[/]")
    opts = AnalysisOptions(
        preset=preset,
        prompt_file=prompt_file,
        model_override=model,
        filter_model_override=filter_model,
        use_cache=not no_cache,
        include_transcripts=include_transcripts,
        min_msg_chars=min_msg_chars,
        min_msg_id=msg_id - 1,
        max_msg_id=msg_id,
        enrich=enrich_opts,
    )
    # Pass thread_id=None: msg_id is chat-unique; thread filter would risk
    # excluding a forum-topic message we just fetched outside the subscription.
    result = await run_analysis(
        repo=repo,
        chat_id=chat_id,
        thread_id=None,
        title=title,
        opts=opts,
        chat_username=chat_username,
        chat_internal_id=chat_internal_id,
        client=client,
    )
    _print_and_write(result, output=output, title=title, console_out=console_out)


async def _run_single(
    *,
    client,
    repo: Repo,
    chat_id: int,
    thread_id: int | None,
    title: str | None,
    chat_username: str | None,
    chat_internal_id: int | None,
    since_dt: datetime | None,
    until_dt: datetime | None,
    from_msg_id: int | None,
    full_history: bool,
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
    enrich_opts: EnrichOpts | None = None,
    thread_title: str | None = None,
    topic_titles: dict[int, str] | None = None,
    topic_markers: dict[int, int] | None = None,
) -> None:
    """Analyze one chat or one thread via the shared pipeline."""
    from analyzetg.core.pipeline import prepare_chat_run
    from analyzetg.enrich.base import EnrichOpts as _EnrichOpts

    effective_enrich = enrich_opts if enrich_opts is not None else _EnrichOpts()

    prepared = await prepare_chat_run(
        client=client,
        repo=repo,
        settings=get_settings(),
        chat_id=chat_id,
        thread_id=thread_id,
        chat_title=title,
        thread_title=thread_title,
        chat_username=chat_username,
        chat_internal_id=chat_internal_id,
        since_dt=since_dt,
        until_dt=until_dt,
        from_msg_id=from_msg_id,
        full_history=full_history,
        enrich_opts=effective_enrich,
        include_transcripts=include_transcripts,
        min_msg_chars=min_msg_chars,
        topic_titles=topic_titles,
        topic_markers=topic_markers,
        mark_read=mark_read,
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
        enrich=effective_enrich,
    )
    result = await run_analysis(
        repo=repo,
        chat_id=chat_id,
        thread_id=thread_id,
        title=title,
        opts=opts,
        chat_username=chat_username,
        chat_internal_id=chat_internal_id,
        client=client,
        topic_titles=topic_titles,
        topic_markers=topic_markers,
        messages=prepared.messages,
    )

    _print_and_write(
        result,
        output=output,
        title=title,
        thread_title=thread_title,
        console_out=console_out,
    )

    if prepared.mark_read_fn and result.msg_count > 0:
        await prepared.mark_read_fn()


async def _run_forum_per_topic(
    *,
    client,
    repo: Repo,
    chat_id: int,
    chat_title: str | None,
    chat_username: str | None,
    chat_internal_id: int | None,
    since_dt: datetime | None,
    until_dt: datetime | None,
    from_msg_id: int | None,
    full_history: bool,
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
    enrich_opts: EnrichOpts | None = None,
    yes: bool = False,
) -> None:
    """One analysis per topic; reports land in reports/{chat-slug}/{topic-slug}/analyze/.

    Delegates the resolve/backfill/enrich prefix to
    core.pipeline.prepare_chat_runs_per_topic; this function only runs
    the LLM stage and writes each report. `yes=True` skips the
    iterator's internal typer.confirm — wizard already confirmed.
    """
    from analyzetg.core.pipeline import prepare_chat_runs_per_topic
    from analyzetg.enrich.base import EnrichOpts as _EnrichOpts

    effective_enrich = enrich_opts if enrich_opts is not None else _EnrichOpts()

    if console_out:
        base_dir: Path | None = None
    else:
        if output is not None and output.exists() and output.is_dir():
            base_dir = output
        elif output is not None and output.suffix:
            console.print(f"[red]--output {output} is a single file; per-topic mode needs a directory.[/]")
            raise typer.Exit(2)
        else:
            base_dir = output or Path("reports")
        base_dir.mkdir(parents=True, exist_ok=True)
    chat_slug = _chat_slug(chat_title, chat_id)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    async for prepared in prepare_chat_runs_per_topic(
        client=client,
        repo=repo,
        settings=get_settings(),
        chat_id=chat_id,
        chat_title=chat_title,
        chat_username=chat_username,
        chat_internal_id=chat_internal_id,
        since_dt=since_dt,
        until_dt=until_dt,
        from_msg_id=from_msg_id,
        full_history=full_history,
        enrich_opts=effective_enrich,
        include_transcripts=include_transcripts,
        min_msg_chars=min_msg_chars,
        mark_read=mark_read,
        yes=yes,
    ):
        try:
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
                enrich=effective_enrich,
            )
            result = await run_analysis(
                repo=repo,
                chat_id=chat_id,
                thread_id=prepared.thread_id,
                title=prepared.chat_title,
                opts=opts,
                chat_username=chat_username,
                chat_internal_id=chat_internal_id,
                client=client,
                messages=prepared.messages,
            )
            per_file: Path | None = None
            if base_dir is not None:
                topic_slug = _topic_slug(prepared.thread_title, prepared.thread_id or 0)
                per_file = base_dir / chat_slug / topic_slug / "analyze" / f"{preset}-{stamp}.md"
            _print_and_write(
                result,
                output=per_file,
                title=prepared.chat_title,
                thread_title=prepared.thread_title,
                console_out=console_out,
            )
            if prepared.mark_read_fn and result.msg_count > 0:
                await prepared.mark_read_fn()
        except typer.Exit:
            raise
        except Exception as e:
            log.error(
                "analyze.forum_per_topic.error",
                chat_id=chat_id,
                topic_id=prepared.thread_id,
                err=str(e)[:200],
            )
            console.print(f"[red]Topic {prepared.thread_title} failed:[/] {e}")


async def _forum_pick_mode(client, chat_id: int, chat_title: str | None) -> tuple[bool, bool, int]:
    """Interactively pick a forum mode. Returns (all_flat, all_per_topic, thread_id)."""
    console.print("[dim]→ Listing forum topics...[/]")
    topics = await list_forum_topics(client, chat_id)
    if not topics:
        console.print("[yellow]No topics in this forum.[/]")
        raise typer.Exit(0)

    if not sys.stdin.isatty():
        _print_topics_table(topics, with_unread=True)
        console.print(
            "\n[red]This is a forum — pick one of:[/]\n"
            "  --thread <id>       single topic\n"
            "  --all-per-topic     one analysis per topic\n"
            "  --all-flat          whole forum as one chat (needs a period flag)\n"
            "Or run without flags in a terminal for an interactive picker."
        )
        raise typer.Exit(2)

    _print_topics_table(topics, with_unread=True)
    prompt = "Pick topic id, [cyan]A[/]ll-flat, [cyan]P[/]er-topic, [cyan]Q[/]uit"
    while True:
        answer = typer.prompt(prompt.replace("[cyan]", "").replace("[/]", ""), default="P")
        answer = answer.strip()
        up = answer.upper()
        if up == "Q":
            console.print("[dim]Aborted.[/]")
            raise typer.Exit(0)
        if up == "A":
            return True, False, 0
        if up == "P":
            return False, True, 0
        if answer.isdigit():
            tid = int(answer)
            if any(t.topic_id == tid for t in topics):
                return False, False, tid
            console.print(f"[red]No topic with id={tid}.[/]")
            continue
        console.print("[red]Not a valid choice. Try again.[/]")


def _print_topics_table(topics: list[ForumTopic], *, with_unread: bool = True) -> None:
    t = Table(title="Forum topics")
    cols = ["id", "title", "unread", "top_msg", "closed", "pinned"]
    if not with_unread:
        cols.remove("unread")
    for col in cols:
        t.add_column(col)
    for topic in topics:
        row = [
            str(topic.topic_id),
            topic.title,
            str(topic.unread_count) if with_unread else None,
            str(topic.top_message or ""),
            "yes" if topic.closed else "",
            "yes" if topic.pinned else "",
        ]
        t.add_row(*[c for c in row if c is not None])
    console.print(t)


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
    enrich_opts: EnrichOpts | None = None,
    folder: str | None = None,
    yes: bool = False,
) -> None:
    """No <ref>: list dialogs with unread messages, confirm, analyze each.

    Delegates resolve/backfill/enrich to prepare_all_unread_runs and
    only adds the LLM stage + per-chat report write. Folder filtering
    flows through to the iterator unchanged.
    """
    from analyzetg.core.pipeline import prepare_all_unread_runs
    from analyzetg.enrich.base import EnrichOpts as _EnrichOpts

    effective_enrich = enrich_opts if enrich_opts is not None else _EnrichOpts()

    if console_out:
        out_dir: Path | None = None
    else:
        out_dir = _resolve_output_dir(output, 0) or Path("reports")
        out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    async for prepared in prepare_all_unread_runs(
        client=client,
        repo=repo,
        settings=get_settings(),
        enrich_opts=effective_enrich,
        include_transcripts=include_transcripts,
        min_msg_chars=min_msg_chars,
        mark_read=mark_read,
        folder=folder,
        yes=yes,
    ):
        try:
            opts = AnalysisOptions(
                preset=preset,
                prompt_file=prompt_file,
                model_override=model,
                filter_model_override=filter_model,
                use_cache=not no_cache,
                include_transcripts=include_transcripts,
                min_msg_chars=min_msg_chars,
                enrich=effective_enrich,
            )
            result = await run_analysis(
                repo=repo,
                chat_id=prepared.chat_id,
                thread_id=None,
                title=prepared.chat_title,
                opts=opts,
                chat_username=prepared.chat_username,
                chat_internal_id=prepared.chat_internal_id,
                client=client,
                messages=prepared.messages,
            )
            per_file = None
            if out_dir:
                chat_out = out_dir / _chat_slug(prepared.chat_title, prepared.chat_id) / "analyze"
                chat_out.mkdir(parents=True, exist_ok=True)
                per_file = chat_out / f"{preset}-{stamp}.md"
            _print_and_write(
                result,
                output=per_file,
                title=prepared.chat_title,
                console_out=console_out,
            )
            if prepared.mark_read_fn and result.msg_count > 0:
                await prepared.mark_read_fn()
        except Exception as e:
            log.error("analyze.no_ref.chat_error", chat_id=prepared.chat_id, err=str(e)[:200])
            console.print(f"[red]Failed:[/] {e}")


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


# `\w` in Python 3 is Unicode-aware by default (matches Cyrillic, CJK, etc.),
# which is why titles like "ОБЩИЙ ЧАТ" now slug to "общий-чат" instead of
# collapsing to "" and falling back to `topic-<id>`. Modern file systems
# (APFS, ext4, NTFS) and every shell we care about handle UTF-8 filenames
# fine, so there's no reason to transliterate to ASCII.
_SLUG_RE = re.compile(r"[^\w\-]+", re.UNICODE)


def _slugify(text: str) -> str:
    """Lowercase, punctuation-stripped, 40-char-capped directory slug.

    Preserves Unicode letters (Cyrillic, CJK, Arabic, …). Empty /
    all-punctuation input returns `""` — callers must provide a
    fallback (see `_chat_slug`/`_topic_slug`).
    """
    slug = _SLUG_RE.sub("-", text).strip("-").lower()
    return slug[:40]


def _unique_path(base: Path) -> Path:
    """Return `base` or the first numbered sibling that doesn't exist yet.

    Two runs inside the same second (same preset + same chat) would otherwise
    overwrite each other. We append `-2`, `-3`, ... to the filename stem
    until we find a free slot, capping at 100 to surface any genuine
    pathological case (e.g. an infinite loop in a calling script).
    """
    if not base.exists():
        return base
    stem = base.stem
    suffix = base.suffix
    parent = base.parent
    for i in range(2, 100):
        cand = parent / f"{stem}-{i}{suffix}"
        if not cand.exists():
            return cand
    raise RuntimeError(f"100 collisions at {base} — check the caller for a runaway loop")


def _chat_slug(title: str | None, chat_id: int) -> str:
    """Directory-safe identifier for a chat.

    Falls back to `chat-<abs chat_id>` when the title is empty or slugs
    down to nothing (e.g. emoji-only Telegram titles). The `abs()` drops
    Telethon's `-100` channel prefix so the directory name stays tidy;
    chat_id is still recoverable from `atg describe`.
    """
    if title and (slug := _slugify(title)):
        return slug
    return f"chat-{abs(chat_id)}"


def _topic_slug(title: str | None, thread_id: int) -> str:
    """Directory-safe identifier for a forum topic. Falls back to
    `topic-<id>` when the title isn't known at write time — keeps the
    directory structure deterministic even when the caller only has
    the numeric id (e.g. direct `--thread N` without topic lookup).
    """
    if title and (slug := _slugify(title)):
        return slug
    return f"topic-{thread_id}"


def _fmt_cost_precise(value: float) -> str:
    """Cost string with enough decimals to be non-zero for tiny runs.

    Report header needs precision the terminal summary can skip — a $0.003
    summary hiding behind `$0.00` is what triggered this refactor.
    """
    if value <= 0:
        return "$0"
    if value < 0.001:
        return "< $0.001"
    if value < 0.01:
        return f"${value:.4f}"
    if value < 1.0:
        return f"${value:.3f}"
    return f"${value:.2f}"


def _fmt_period_header(period: tuple[datetime | None, datetime | None] | None) -> str:
    """Human-readable period for the report header.

    (None, None) → "unread / full history (no date filter)" since both
    cases collapse to the same thing in the DB query. Concrete datetimes
    render as YYYY-MM-DD HH:MM, leaving ambiguity off the page.
    """
    if period is None or (period[0] is None and period[1] is None):
        return "unread / full history (no date filter)"
    a = period[0].strftime("%Y-%m-%d %H:%M") if period[0] else "…"
    b = period[1].strftime("%Y-%m-%d %H:%M") if period[1] else "…"
    return f"{a} → {b}"


def _render_report_header(result: AnalysisResult, *, title: str | None) -> str:
    """Build the fixed-format metadata block prepended to every saved report.

    Lets users (and future-you) answer "what chat, what period, what model,
    what did this cost?" in 3 seconds instead of grepping git history. Order
    of fields is chosen so the most load-bearing facts (chat, period, count)
    are at the top and the diagnostic details follow.
    """
    lines: list[str] = ["---"]
    lines.append(f"**Chat:** {title or result.chat_id}")
    if result.thread_id:
        lines.append(f"**Thread:** {result.thread_id}")
    lines.append(f"**Period:** {_fmt_period_header(result.period)}")

    msg_line = f"**Messages analyzed:** {result.msg_count}"
    if result.raw_msg_count and result.raw_msg_count != result.msg_count:
        dropped = result.raw_msg_count - result.msg_count
        msg_line += f" (from {result.raw_msg_count} raw, −{dropped} after filter/dedupe)"
    lines.append(msg_line)

    preset_line = f"**Preset:** `{result.preset}`"
    if result.prompt_version:
        preset_line += f" (v={result.prompt_version})"
    lines.append(preset_line)

    model_line = f"**Model:** `{result.model}`"
    if result.chunk_count > 1 and result.filter_model and result.filter_model != result.model:
        model_line += f" (+ `{result.filter_model}` for map phase)"
    lines.append(model_line)

    if result.chunk_count:
        lines.append(f"**Chunks:** {result.chunk_count}")

    total_calls = result.cache_hits + result.cache_misses
    if total_calls:
        lines.append(f"**Cache:** {result.cache_hits}/{total_calls} hits")

    if result.enrich_kinds:
        lines.append(f"**Enrichment:** {', '.join(result.enrich_kinds)}")
    if result.enrich_summary:
        lines.append(f"**Enrichment detail:** {result.enrich_summary}")

    analysis_cost = result.total_cost_usd
    if result.enrich_cost_usd:
        # Analysis cost already excludes enrichment (enrichment logs separately
        # into usage_log under kind='audio'/'chat' phase labels). Show both so
        # the user can audit where the spend went.
        total = analysis_cost + result.enrich_cost_usd
        lines.append(
            f"**Cost:** {_fmt_cost_precise(total)} "
            f"(analysis {_fmt_cost_precise(analysis_cost)} + "
            f"enrichment {_fmt_cost_precise(result.enrich_cost_usd)})"
        )
    else:
        lines.append(f"**Cost:** {_fmt_cost_precise(analysis_cost)}")

    lines.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    lines.append("---")
    lines.append("")  # blank line before the LLM body
    return "\n".join(lines)


def _print_and_write(
    result: AnalysisResult,
    *,
    output: Path | None,
    title: str | None,
    thread_title: str | None = None,
    console_out: bool = False,
) -> None:
    """Write the report to disk (and/or stdout) and log the summary line.

    `thread_title` is used only for the default output path when `output`
    is None and the run targeted a forum topic; callers that already
    computed an explicit `output=` path (batch mode, per-topic loop)
    don't need to pass it.
    """
    console.print(
        f"[bold cyan]Run[/] preset={result.preset} msgs={result.msg_count} "
        f"chunks={result.chunk_count} cache_hits={result.cache_hits}/"
        f"{result.cache_hits + result.cache_misses} cost=${result.total_cost_usd:.4f}"
    )
    body = _render_report_header(result, title=title) + _with_truncation_banner(result)

    if result.truncated:
        console.print(
            "[bold red]⚠ Output truncated[/] — the model hit "
            "[cyan]output_budget_tokens[/]. Edit the preset file "
            f"([cyan]presets/{result.preset}.md[/]) to raise it, or re-run with "
            "[cyan]--no-cache[/] if a stale cache is in the way."
        )

    if console_out:
        from rich.markdown import Markdown
        from rich.rule import Rule

        console.print(Rule(title or "result", style="cyan"))
        console.print(Markdown(body))
        console.print(Rule(style="cyan"))
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output = _unique_path(output)
            output.write_text(body, encoding="utf-8")
            console.print(f"[green]Also saved:[/] {output}")
        return

    if output is None:
        output = _default_output_path(
            chat_title=title,
            chat_id=result.chat_id,
            thread_id=result.thread_id or 0,
            thread_title=thread_title,
            preset=result.preset,
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    # Even with seconds-precision stamps, two parallel invocations can still
    # land in the same second — _unique_path appends -2/-3 to avoid overwrite.
    output = _unique_path(output)
    output.write_text(body, encoding="utf-8")
    console.print(f"[green]Written:[/] {output}")


def _with_truncation_banner(result) -> str:
    if not getattr(result, "truncated", False):
        return result.final_result
    banner = (
        "> ⚠️ **Output was truncated.** The model hit "
        "`output_budget_tokens` and stopped mid-response.\n"
        f"> Raise the cap in `presets/{result.preset}.md` "
        "(e.g. `output_budget_tokens: 4000`) and re-run with `--no-cache`.\n\n"
    )
    return banner + result.final_result


def _default_output_path(
    *,
    chat_title: str | None,
    chat_id: int,
    thread_id: int = 0,
    thread_title: str | None = None,
    preset: str,
) -> Path:
    """Pick a default report location.

    Layout:
      - Non-forum: `reports/{chat-slug}/analyze/{preset}-{stamp}.md`
      - Forum topic: `reports/{chat-slug}/{topic-slug}/analyze/{preset}-{stamp}.md`

    Topic nesting keeps per-topic analyses from piling up in a single
    directory (which was the old layout's actual failure mode — running
    `summary` on three topics of the same forum produced three files
    named `summary-<date>.md` in `reports/<forum>/analyze/`, with no
    way to tell which topic each belongs to).
    """
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    parts: list[str] = ["reports", _chat_slug(chat_title, chat_id)]
    if thread_id:
        parts.append(_topic_slug(thread_title, thread_id))
    parts.extend(["analyze", f"{preset}-{stamp}.md"])
    return Path(*parts)


def _compute_window(
    since: str | None, until: str | None, last_days: int | None
) -> tuple[datetime | None, datetime | None]:
    if last_days:
        until_dt = datetime.now()
        since_dt = until_dt - timedelta(days=last_days)
        return since_dt, until_dt
    return _parse_ymd(since), _parse_ymd(until)


async def run_all_unread_analyze(
    *,
    preset: str = "summary",
    prompt_file: Path | None = None,
    model: str | None = None,
    filter_model: str | None = None,
    output: Path | None = None,
    console_out: bool = False,
    mark_read: bool = False,
    no_cache: bool = False,
    include_transcripts: bool = True,
    min_msg_chars: int | None = None,
    enrich_opts: EnrichOpts | None = None,
    folder: str | None = None,
    yes: bool = False,
) -> None:
    """Public: run the batch-across-all-unread-chats flow (was the old no-ref default).

    Pass `folder="Alpha"` (or any case-insensitive substring of a folder title)
    to restrict the batch to chats in that Telegram folder.
    `yes=True` skips the interactive confirmation — the wizard sets this so
    the user isn't asked twice after already approving the plan."""
    settings = get_settings()
    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
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
            enrich_opts=enrich_opts,
            yes=yes,
            folder=folder,
        )


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
