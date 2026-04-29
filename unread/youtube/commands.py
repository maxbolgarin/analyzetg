"""Top-level handler for `unread analyze <youtube-url>`.

Mirrors the post-`prepare_chat_run` half of `_run_single` (no Telegram
backfill, no mark_read). Splits the transcript into multiple synthetic
`Message` rows so the existing chunker / map-reduce flow can summarize
long videos without hitting `formatter._BODY_CAP` (4000 chars/msg).
"""

from __future__ import annotations

import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from unread.analyzer.pipeline import (
    AnalysisOptions,
    estimate_cost,
    run_analysis,
)
from unread.config import get_settings
from unread.db.repo import open_repo
from unread.i18n import t as _t
from unread.i18n import tf as _tf
from unread.models import Message
from unread.util.logging import get_logger
from unread.util.pricing import audio_cost
from unread.youtube.metadata import YoutubeMetadata, fetch_metadata
from unread.youtube.paths import youtube_report_path
from unread.youtube.transcript import (
    NoTranscriptAvailable,
    TranscriptSource,
    YoutubeFetchError,
    get_transcript,
)
from unread.youtube.urls import extract_video_id

console = Console()
log = get_logger(__name__)


# Each synthetic message body must stay below `formatter._BODY_CAP` (4000)
# or the formatter will truncate with `…`. 3500 leaves headroom for any
# label additions and keeps cue-aligned splits readable.
_SEGMENT_CHARS = 3500
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def _parse_upload_date(yyyymmdd: str | None) -> datetime:
    """yt-dlp gives `upload_date` as YYYYMMDD; default to now() on miss."""
    if yyyymmdd and len(yyyymmdd) == 8 and yyyymmdd.isdigit():
        try:
            return datetime.strptime(yyyymmdd, "%Y%m%d").replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _meta_header(meta: YoutubeMetadata) -> str:
    """Compact metadata block prepended as the first synthetic message."""
    bits: list[str] = [f"YouTube video: {meta.title or meta.video_id}"]
    if meta.channel_title:
        bits.append(f"Channel: {meta.channel_title}")
    if meta.upload_date:
        bits.append(f"Uploaded: {meta.upload_date}")
    if meta.duration_sec:
        bits.append(f"Duration: {_fmt_hms(meta.duration_sec)}")
    if meta.view_count is not None:
        bits.append(f"Views: {meta.view_count:,}")
    if meta.like_count is not None:
        bits.append(f"Likes: {meta.like_count:,}")
    bits.append(f"URL: {meta.url}")
    if meta.description:
        desc = meta.description.strip()
        # Cap at ~1500 chars so the header itself fits comfortably under
        # _BODY_CAP even with a verbose description; long descriptions
        # rarely add detail the transcript doesn't.
        if len(desc) > 1500:
            desc = desc[:1500].rstrip() + "…"
        bits.append("")
        bits.append("Description:")
        bits.append(desc)
    return "\n".join(bits)


