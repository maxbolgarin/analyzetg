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

import sys
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from analyzetg.analyzer.pipeline import AnalysisOptions, AnalysisResult, run_analysis
from analyzetg.analyzer.prompts import Preset, get_presets, load_custom_preset
from analyzetg.config import get_settings
from analyzetg.core.paths import chat_slug as _chat_slug
from analyzetg.core.paths import compute_window as _compute_window
from analyzetg.core.paths import derive_internal_id as _derive_internal_id
from analyzetg.core.paths import has_explicit_period as _has_explicit_period
from analyzetg.core.paths import parse_ymd as _parse_ymd
from analyzetg.core.paths import slugify as _slugify  # noqa: F401  re-export for tests
from analyzetg.core.paths import topic_slug as _topic_slug
from analyzetg.core.paths import unique_path as _unique_path
from analyzetg.db.repo import Repo, open_repo
from analyzetg.enrich.base import EnrichOpts
from analyzetg.i18n import t as _t
from analyzetg.i18n import tf as _tf
from analyzetg.tg.client import tg_client
from analyzetg.tg.resolver import resolve
from analyzetg.tg.topics import ForumTopic, list_forum_topics
from analyzetg.util.logging import get_logger

console = Console()
log = get_logger(__name__)


_ENRICH_KINDS = ("voice", "videonote", "video", "image", "doc", "link")

# Match `[#12345](https://t.me/...)` markdown citations the LLM emits per the
# Q&A / analysis system prompts. Captures (msg_id_int, url). The link side
# uses `[^)]+` — Telegram URLs (the only template the LLM gets) never
# contain `)`, so this is unambiguous in practice. URLs with literal parens
# (Wikipedia-style) would truncate; we don't ship those as citations.
_CITATION_RE = __import__("re").compile(r"\[#(\d+)\]\(([^)]+)\)")


_VERIFY_SYSTEM: dict[str, str] = {
    "en": (
        "You are a reviewer of a Telegram-chat analysis. You are given: (a) the "
        "source messages, (b) an analytical report based on them. Your task is "
        "to find statements in the report that are NOT supported by the cited "
        "message or are not backed by a citation at all. Output a short "
        "markdown bullet list: one bullet per statement + reason (no citation "
        "/ contradicts #N / doesn't follow from the context). If everything "
        "checks out, respond with a single line: 'All claims supported.'"
    ),
    "ru": (
        "Ты — рецензент анализа Telegram-чата. Тебе даны: (а) исходные "
        "сообщения, (б) аналитический отчёт по ним. Твоя задача — найти "
        "в отчёте утверждения, которые НЕ подтверждаются цитируемым "
        "сообщением или вообще не подкреплены цитатой. Выведи короткий "
        "markdown-список таких пунктов: каждый пункт = одно утверждение + "
        "причина (нет цитаты / противоречит #N / не следует из контекста). "
        "Если всё подтверждено, ответь одной строкой: 'All claims supported.'"
    ),
}

_VERIFY_USER_TEMPLATE: dict[str, tuple[str, str, str]] = {
    "en": (
        "Messages:\n\n",
        "\n\nReport:\n\n",
        "\n\nUnsupported statements (or 'All claims supported.'):",
    ),
    "ru": (
        "Сообщения:\n\n",
        "\n\nОтчёт:\n\n",
        "\n\nНеподтверждённые утверждения (или 'All claims supported.'):",
    ),
}


async def _self_check(
    *,
    result: AnalysisResult,
    messages,
    repo: Repo,
    content_language: str = "en",
) -> str:
    """Cheap-model audit pass over an analysis report.

    Sends source messages + the produced analysis to `filter_model_default`
    (gpt-5.4-nano-class) with a system prompt asking for unsupported
    claims. Output is a short markdown bullet list, or "All claims
    supported." for a clean run. Caller appends as `## Verification`
    (heading translated via i18n using the UI `language`, NOT
    `content_language`).

    `content_language` selects the verification system prompt + the
    formatter's label language. It should match the language of the
    analysis report being audited so the auditor and the report speak
    the same language.

    Failures (network blip, parse glitch) return empty string — the
    primary report is what the user paid for; verification is a bonus.
    """
    from analyzetg.analyzer.formatter import format_messages
    from analyzetg.analyzer.openai_client import build_messages, chat_complete, make_client

    settings = get_settings()
    used_model = settings.openai.filter_model_default
    sys_prompt = _VERIFY_SYSTEM.get(content_language, _VERIFY_SYSTEM["en"])
    head, mid, tail = _VERIFY_USER_TEMPLATE.get(content_language, _VERIFY_USER_TEMPLATE["en"])
    formatted_msgs = format_messages(messages, language=content_language)
    user_text = f"{head}{formatted_msgs}{mid}{result.final_result}{tail}"
    try:
        oai = make_client()
        res = await chat_complete(
            oai,
            repo=repo,
            model=used_model,
            messages=build_messages(sys_prompt, "", user_text),
            max_tokens=1500,
            context={"phase": "self_check", "preset": result.preset},
        )
        return (res.text or "").strip()
    except Exception as e:
        log.warning("analyze.self_check_failed", err=str(e)[:200])
        return ""


async def _expand_citations(
    body: str,
    *,
    chat_id: int,
    repo: Repo,
    context_n: int,
    thread_id: int | None = None,
    cap: int = 30,
    language: str = "en",
) -> str:
    """Append a `## Sources` (or its localized equivalent) section with
    `<details>`-fold blocks for every cited msg_id, showing `context_n`
    messages on each side.

    Capped at `cap` distinct citations so a runaway LLM that cites 200
    messages doesn't 10x the report file size. Falls back to the body
    unchanged when no citations are present or context_n <= 0.
    """
    if context_n <= 0:
        return body
    seen: list[int] = []
    for m in _CITATION_RE.finditer(body):
        try:
            mid = int(m.group(1))
        except ValueError:
            continue
        if mid in seen:
            continue
        seen.append(mid)
        if len(seen) >= cap:
            break
    if not seen:
        return body

    blocks: list[str] = ["", f"## {_t('sources_heading', language)}", ""]
    for mid in seen:
        msgs = await repo.get_messages_around(
            chat_id, mid, before=context_n, after=context_n, thread_id=thread_id
        )
        if not msgs:
            continue
        # Find anchor — message with the cited msg_id; mark it visually.
        anchor_date = None
        anchor_sender = None
        for m in msgs:
            if m.msg_id == mid:
                anchor_date = m.date.strftime("%Y-%m-%d %H:%M") if m.date else "?"
                anchor_sender = m.sender_name or f"id:{m.sender_id}" if m.sender_id else "?"
                break
        summary = f"#{mid} — {anchor_date or '?'} {anchor_sender or ''}".strip()
        blocks.append(f"<details><summary>{summary}</summary>\n")
        for m in msgs:
            marker = "►" if m.msg_id == mid else " "
            ts = m.date.strftime("%H:%M") if m.date else "—"
            sender = m.sender_name or f"id:{m.sender_id or '?'}"
            text = (m.text or m.transcript or "").replace("\n", " ").strip()
            if len(text) > 280:
                text = text[:277] + "…"
            # Escape backticks so a stray ` in the message body doesn't
            # close the inline-code span we open around `[ts #msg_id]`,
            # bleeding markdown formatting into the rest of the line.
            text = text.replace("`", "\\`")
            blocks.append(f"{marker} `[{ts} #{m.msg_id}]` **{sender}**: {text}")
        blocks.append("\n</details>")
        blocks.append("")  # spacer between blocks
    return body + "\n".join(blocks)