def _fmt_hms(seconds: int | None) -> str:
    """Format seconds as `HH:MM:SS` (or `MM:SS` when under an hour)."""
    sec = max(0, int(seconds or 0))
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _segment_transcript(text: str, *, max_chars: int = _SEGMENT_CHARS) -> list[str]:
    """Split transcript into ≤max_chars chunks, preferring sentence boundaries."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    out: list[str] = []
    sentences = _SENTENCE_END.split(text)
    buf = ""
    for sent in sentences:
        s = sent.strip()
        if not s:
            continue
        candidate = (buf + " " + s) if buf else s
        if len(candidate) <= max_chars:
            buf = candidate
            continue
        if buf:
            out.append(buf)
            buf = ""
        if len(s) <= max_chars:
            buf = s
            continue
        # Sentence itself longer than budget — hard cut.
        tail = s
        while len(tail) > max_chars:
            out.append(tail[:max_chars])
            tail = tail[max_chars:]
        buf = tail
    if buf:
        out.append(buf)
    return out


def _segment_timed_cues(
    cues: list[tuple[int, str]],
    *,
    max_chars: int = _SEGMENT_CHARS,
) -> list[tuple[int, str]]:
    """Pack (start_sec, text) cues into segments of ≤max_chars chars.

    Each output entry's `start_sec` is the start of the FIRST cue in
    that segment — so a citation `[#start_sec]` lands the user at the
    moment the segment's content begins, not somewhere in the middle.
    """
    out: list[tuple[int, str]] = []
    cur_start: int | None = None
    cur_lines: list[str] = []
    cur_chars = 0
    for start, line in cues:
        prefix_chars = len(line) + 1  # +1 for join separator
        if cur_start is None:
            cur_start = start
            cur_lines = [line]
            cur_chars = len(line)
            continue
        if cur_chars + prefix_chars > max_chars:
            out.append((cur_start, " ".join(cur_lines)))
            cur_start = start
            cur_lines = [line]
            cur_chars = len(line)
        else:
            cur_lines.append(line)
            cur_chars += prefix_chars
    if cur_start is not None and cur_lines:
        out.append((cur_start, " ".join(cur_lines)))
    return out


def _build_synthetic_messages(
    meta: YoutubeMetadata,
    transcript_text: str,
    *,
    timed_cues: list[tuple[int, str]] | None = None,
) -> list[Message]:
    """Header + per-segment `Message` list keyed off `chat_id=0`.

    msg_id strategy: the metadata header is `msg_id=0` (so a citation to
    `#0` is a clear "header marker, not the speaker"). Transcript segments
    use `msg_id = max(prev+1, start_sec)` so each msg_id is the second-
    offset of the segment's first cue — citations like `[#754]` resolve to
    `?t=754s` via the link template override. When timed cues are not
    available (Whisper path), offsets get spread uniformly across
    `meta.duration_sec`.
    """
    upload_dt = _parse_upload_date(meta.upload_date)
    duration = max(1, int(meta.duration_sec or 0))
    sender = meta.channel_title or "YouTube"

    msgs: list[Message] = [
        Message(
            chat_id=0,
            msg_id=0,
            date=upload_dt,
            sender_name=sender,
            text=_meta_header(meta),
        )
    ]

    if timed_cues:
        timed_segments = _segment_timed_cues(timed_cues)
    else:
        plain_segments = _segment_transcript(transcript_text)
        n = max(1, len(plain_segments))
        timed_segments = [(int((i / n) * duration), seg) for i, seg in enumerate(plain_segments)]

    prev_id = 0
    for start_sec, seg in timed_segments:
        # Enforce strictly-increasing msg_ids — two short cues at the same
        # second would otherwise collide.
        msg_id = max(prev_id + 1, int(start_sec))
        prev_id = msg_id
        body = f"[{_fmt_hms(start_sec)}] {seg}"
        msgs.append(
            Message(
                chat_id=0,
                msg_id=msg_id,
                date=upload_dt + timedelta(seconds=int(start_sec)),
                sender_name=sender,
                text=body,
            )
        )
    return msgs


def _has_any_captions(meta: YoutubeMetadata) -> bool:
    return bool(meta.subtitles or meta.automatic_captions)


def _render_metadata_panel(meta: YoutubeMetadata, *, audio_estimate: float) -> Panel:
    """Pretty-print metadata + caption availability + Whisper estimate."""
    rows: list[str] = []
    if meta.channel_title:
        rows.append(f"[bold]Channel[/] {meta.channel_title}")
    rows.append(f"[bold]Title[/]   {meta.title or meta.video_id}")
    if meta.duration_sec:
        rows.append(f"[bold]Duration[/] {_fmt_hms(meta.duration_sec)}")
    if meta.upload_date:
        rows.append(f"[bold]Uploaded[/] {meta.upload_date}")
    if meta.view_count is not None:
        rows.append(f"[bold]Views[/]    {meta.view_count:,}")
    if meta.like_count is not None:
        rows.append(f"[bold]Likes[/]    {meta.like_count:,}")
    captions = _has_any_captions(meta)
    cap_label = "[green]available[/]" if captions else "[yellow]none[/] (Whisper required)"
    rows.append(f"[bold]Captions[/] {cap_label}")
    if audio_estimate > 0:
        rows.append(f"[bold]Whisper estimate[/] ~${audio_estimate:.4f}")
    rows.append(f"[bold]URL[/]      {meta.url}")
    if meta.description:
        desc = meta.description.strip().splitlines()[0][:200]
        rows.append("")
        rows.append(f"[grey70]{desc}…[/]" if len(meta.description) > 200 else f"[grey70]{desc}[/]")
    return Panel("\n".join(rows), title="YouTube video", border_style="cyan")


def _is_interactive() -> bool:
    """True if stdin is an interactive terminal (not piped / non-tty)."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, OSError):
        return False