def _load_preset_for_commands(
    preset_name: str, prompt_file: Path | None, *, language: str = "en"
) -> Preset | None:
    """Best-effort preset load — returns None if the preset isn't resolvable
    yet (e.g. `custom` without a prompt_file). Used only to read
    `enrich_kinds` for opts merging; the pipeline does its own strict load.
    """

    if preset_name == "custom":
        if prompt_file is None:
            return None
        try:
            return load_custom_preset(prompt_file, language=language)
        except Exception:
            return None
    try:
        return get_presets(language).get(preset_name)
    except Exception:
        return None


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
    msg: str | None = None,
    from_msg: str | None,
    full_history: bool,
    since: str | None,
    until: str | None,
    last_days: int | None,
    last_hours: int | None = None,
    preset: str | None = None,
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
    max_cost: float | None = None,
    post_saved: bool = False,
    dry_run: bool = False,
    cite_context: int = 0,
    self_check: bool = False,
    by: str | None = None,
    post_to: str | None = None,
    repeat_last: bool = False,
    with_comments: bool = False,
    yes: bool = False,
    language: str | None = None,
    content_language: str | None = None,
    youtube_source: str = "auto",
) -> None:
    settings_for_lang = get_settings()
    effective_language = (language or settings_for_lang.locale.language or "en").lower()
    effective_content_language = (
        content_language or settings_for_lang.locale.content_language or effective_language
    ).lower()

    # Default preset — overridden later for single-msg mode.
    effective_preset = preset or "summary"
    # Preset directory is selected by `content_language` (it determines
    # which prompts the LLM gets); UI / report-heading language is
    # tracked separately as `effective_language`.
    resolved_preset = _load_preset_for_commands(
        effective_preset, prompt_file, language=effective_content_language
    )
    enrich_opts = build_enrich_opts(
        cli_enrich=enrich,
        cli_enrich_all=enrich_all,
        cli_no_enrich=no_enrich,
        preset=resolved_preset,
    )

    # No ref but --folder → batch-analyze unread chats in that folder; skip wizard.
    if ref is None and folder:
        # Batch mode is unread-only today. Reject period flags explicitly —
        # silently analyzing unread when the user asked for --full-history
        # (or --since/--until/--last-days/--from-msg) would both hide data
        # and waste LLM spend. Pointing them at single-chat mode is cheap.
        rejected = []
        if full_history:
            rejected.append("--full-history")
        if since:
            rejected.append("--since")
        if until:
            rejected.append("--until")
        if last_days is not None:
            rejected.append("--last-days")
        if last_hours is not None:
            rejected.append("--last-hours")
        if from_msg:
            rejected.append("--from-msg")
        if rejected:
            raise typer.BadParameter(
                f"--folder is unread-only and does not support {', '.join(rejected)}. "
                "Run per-chat with `atg analyze <ref> <flag>` for a specific window."
            )
        # --output must be a directory (or absent) — we write one file per chat.
        # Validate upfront so a typo like `-o report.md` doesn't surface only
        # after Telegram has been hit for every chat in the batch.
        if output and output.suffix and not (output.exists() and output.is_dir()):
            raise typer.BadParameter(
                f"--output {output} looks like a file but --folder batch needs a directory "
                "(one report per chat). Pass a directory path or drop --output."
            )
        await run_all_unread_analyze(
            preset=effective_preset,
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
            language=effective_language,
            content_language=effective_content_language,
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
            post_saved=post_saved,
            max_cost=max_cost,
            self_check=self_check,
            cite_context=cite_context,
            no_cache=no_cache,
            dry_run=dry_run,
            by=by,
            post_to=post_to,
            with_comments=with_comments,
            language=effective_language,
            content_language=effective_content_language,
        )
        return
    # Direct path: treat mark_read=None as False (CLI tri-state default).
    mark_read_bool = bool(mark_read)

    settings = get_settings()
    since_dt, until_dt = _compute_window(since, until, last_days, last_hours)
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
                console.print(f"[dim]{_tf('using_chat_from_msg_link', ref=ref)}[/]")
            effective_ref = msg

    # YouTube branch: detect a YouTube URL early and dispatch to the
    # dedicated handler. No Telegram resolve / backfill / mark_read.
    from analyzetg.youtube.urls import is_youtube_url as _is_yt

    if effective_ref and _is_yt(effective_ref):
        _rejected_yt = []
        if folder:
            _rejected_yt.append("--folder")
        if thread is not None:
            _rejected_yt.append("--thread")
        if all_flat:
            _rejected_yt.append("--all-flat")
        if all_per_topic:
            _rejected_yt.append("--all-per-topic")
        if with_comments:
            _rejected_yt.append("--with-comments")
        if from_msg:
            _rejected_yt.append("--from-msg")
        if full_history:
            _rejected_yt.append("--full-history")
        if since:
            _rejected_yt.append("--since")
        if until:
            _rejected_yt.append("--until")
        if last_days is not None:
            _rejected_yt.append("--last-days")
        if last_hours is not None:
            _rejected_yt.append("--last-hours")
        if msg:
            _rejected_yt.append("--msg")
        if repeat_last:
            _rejected_yt.append("--repeat-last")
        if mark_read is not None:
            _rejected_yt.append("--mark-read/--no-mark-read")
        if _rejected_yt:
            raise typer.BadParameter(
                f"YouTube URLs do not support {', '.join(_rejected_yt)}. "
                "These flags only apply to Telegram chats."
            )
        if youtube_source not in ("auto", "captions", "audio"):
            raise typer.BadParameter(
                f"Invalid --youtube-source={youtube_source!r}. Valid: auto, captions, audio."
            )

        from analyzetg.youtube.commands import cmd_analyze_youtube

        await cmd_analyze_youtube(
            url=effective_ref,
            preset=preset,
            prompt_file=prompt_file,
            model=model,
            filter_model=filter_model,
            output=output,
            console_out=console_out,
            no_cache=no_cache,
            max_cost=max_cost,
            dry_run=dry_run,
            self_check=self_check,
            cite_context=cite_context,
            post_to=post_to,
            post_saved=post_saved,
            language=effective_language,
            content_language=effective_content_language,
            youtube_source=youtube_source,  # type: ignore[arg-type]
            yes=yes,
        )
        return

    async with tg_client(settings) as client, open_repo(settings.storage.data_path) as repo:
        console.print(f"[dim]{_tf('resolving', ref=effective_ref)}[/]")
        resolved = await resolve(client, repo, effective_ref)
        chat_id = resolved.chat_id
        thread_id = thread if thread is not None else (resolved.thread_id or 0)
        title = resolved.title
        console.print(
            f"[dim]→ Resolved[/] {title or chat_id} "
            f"[dim](id={chat_id}, kind={resolved.kind}"
            f"{', thread=' + str(thread_id) if thread_id else ''})[/]"
        )

        # `--repeat-last`: load saved kwargs and use them as defaults for
        # everything the user didn't explicitly set on this run. We can't
        # tell "explicit" from "default" perfectly without Typer's source
        # tracking, so we use a "default-shaped" heuristic: None / False / 0
        # / "" → take from saved. Anything else → respect the user's value.
        if repeat_last:
            saved = await repo.get_last_run_args(chat_id, int(thread_id or 0))
            if saved is None:
                console.print(
                    "[yellow]No saved run found[/] for this chat. "
                    "Run `atg analyze <ref>` once normally first."
                )
                raise typer.Exit(0)
            console.print(f"[dim]{_tf('repeating_last_run', ts=saved.get('__updated_at'))}[/]")
            # Map saved keys → local vars (only fill defaults).
            if not preset and saved.get("preset"):
                effective_preset = saved["preset"]
            if not since and saved.get("since_ymd"):
                since = saved["since_ymd"]
                since_dt = _parse_ymd(since)
            if not until and saved.get("until_ymd"):
                until = saved["until_ymd"]
                until_dt = _parse_ymd(until)
            if from_msg_id is None and saved.get("from_msg_id") is not None:
                from_msg_id = int(saved["from_msg_id"])
            if not full_history and saved.get("full_history"):
                full_history = True
            if not by and saved.get("by"):
                by = saved["by"]
            if not with_comments and saved.get("with_comments"):
                with_comments = True
            if not post_to and saved.get("post_to"):
                post_to = saved["post_to"]
            if not post_saved and saved.get("post_saved"):
                post_saved = True
            if cite_context == 0 and saved.get("cite_context"):
                cite_context = int(saved["cite_context"])
            if not self_check and saved.get("self_check"):
                self_check = True
            if language is None and saved.get("language"):
                effective_language = str(saved["language"]).lower()
            if content_language is None and saved.get("content_language"):
                effective_content_language = str(saved["content_language"]).lower()
            if mark_read is None and saved.get("mark_read") is not None:
                mark_read = bool(saved["mark_read"])
                mark_read_bool = mark_read
            if min_msg_chars is None and saved.get("min_msg_chars") is not None:
                min_msg_chars = int(saved["min_msg_chars"])
            if not model and saved.get("model"):
                model = saved["model"]
            if not filter_model and saved.get("filter_model"):
                filter_model = saved["filter_model"]

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
            single_preset = _load_preset_for_commands(
                single_preset_name, prompt_file, language=effective_content_language
            )
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
                language=effective_language,
                content_language=effective_content_language,
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
                language=effective_language,
                content_language=effective_content_language,
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

            console.print(f"[dim]{_t('listing_forum_topics_for_flat')}[/]")
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
                console.print(f"[dim]{_t('looking_up_topic_marker')}[/]")
            topics = await list_forum_topics(client, chat_id)
            matched = next((t for t in topics if t.topic_id == thread_id), None)
            if matched is None:
                console.print(f"[red]{_tf('topic_not_found', thread_id=thread_id)}[/]")
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
            max_cost=max_cost,
            post_saved=post_saved,
            dry_run=dry_run,
            cite_context=cite_context,
            self_check=self_check,
            by=by,
            post_to=post_to,
            with_comments=with_comments,
            yes=yes,
            include_transcripts=include_transcripts,
            min_msg_chars=min_msg_chars,
            enrich_opts=enrich_opts,
            topic_titles=topic_titles,
            topic_markers=topic_markers,
            language=effective_language,
            content_language=effective_content_language,
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
    language: str = "en",
    content_language: str = "en",
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
        console.print(f"[dim]{_tf('fetching_message', msg_id=msg_id)}[/]")
        tel_msg = await client.get_messages(chat_id, ids=msg_id)
        if tel_msg is None:
            console.print(f"[red]{_tf('msg_not_found_in_chat', msg_id=msg_id, chat_id=chat_id)}[/]")
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
            console.print(f"[red]{_tf('failed_persist_msg', msg_id=msg_id)}[/]")
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
        console.print(f"[yellow]{_tf('nothing_to_analyze_for_msg', msg_id=msg_id, hint=hint)}[/]")
        raise typer.Exit(0)

    console.print(f"[dim]{_t('running_analysis')}[/]")
    # When the single message is a video / videonote, mark the run as
    # `source_kind="video"` so the formatter renders `=== Video: <title> ===`
    # and the base prompt's video framing applies. Voice messages stay
    # `chat` — the "Video" label would be misleading. The preset itself
    # (single_msg by default) already handles a one-message transcript
    # body cleanly, so we don't swap it.
    inferred_source_kind = "video" if loaded.media_type in ("video", "videonote") else "chat"
    if inferred_source_kind == "video" and preset == "single_msg":
        # Tiny UX touch — surface the auto-detected reframing so users
        # know why the report looks like a video summary.
        console.print(f"[dim]Detected {loaded.media_type} — analyzing as a video transcript[/]")
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
        source_kind=inferred_source_kind,
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
        language=language,
        content_language=content_language,
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
    max_cost: float | None = None,
    post_saved: bool = False,
    dry_run: bool = False,
    cite_context: int = 0,
    self_check: bool = False,
    by: str | None = None,
    post_to: str | None = None,
    with_comments: bool = False,
    yes: bool = False,
    language: str = "en",
    content_language: str = "en",
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
        with_comments=with_comments,
        language=language,
        content_language=content_language,
    )

    # --dry-run: print the cost estimate and exit before any LLM call.
    # Useful before a `--enrich-all --full-history` run on a busy chat.
    if dry_run:
        loaded_preset = _load_preset_for_commands(preset, prompt_file, language=content_language)
        n = len(prepared.messages)
        if loaded_preset is None:
            console.print(f"[bold]{_tf('dry_run_unloadable', n=n, preset=preset)}[/]")
            return
        from analyzetg.analyzer.pipeline import estimate_cost as _estimate_cost

        lo, hi = _estimate_cost(
            n_messages=n,
            preset=loaded_preset,
            settings=get_settings(),
        )
        console.print(
            "[bold]"
            + _tf(
                "dry_run_summary",
                preset=preset,
                n=n,
                final=loaded_preset.final_model,
                fil=loaded_preset.filter_model,
            )
            + "[/]"
        )
        if hi is not None:
            console.print("  [bold]" + _tf("estimated_cost_band", lo=lo, hi=hi) + "[/]")
            console.print(f"  [dim]{_t('estimate_enrich_note')}[/]")
        else:
            console.print(f"  [yellow]{_t('estimate_unavailable')}[/]")
        return

    # Budget guard: refuse (or confirm) before any LLM call when the
    # estimated upper-bound cost exceeds the user's --max-cost. The
    # estimator only covers the analysis itself (map+reduce); enrichment
    # spend is not folded in — caveat is in the help text.
    if max_cost is not None and prepared.messages:
        loaded_preset = _load_preset_for_commands(preset, prompt_file, language=content_language)
        if loaded_preset is not None:
            from analyzetg.analyzer.pipeline import estimate_cost as _estimate_cost

            lo, hi = _estimate_cost(
                n_messages=len(prepared.messages),
                preset=loaded_preset,
                settings=get_settings(),
            )
            if hi is not None and hi > max_cost:
                console.print(
                    "[bold yellow]"
                    + _tf(
                        "max_cost_exceeded",
                        lo=lo,
                        hi=hi,
                        max=max_cost,
                        n=len(prepared.messages),
                        preset=preset,
                    )
                    + "[/]"
                )
                if yes:
                    console.print(f"[red]{_t('aborting_yes_set')}[/]")
                    raise typer.Exit(2)
                if not typer.confirm(_t("run_anyway_q"), default=False):
                    console.print(f"[yellow]{_t('aborted')}[/]")
                    raise typer.Exit(0)
            elif hi is None:
                console.print(f"[dim]{_t('max_cost_not_enforced')}[/]")

    console.print(f"[dim]{_t('running_analysis')}[/]")
    sender_id_arg = int(by) if by and by.lstrip("-").isdigit() else None
    sender_substring_arg = by if by and sender_id_arg is None else None
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
        sender_substring=sender_substring_arg,
        sender_id=sender_id_arg,
        with_comments=with_comments and prepared.comments_chat_id is not None,
        comments_chat_id=prepared.comments_chat_id,
    )
    # Build chat_groups when comments were included so the formatter
    # emits a per-chat header + link template per section. Each msg keeps
    # its original chat_id; rendering groups them automatically.
    chat_groups: dict[int, dict] | None = None
    if prepared.comments_chat_id is not None:
        from analyzetg.analyzer.formatter import build_link_template as _build_lt

        chat_groups = {
            chat_id: {
                "title": title or str(chat_id),
                "link_template": _build_lt(
                    chat_username=chat_username,
                    chat_internal_id=chat_internal_id,
                    thread_id=thread_id,
                ),
            },
            prepared.comments_chat_id: {
                "title": prepared.comments_chat_title or f"Comments {prepared.comments_chat_id}",
                "link_template": _build_lt(
                    chat_username=prepared.comments_chat_username,
                    chat_internal_id=prepared.comments_chat_internal_id,
                    thread_id=None,
                ),
            },
        }
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
        chat_groups=chat_groups,
        language=language,
        content_language=content_language,
    )

    if self_check and result.final_result and prepared.messages:
        verification = await _self_check(
            result=result,
            messages=prepared.messages,
            repo=repo,
            # LLM-facing → content_language. The heading below is user-facing
            # → language.
            content_language=content_language,
        )
        if verification:
            heading = _t("verification_heading", language)
            result.final_result = result.final_result.rstrip() + f"\n\n## {heading}\n\n" + verification

    if cite_context > 0 and result.final_result:
        result.final_result = await _expand_citations(
            result.final_result,
            chat_id=chat_id,
            repo=repo,
            context_n=cite_context,
            thread_id=thread_id,
            language=language,
        )

    # Persist a JSON-safe slice of the run's flags so the wizard's
    # "🔁 Repeat last run" entry can reconstruct them. Bytes-y / Path-y
    # things flatten to strings; runtime-only values (client, repo) are
    # explicitly omitted.
    if result.msg_count > 0:
        try:
            await repo.put_last_run_args(
                chat_id=chat_id,
                thread_id=int(thread_id or 0),
                args={
                    "preset": preset,
                    "since_ymd": since_dt.strftime("%Y-%m-%d") if since_dt else None,
                    "until_ymd": until_dt.strftime("%Y-%m-%d") if until_dt else None,
                    "from_msg_id": from_msg_id,
                    "full_history": bool(full_history),
                    "include_transcripts": bool(include_transcripts),
                    "min_msg_chars": min_msg_chars,
                    "model": model,
                    "filter_model": filter_model,
                    "no_cache": bool(no_cache),
                    "mark_read": bool(mark_read),
                    "post_to": post_to,
                    "post_saved": bool(post_saved),
                    "cite_context": cite_context,
                    "self_check": bool(self_check),
                    "by": by,
                    "with_comments": bool(with_comments),
                    "thread": int(thread_id) if thread_id else None,
                    "language": language,
                    "content_language": content_language,
                },
            )
        except Exception as e:
            log.warning("analyze.last_run_args_persist_failed", err=str(e)[:200])

    _print_and_write(
        result,
        output=output,
        title=title,
        thread_title=thread_title,
        console_out=console_out,
    )

    # `--post-saved` is sugar for `--post-to=me`. Explicit --post-to wins
    # if both are passed.
    post_target = post_to if post_to else ("me" if post_saved else None)
    if post_target and result.msg_count > 0:
        try:
            await _post_to_chat(client, repo, result, title=title, target=post_target)
        except Exception as e:
            log.warning("analyze.post_failed", chat_id=chat_id, target=post_target, err=str(e)[:200])
            console.print(f"[yellow]{_tf('couldnt_post_to', target=post_target, err=e)}[/]")

    if prepared.mark_read_fn and result.msg_count > 0:
        # Report is already on disk; a mark-read failure (permission denied,
        # network blip) should warn rather than crash with a traceback the
        # user can't act on.
        try:
            await prepared.mark_read_fn()
        except Exception as e:
            log.warning("analyze.mark_read_failed", chat_id=chat_id, err=str(e)[:200])
            console.print(f"[yellow]{_tf('couldnt_mark_read', err=e)}[/]")


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
    language: str = "en",
    content_language: str = "en",
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
            console.print(f"[red]{_tf('output_is_file_need_dir', path=output)}[/]")
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
        language=language,
        content_language=content_language,
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
                language=language,
                content_language=content_language,
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
            console.print(f"[red]{_tf('topic_failed', title=prepared.thread_title, err=e)}[/]")