async def _interactive_pick_source(
    meta: YoutubeMetadata,
    *,
    audio_estimate: float,
) -> TranscriptSource | None:
    """Prompt the user to confirm + pick a transcript source.

    Returns the chosen TranscriptSource ("auto" / "captions" / "audio")
    or `None` to signal cancel.
    """
    from unread.util.prompt import Choice
    from unread.util.prompt import select as _select
    from unread.util.prompt import separator as _sep

    has_captions = _has_any_captions(meta)
    audio_label = f"Audio + Whisper — ~${audio_estimate:.4f}" if audio_estimate > 0 else "Audio + Whisper"
    choices: list = [
        Choice(value="auto", label="Auto — captions if available, otherwise Whisper (recommended)"),
    ]
    if has_captions:
        choices.append(Choice(value="captions", label="Captions only — free, fast"))
    choices.append(Choice(value="audio", label=audio_label))
    choices.append(_sep())
    choices.append(Choice(value="__cancel__", label="Cancel"))

    answer = _select(
        "Continue analysis? Pick the transcript source:",
        choices=choices,
        default_value="auto",
    )
    if answer is None or answer == "__cancel__":
        return None
    return answer


def _restore_metadata_from_row(row: dict) -> YoutubeMetadata:
    """Rebuild YoutubeMetadata from a `youtube_videos` cache row."""
    import json

    tags_raw = row.get("tags")
    try:
        tags = list(json.loads(tags_raw)) if tags_raw else None
    except (TypeError, ValueError):
        tags = None
    return YoutubeMetadata(
        video_id=row["video_id"],
        url=row["url"],
        title=row.get("title"),
        channel_id=row.get("channel_id"),
        channel_title=row.get("channel_title"),
        channel_url=row.get("channel_url"),
        description=row.get("description"),
        upload_date=row.get("upload_date"),
        duration_sec=row.get("duration_sec"),
        view_count=row.get("view_count"),
        like_count=row.get("like_count"),
        tags=tags,
        language=row.get("language"),
        subtitles=None,
        automatic_captions=None,
    )