async def _forum_pick_mode(client, chat_id: int, chat_title: str | None) -> tuple[bool, bool, int]:
    """Interactively pick a forum mode. Returns (all_flat, all_per_topic, thread_id)."""
    console.print(f"[dim]{_t('listing_forum_topics')}[/]")
    topics = await list_forum_topics(client, chat_id)
    if not topics:
        console.print(f"[yellow]{_t('no_topics_in_forum')}[/]")
        raise typer.Exit(0)

    if not sys.stdin.isatty():
        _print_topics_table(topics, with_unread=True)
        console.print(
            "\n[red]This is a forum — pick one of:[/]\n"
            "  --thread <id>       single topic\n"
            "  --all-per-topic     one analysis per topic\n"
            "  --all-flat          whole forum as one chat (defaults to per-topic unread)\n"
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
            console.print(f"[dim]{_t('aborted')}[/]")
            raise typer.Exit(0)
        if up == "A":
            return True, False, 0
        if up == "P":
            return False, True, 0
        if answer.isdigit():
            tid = int(answer)
            if any(t.topic_id == tid for t in topics):
                return False, False, tid
            console.print(f"[red]{_tf('no_topic_with_id', tid=tid)}[/]")
            continue
        console.print(f"[red]{_t('not_a_valid_choice')}[/]")


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
    mark_read: bool | None,
    no_cache: bool,
    include_transcripts: bool,
    min_msg_chars: int | None,
    enrich_opts: EnrichOpts | None = None,
    folder: str | None = None,
    yes: bool = False,
    language: str = "en",
    content_language: str = "en",
) -> None:
    """No <ref>: list dialogs with unread messages, confirm, analyze each.

    Delegates resolve/backfill/enrich to prepare_all_unread_runs and
    only adds the LLM stage + per-chat report write. Folder filtering
    flows through to the iterator unchanged.

    `mark_read=None` is the tri-state "not specified" — prompt the user
    once upfront so a batch of 30 chats doesn't silently stay unread.
    """
    from analyzetg.core.pipeline import prepare_all_unread_runs
    from analyzetg.enrich.base import EnrichOpts as _EnrichOpts

    effective_enrich = enrich_opts if enrich_opts is not None else _EnrichOpts()

    # Tri-state default: ask once (unless --yes was passed → keep unread).
    mark_read_effective: bool
    if mark_read is None:
        if yes or not sys.stdin.isatty():
            mark_read_effective = False
        else:
            mark_read_effective = typer.confirm(_t("mark_chats_read_after_analyze_q"), default=False)
    else:
        mark_read_effective = mark_read

    if console_out:
        out_dir: Path | None = None
    else:
        out_dir = _resolve_output_dir(output, 0) or Path("reports")
        out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    # Cross-chat synthesis preset: aggregate messages from all selected chats
    # into a single run_analysis call so the LLM sees them with `=== Chat: …
    # ===` separators (chat_groups mode) and can produce a one-shot summary
    # of "what was happening across these chats". For other presets the
    # batch flow stays per-chat (one report per chat).
    if preset == "multichat":
        await _run_multichat_batch(
            client=client,
            repo=repo,
            preset=preset,
            prompt_file=prompt_file,
            model=model,
            filter_model=filter_model,
            output=output,
            console_out=console_out,
            no_cache=no_cache,
            include_transcripts=include_transcripts,
            min_msg_chars=min_msg_chars,
            enrich_opts=effective_enrich,
            folder=folder,
            mark_read_effective=mark_read_effective,
            yes=yes,
            stamp=stamp,
            language=language,
            content_language=content_language,
        )
        return

    successes = 0
    failures: list[tuple[int, str | None, str]] = []

    async for prepared in prepare_all_unread_runs(
        client=client,
        repo=repo,
        settings=get_settings(),
        enrich_opts=effective_enrich,
        include_transcripts=include_transcripts,
        min_msg_chars=min_msg_chars,
        mark_read=mark_read_effective,
        folder=folder,
        yes=yes,
        language=language,
        content_language=content_language,
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
                language=language,
                content_language=content_language,
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
                try:
                    await prepared.mark_read_fn()
                except Exception as e:
                    # Report was already saved — a marking failure should warn,
                    # not crash the remainder of the batch.
                    log.warning(
                        "analyze.no_ref.mark_read_failed",
                        chat_id=prepared.chat_id,
                        err=str(e)[:200],
                    )
                    console.print(f"[yellow]{_tf('couldnt_mark_read', err=e)}[/]")
            successes += 1
        except Exception as e:
            log.error("analyze.no_ref.chat_error", chat_id=prepared.chat_id, err=str(e)[:200])
            console.print(f"[red]{_tf('batch_chat_failed', err=e)}[/]")
            failures.append((prepared.chat_id, prepared.chat_title, str(e)[:200]))

    total = successes + len(failures)
    if total == 0:
        return
    if failures:
        console.print(
            f"\n[bold]Batch complete:[/] {successes}/{total} chats succeeded, [red]{len(failures)} failed[/]."
        )
        for cid, ctitle, err in failures:
            console.print(f"  [red]×[/] {ctitle or cid}: {err}")
        raise typer.Exit(1)
    console.print(f"\n[bold green]Batch complete:[/] {successes}/{total} chats succeeded.")


async def _run_multichat_batch(
    *,
    client,
    repo: Repo,
    preset: str,
    prompt_file: Path | None,
    model: str | None,
    filter_model: str | None,
    output: Path | None,
    console_out: bool,
    no_cache: bool,
    include_transcripts: bool,
    min_msg_chars: int | None,
    enrich_opts: EnrichOpts,
    folder: str | None,
    mark_read_effective: bool,
    yes: bool,
    stamp: str,
    language: str,
    content_language: str,
) -> None:
    """Cross-chat batch flow: gather messages across all selected chats
    and run ONE analysis with `chat_groups` so the LLM sees per-chat
    sections separated by `=== Chat: … ===` headers.

    Used when `preset == "multichat"`. Mark-read fires per chat after the
    single LLM call succeeds, mirroring the per-chat behavior.
    """
    from analyzetg.analyzer.formatter import build_link_template as _build_lt
    from analyzetg.core.pipeline import prepare_all_unread_runs

    all_messages: list = []
    chat_groups: dict[int, dict] = {}
    mark_fns: list = []

    async for prepared in prepare_all_unread_runs(
        client=client,
        repo=repo,
        settings=get_settings(),
        enrich_opts=enrich_opts,
        include_transcripts=include_transcripts,
        min_msg_chars=min_msg_chars,
        mark_read=mark_read_effective,
        folder=folder,
        yes=yes,
        language=language,
        content_language=content_language,
    ):
        if not prepared.messages:
            continue
        all_messages.extend(prepared.messages)
        chat_groups[prepared.chat_id] = {
            "title": prepared.chat_title or str(prepared.chat_id),
            "link_template": _build_lt(
                chat_username=prepared.chat_username,
                chat_internal_id=prepared.chat_internal_id,
                thread_id=None,
            ),
        }
        if prepared.mark_read_fn is not None:
            mark_fns.append(prepared.mark_read_fn)

    if not all_messages:
        console.print(f"[dim]{_t('no_unread_across_chats')}[/]")
        return

    console.print(
        f"[dim]→ Cross-chat synthesis over {len(all_messages)} message(s) "
        f"from {len(chat_groups)} chat(s)...[/]"
    )
    opts = AnalysisOptions(
        preset=preset,
        prompt_file=prompt_file,
        model_override=model,
        filter_model_override=filter_model,
        use_cache=not no_cache,
        include_transcripts=include_transcripts,
        min_msg_chars=min_msg_chars,
        enrich=enrich_opts,
    )
    primary_chat_id = next(iter(chat_groups))
    primary_meta = chat_groups[primary_chat_id]
    result = await run_analysis(
        repo=repo,
        chat_id=primary_chat_id,
        thread_id=None,
        title=primary_meta.get("title"),
        opts=opts,
        chat_username=None,
        chat_internal_id=None,
        client=client,
        messages=all_messages,
        chat_groups=chat_groups,
        language=language,
        content_language=content_language,
    )

    if console_out:
        out_path: Path | None = None
    else:
        base = _resolve_output_dir(output, 0) or Path("reports")
        base.mkdir(parents=True, exist_ok=True)
        out_path = base / "multichat" / f"{preset}-{stamp}.md"
    _print_and_write(
        result,
        output=out_path,
        title=f"{len(chat_groups)} chats — multichat",
        console_out=console_out,
    )

    # Fire each chat's mark-read after the LLM call succeeded.
    for fn in mark_fns:
        try:
            await fn()
        except Exception as e:
            log.warning("analyze.multichat.mark_read_failed", err=str(e)[:200])


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

    (None, None) → localised "unread / full history" string. Concrete
    datetimes render as YYYY-MM-DD HH:MM, leaving ambiguity off the page.
    """
    if period is None or (period[0] is None and period[1] is None):
        return _t("report_meta_period_unread")
    a = period[0].strftime("%Y-%m-%d %H:%M") if period[0] else "…"
    b = period[1].strftime("%Y-%m-%d %H:%M") if period[1] else "…"
    return f"{a} → {b}"


def _render_report_header(result: AnalysisResult, *, title: str | None) -> str:
    """Build the fixed-format metadata block prepended to every saved report.

    Labels go through `i18n.t()` so the saved-file metadata follows the
    user's UI language (`locale.language`) — independent of the analysis
    body's language.
    """
    lines: list[str] = ["---"]
    lines.append(f"{_t('report_meta_chat')} {title or result.chat_id}")
    if result.thread_id:
        lines.append(f"{_t('report_meta_thread')} {result.thread_id}")
    lines.append(f"{_t('report_meta_period')} {_fmt_period_header(result.period)}")

    msg_line = f"{_t('report_meta_messages')} {result.msg_count}"
    if result.raw_msg_count and result.raw_msg_count != result.msg_count:
        dropped = result.raw_msg_count - result.msg_count
        msg_line += (
            " (" + _tf("report_meta_messages_filtered", raw=result.raw_msg_count, dropped=dropped) + ")"
        )
    lines.append(msg_line)

    preset_line = f"{_t('report_meta_preset')} `{result.preset}`"
    if result.prompt_version:
        preset_line += f" (v={result.prompt_version})"
    lines.append(preset_line)

    model_line = f"{_t('report_meta_model')} `{result.model}`"
    if result.chunk_count > 1 and result.filter_model and result.filter_model != result.model:
        model_line += f" (+ `{result.filter_model}` {_t('report_meta_model_map_phase')})"
    lines.append(model_line)

    if result.chunk_count:
        lines.append(f"{_t('report_meta_chunks')} {result.chunk_count}")

    total_calls = result.cache_hits + result.cache_misses
    if total_calls:
        lines.append(
            f"{_t('report_meta_cache')} "
            + _tf("report_meta_cache_hits_of", hits=result.cache_hits, total=total_calls)
        )

    if result.enrich_kinds:
        lines.append(f"{_t('report_meta_enrichment')} {', '.join(result.enrich_kinds)}")
    if result.enrich_summary:
        lines.append(f"{_t('report_meta_enrichment_detail')} {result.enrich_summary}")

    analysis_cost = result.total_cost_usd
    if result.enrich_cost_usd:
        total = analysis_cost + result.enrich_cost_usd
        lines.append(
            f"{_t('report_meta_cost')} {_fmt_cost_precise(total)} "
            f"(analysis {_fmt_cost_precise(analysis_cost)} + "
            f"enrichment {_fmt_cost_precise(result.enrich_cost_usd)})"
        )
    else:
        lines.append(f"{_t('report_meta_cost')} {_fmt_cost_precise(analysis_cost)}")

    lines.append(f"{_t('report_meta_generated')} {datetime.now().strftime('%Y-%m-%d %H:%M')}")

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
            console.print(f"[green]{_tf('also_saved', path=output)}[/]")
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
    console.print(f"[green]{_tf('written_to', path=output)}[/]")


# Telegram caps a single message at 4096 chars (UTF-16 code units, but
# 4096 chars is the practical safe ceiling for plain text).
_TG_MSG_LIMIT = 4000  # leave headroom for any wrapping markdown we add


async def _post_to_chat(
    client,
    repo,
    result,
    *,
    title: str | None,
    target: str,
) -> None:
    """Send the rendered analysis to a Telegram chat.

    `target == "me"` (or empty) routes to Saved Messages via
    `client.get_me()`. Anything else is resolved through `tg.resolver` —
    same parser the rest of the CLI uses, so usernames, t.me/ links,
    titles (fuzzy), and numeric ids all work.

    Splits on paragraph → line → hard-cut boundaries to stay under
    Telegram's per-message char limit. No parse_mode (plain text) so the
    LLM's stray `*`/`_` don't trip Telegram's markdown interpreter.
    """
    header = f"📊 analyzetg — {title or result.chat_id}\npreset: {result.preset}\n"
    body = header + "\n" + _with_truncation_banner(result)
    chunks = _split_for_telegram(body, _TG_MSG_LIMIT)

    target_norm = (target or "me").strip().lower()
    if target_norm in ("me", "saved", ""):
        entity = await client.get_me()
        label = "Saved Messages"
    else:
        from analyzetg.tg.resolver import resolve as _resolve

        resolved = await _resolve(client, repo, target)
        entity = resolved.chat_id
        label = resolved.title or str(resolved.chat_id)

    for chunk in chunks:
        await client.send_message(entity, chunk)
    console.print(f"[green]{_tf('posted_to_n_msgs', label=label, n=len(chunks))}[/]")


# Back-compat shim: existing callers expect `_post_to_saved_messages`.
async def _post_to_saved_messages(client, result, *, title: str | None) -> None:
    """Send the analysis to Saved Messages — thin wrapper over _post_to_chat.

    Kept so the existing `--post-saved` call site doesn't need plumbing
    repo through. The new `--post-to=…` flag goes through `_post_to_chat`
    directly with whatever target the user picked.
    """
    await _post_to_chat(client, None, result, title=title, target="me")


def _split_for_telegram(text: str, limit: int) -> list[str]:
    """Split text into ≤ limit-char chunks on the friendliest boundary."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    paragraphs = text.split("\n\n")
    buf = ""
    for para in paragraphs:
        candidate = (buf + "\n\n" + para) if buf else para
        if len(candidate) <= limit:
            buf = candidate
            continue
        # Flush the buffer; then the paragraph itself may need further split.
        if buf:
            out.append(buf)
            buf = ""
        if len(para) <= limit:
            buf = para
            continue
        # Paragraph alone is too long: split on lines.
        lines = para.split("\n")
        line_buf = ""
        for raw_line in lines:
            cand = (line_buf + "\n" + raw_line) if line_buf else raw_line
            if len(cand) <= limit:
                line_buf = cand
                continue
            if line_buf:
                out.append(line_buf)
            # If the line itself is too long, hard-cut.
            tail = raw_line
            while len(tail) > limit:
                out.append(tail[:limit])
                tail = tail[limit:]
            line_buf = tail
        if line_buf:
            buf = line_buf
    if buf:
        out.append(buf)
    return out


def _with_truncation_banner(result) -> str:
    if not getattr(result, "truncated", False):
        return result.final_result
    return _tf("truncation_banner_md", preset=result.preset) + result.final_result


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


async def run_all_unread_analyze(
    *,
    preset: str = "summary",
    prompt_file: Path | None = None,
    model: str | None = None,
    filter_model: str | None = None,
    output: Path | None = None,
    console_out: bool = False,
    mark_read: bool | None = False,
    no_cache: bool = False,
    include_transcripts: bool = True,
    min_msg_chars: int | None = None,
    enrich_opts: EnrichOpts | None = None,
    folder: str | None = None,
    yes: bool = False,
    language: str | None = None,
    content_language: str | None = None,
) -> None:
    """Public: run the batch-across-all-unread-chats flow (was the old no-ref default).

    Pass `folder="Alpha"` (or any case-insensitive substring of a folder title)
    to restrict the batch to chats in that Telegram folder.
    `yes=True` skips the interactive confirmation — the wizard sets this so
    the user isn't asked twice after already approving the plan."""
    settings = get_settings()
    lang = (language or settings.locale.language or "en").lower()
    clang = (content_language or settings.locale.content_language or lang).lower()
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
            language=lang,
            content_language=clang,
        )