async def cmd_analyze_youtube(
    *,
    url: str,
    preset: str | None,
    prompt_file: Path | None,
    model: str | None,
    filter_model: str | None,
    output: Path | None,
    console_out: bool,
    no_cache: bool = False,
    max_cost: float | None = None,
    dry_run: bool = False,
    self_check: bool = False,
    cite_context: int = 0,
    post_to: str | None = None,
    post_saved: bool = False,
    language: str = "en",
    content_language: str = "en",
    youtube_source: TranscriptSource = "auto",
    yes: bool = False,
) -> None:
    """Analyze one YouTube video. Captions-first, Whisper fallback."""
    from unread.analyzer.commands import (
        _load_preset_for_commands,
        _post_to_chat,
        _print_and_write,
        _self_check,
    )

    settings = get_settings()
    try:
        video_id = extract_video_id(url)
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e

    # `youtube_source="audio"` forces Whisper transcription, which needs
    # ffmpeg to extract / transcode the audio. Catch the missing-binary
    # case here instead of mid-pipeline after metadata fetch. ("auto"
    # only uses ffmpeg if captions are unavailable; we let that fail
    # naturally with the existing transcript-side error so a captions-
    # only run on a machine without ffmpeg still works.)
    if youtube_source == "audio":
        from unread.util.preflight import require_ffmpeg

        require_ffmpeg("download and transcribe YouTube audio")

    # YouTube videos default to the `video` preset (system prompt tuned
    # for transcripts, time-stamped citations, no chat semantics). User
    # `--preset summary` etc. still wins.
    effective_preset = preset or "video"

    async with open_repo(settings.storage.data_path) as repo:
        cached = None if no_cache else await repo.get_youtube_video(video_id)
        timed_cues: list[tuple[int, str]] | None = None
        if cached and cached.get("transcript"):
            console.print(f"[grey70]Using cached YouTube metadata + transcript ({video_id})[/]")
            metadata = _restore_metadata_from_row(cached)
            transcript_text = cached["transcript"] or ""
            transcript_source: str = cached.get("transcript_source") or "captions"
            transcript_cost = float(cached.get("transcript_cost_usd") or 0.0)
            transcript_lang = cached.get("language")
            timed_raw = cached.get("transcript_timed_json")
            if timed_raw:
                try:
                    import json as _json

                    timed_cues = [(int(s), str(t)) for s, t in _json.loads(timed_raw)]
                except (TypeError, ValueError):
                    timed_cues = None
        else:
            console.print(f"[grey70]Fetching YouTube metadata for {video_id}…[/]")
            try:
                metadata = await fetch_metadata(video_id)
            except YoutubeFetchError as e:
                # yt-dlp couldn't reach the video at all — friendly banner
                # plus an upgrade hint, since this is the most common
                # symptom of yt-dlp lagging behind a YouTube format change.
                console.print(f"[red]{_t('youtube_fetch_failed').format(err=str(e)[:300])}[/]")
                console.print(f"[grey70]{_t('youtube_fetch_failed_hint')}[/]")
                raise typer.Exit(1) from e

            audio_estimate = float(
                audio_cost(settings.openai.audio_model_default, metadata.duration_sec) or 0.0
            )
            console.print(_render_metadata_panel(metadata, audio_estimate=audio_estimate))

            # Interactive picker only when:
            #   - stdin is a TTY (not piped),
            #   - --yes wasn't passed (scripted runs skip prompts), and
            #   - --youtube-source was left at the default "auto".
            # Explicit `--youtube-source captions|audio` is honoured as-is.
            effective_source: TranscriptSource = youtube_source
            if youtube_source == "auto" and not yes and _is_interactive():
                picked = await _interactive_pick_source(metadata, audio_estimate=audio_estimate)
                if picked is None:
                    console.print("[yellow]Cancelled.[/]")
                    raise typer.Exit(0)
                effective_source = picked

            try:
                tres = await get_transcript(
                    metadata,
                    source=effective_source,
                    settings=settings,
                    repo=repo,
                )
            except NoTranscriptAvailable as e:
                raise typer.BadParameter(str(e)) from e
            except YoutubeFetchError as e:
                console.print(f"[red]{_t('youtube_fetch_failed').format(err=str(e)[:300])}[/]")
                console.print(f"[grey70]{_t('youtube_fetch_failed_hint')}[/]")
                raise typer.Exit(1) from e
            transcript_text = tres.text
            transcript_source = tres.source
            transcript_cost = tres.cost_usd
            transcript_lang = tres.language
            timed_cues = tres.timed_cues

            cost_str = f", ${transcript_cost:.4f}" if transcript_cost > 0 else ""
            console.print(
                f"[green]Transcript ready[/] ({tres.source}, {len(transcript_text):,} chars{cost_str})"
            )

            await repo.put_youtube_video(
                video_id=video_id,
                url=metadata.url,
                title=metadata.title,
                channel_id=metadata.channel_id,
                channel_title=metadata.channel_title,
                channel_url=metadata.channel_url,
                description=metadata.description,
                upload_date=metadata.upload_date,
                duration_sec=metadata.duration_sec,
                view_count=metadata.view_count,
                like_count=metadata.like_count,
                tags=metadata.tags,
                language=transcript_lang,
                transcript=transcript_text,
                transcript_source=transcript_source,
                transcript_model=(
                    settings.openai.audio_model_default if transcript_source == "audio" else None
                ),
                transcript_cost_usd=transcript_cost,
                transcript_timed=timed_cues,
            )

        if not transcript_text.strip():
            console.print("[red]Empty transcript — nothing to analyze.[/]")
            raise typer.Exit(2)

        messages = _build_synthetic_messages(metadata, transcript_text, timed_cues=timed_cues)
        loaded_preset = _load_preset_for_commands(effective_preset, prompt_file, language=content_language)

        if dry_run:
            n = len(messages)
            if loaded_preset is None:
                console.print(f"[bold]Dry run: {n} synthetic msgs / preset={effective_preset}[/]")
                return
            lo, hi = estimate_cost(
                n_messages=n,
                preset=loaded_preset,
                settings=settings,
            )
            console.print(
                f"[bold]Dry run: video={video_id} "
                f"chars={len(transcript_text):,} "
                f"segments={n} preset={effective_preset} "
                f"final={loaded_preset.final_model} filter={loaded_preset.filter_model}[/]"
            )
            if hi is not None:
                analysis_hi = hi + transcript_cost
                console.print(
                    f"  Estimated cost: ${(lo or 0.0) + transcript_cost:.4f} – "
                    f"${analysis_hi:.4f} "
                    f"(transcript ${transcript_cost:.4f} + analysis ${lo or 0:.4f}–${hi:.4f})"
                )
            else:
                console.print("  [yellow]Cost estimate unavailable (missing pricing entry)[/]")
            return

        if max_cost is not None and loaded_preset is not None:
            lo, hi = estimate_cost(
                n_messages=len(messages),
                preset=loaded_preset,
                settings=settings,
            )
            if hi is not None:
                upper = hi + transcript_cost
                if upper > max_cost:
                    console.print(
                        f"[bold yellow]Estimated upper-bound cost ${upper:.4f} "
                        f"exceeds --max-cost ${max_cost:.4f} "
                        f"(transcript ${transcript_cost:.4f} + analysis ≤ ${hi:.4f})[/]"
                    )
                    if yes:
                        console.print("[red]Aborting (--yes set).[/]")
                        raise typer.Exit(2)
                    from unread.util.prompt import confirm as _confirm

                    if not _confirm("Run anyway?", default=False):
                        console.print("[yellow]Aborted.[/]")
                        raise typer.Exit(0)

        opts = AnalysisOptions(
            preset=effective_preset,
            prompt_file=prompt_file,
            model_override=model,
            filter_model_override=filter_model,
            use_cache=not no_cache,
            include_transcripts=True,
            min_msg_chars=0,  # synthetic header may be short; never drop it
            youtube_video_id=video_id,
            source_kind="video",
        )

        # Citations like `[#754]` resolve through `?t=754s`, so a click on
        # any citation in the report jumps straight to that moment in the
        # video. Subbed in by the formatter via `link_template`.
        link_template = f"https://www.youtube.com/watch?v={video_id}&t={{msg_id}}s"

        console.print(f"[grey70]{_t('running_analysis')}[/]")
        result = await run_analysis(
            repo=repo,
            chat_id=0,
            thread_id=None,
            title=metadata.title or video_id,
            opts=opts,
            messages=messages,
            language=language,
            content_language=content_language,
            link_template_override=link_template,
        )

        # Reflect transcript cost in the totals shown to the user. The
        # underlying analysis_cache rows are unaffected — they only hold
        # LLM-side cost.
        if transcript_cost:
            result.total_cost_usd += transcript_cost
            result.enrich_cost_usd += transcript_cost
            result.enrich_kinds = list({*result.enrich_kinds, transcript_source})

        if self_check and result.final_result and messages:
            verification = await _self_check(
                result=result,
                messages=messages,
                repo=repo,
                content_language=content_language,
            )
            if verification:
                heading = _t("verification_heading", language)
                result.final_result = result.final_result.rstrip() + f"\n\n## {heading}\n\n" + verification

        if cite_context > 0 and result.final_result:
            # No Telegram chat → citations have no surrounding context to
            # expand against. Skip silently rather than emitting an empty
            # Sources section.
            log.info("youtube.cite_context_skipped", reason="no telegram chat")

        # Compute output path: explicit --output wins; else a youtube/<channel>/...
        # report file — never the chat-shaped default path.
        if output is None and not console_out:
            output_path: Path | None = youtube_report_path(
                video_id=video_id,
                title=metadata.title,
                channel_title=metadata.channel_title,
                channel_id=metadata.channel_id,
                preset=effective_preset,
            )
        else:
            output_path = output

        _print_and_write(
            result,
            output=output_path,
            title=metadata.title or video_id,
            console_out=console_out,
        )

        post_target = post_to if post_to else ("me" if post_saved else None)
        if post_target and result.msg_count > 0:
            from unread.tg.client import tg_client

            try:
                async with tg_client(settings) as client:
                    await _post_to_chat(
                        client,
                        repo,
                        result,
                        title=metadata.title or video_id,
                        target=post_target,
                    )
            except Exception as e:
                log.warning("youtube.post_failed", target=post_target, err=str(e)[:200])
                console.print(f"[yellow]{_tf('couldnt_post_to', target=post_target, err=e)}[/]")