async def cmd_stats(since: str | None, by: str) -> None:
    settings = get_settings()
    since_dt = _parse_ymd(since)
    async with open_repo(settings.storage.data_path) as repo:
        rows = await repo.stats_by(group_by=by, since=since_dt)
        hit_rate = await repo.cache_hit_rate(since=since_dt)
        total_cost = sum(float(r["cost_usd"] or 0) for r in rows)
        total_unpriced = sum(int(r.get("unpriced_calls") or 0) for r in rows)

        t = Table(title=f"Usage (by {by}){' since ' + since if since else ''}")
        cols = ("bucket", "calls", "prompt", "cached", "completion", "audio_s", "cost_usd")
        for c in cols:
            t.add_column(c)
        for r in rows:
            # Mark buckets where some calls weren't priced — user knows the
            # `cost_usd` column under-reports for that row.
            unpriced = int(r.get("unpriced_calls") or 0)
            calls_cell = str(r["calls"])
            if unpriced:
                calls_cell = f"{r['calls']} ([yellow]{unpriced} unpriced[/])"
            t.add_row(
                str(r["bucket"]) if r["bucket"] is not None else "-",
                calls_cell,
                str(r["prompt_tokens"] or 0),
                str(r["cached_tokens"] or 0),
                str(r["completion_tokens"] or 0),
                str(r["audio_seconds"] or 0),
                f"${float(r['cost_usd'] or 0):.4f}",
            )
        console.print(t)
        console.print(f"[bold]{_tf('stats_total_cost', cost=total_cost)}[/]")
        if total_unpriced:
            console.print(
                f"[yellow]⚠ {total_unpriced} call(s) had no pricing entry — total cost under-reports.[/] "
                "Add the model to [cyan][pricing.chat][/] or [cyan][pricing.audio][/] in config.toml."
            )
        console.print(f"[bold]{_tf('stats_hit_rate', rate=hit_rate)}[/]")
