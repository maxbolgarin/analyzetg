"""Ask-over-YouTube adapter. Wraps unread.youtube.transcript."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from unread.ask.sources.core import DocCitation, cmd_ask_document
from unread.ask.sources.file import _prompt_question  # reuse the same TTY-prompt helper
from unread.config import get_settings

console = Console()


async def cmd_ask_youtube(
    ref: str,
    question: str | None,
    *,
    model: str | None = None,
    output: Path | None = None,
    console_out: bool = False,
    no_console: bool = False,
    no_save: bool = False,
    max_cost: float | None = None,
    yes: bool = False,
    language: str | None = None,
    report_language: str | None = None,
    source_language: str | None = None,
    no_followup: bool = False,
    semantic: bool = False,
    build_index: bool = False,
    rerank: bool | None = None,
    limit: int = 200,
    show_retrieved: bool = False,
    youtube_source: str = "auto",
) -> None:
    """Fetch a YouTube transcript and ask a question over it.

    Mirrors `cmd_analyze_youtube`'s pre-LLM flow: render the metadata
    panel, run the interactive captions-vs-Whisper picker on a TTY when
    `youtube_source="auto"`, log the transcript-ready status. Without
    this the user typed a question and waited blindly while Whisper
    transcoded a multi-megabyte audio stream — even though the video
    had captions readily available.
    """
    from unread.db.repo import open_repo
    from unread.util.pricing import audio_cost
    from unread.youtube.commands import (
        _interactive_pick_source,
        _is_interactive,
        _render_metadata_panel,
        _restore_metadata_from_row,
    )
    from unread.youtube.metadata import fetch_metadata
    from unread.youtube.transcript import (
        NoTranscriptAvailable,
        TranscriptSource,
        YoutubeFetchError,
        get_transcript,
    )
    from unread.youtube.urls import extract_video_id

    settings = get_settings()
    video_id = extract_video_id(ref)
    if not video_id:
        console.print(f"[red]Could not extract a YouTube video id from: {ref}[/]")
        raise typer.Exit(2)

    # Forced-Whisper path needs ffmpeg up front; bail with a friendly
    # message instead of dying mid-pipeline. Mirrors analyze.
    if youtube_source == "audio":
        from unread.util.preflight import require_ffmpeg

        require_ffmpeg("download and transcribe YouTube audio")

    async with open_repo(settings.storage.data_path) as repo:
        cached = await repo.get_youtube_video(video_id)
        if cached and cached.get("transcript"):
            # Cached transcript path — read text directly from the row,
            # do NOT re-call get_transcript (which would re-download
            # captions / re-transcode audio even though we already have
            # the text). Mirrors `cmd_analyze_youtube`'s cached branch.
            console.print(f"[grey70]Using cached YouTube metadata + transcript ({video_id})[/]")
            meta = _restore_metadata_from_row(cached)
            console.print(_render_metadata_panel(meta, audio_estimate=0.0))
            text = (cached.get("transcript") or "").strip()
            cached_source = cached.get("transcript_source") or "captions"
            console.print(f"[green]Transcript ready[/] ({cached_source}, {len(text):,} chars, cached)")
        else:
            if cached:
                meta = _restore_metadata_from_row(cached)
            else:
                console.print(f"[grey70]Fetching YouTube metadata for {video_id}…[/]")
                try:
                    meta = await fetch_metadata(video_id)
                except YoutubeFetchError as e:
                    console.print(f"[red]YouTube fetch failed: {str(e)[:300]}[/]")
                    raise typer.Exit(1) from e

            audio_estimate = float(audio_cost(settings.openai.audio_model_default, meta.duration_sec) or 0.0)
            console.print(_render_metadata_panel(meta, audio_estimate=audio_estimate))

            effective_source: TranscriptSource = youtube_source  # type: ignore[assignment]
            if youtube_source == "auto" and not yes and _is_interactive():
                picked = await _interactive_pick_source(meta, audio_estimate=audio_estimate)
                if picked is None:
                    console.print("[yellow]Cancelled.[/]")
                    raise typer.Exit(0)
                effective_source = picked

            try:
                tres = await get_transcript(meta, source=effective_source, settings=settings, repo=repo)
            except NoTranscriptAvailable as e:
                raise typer.BadParameter(str(e)) from e
            except YoutubeFetchError as e:
                console.print(f"[red]YouTube fetch failed: {str(e)[:300]}[/]")
                raise typer.Exit(1) from e
            cost_str = f", ${tres.cost_usd:.4f}" if tres.cost_usd > 0 else ""
            console.print(
                f"[green]Transcript ready[/] ({tres.source}, {len(tres.text or ''):,} chars{cost_str})"
            )
            text = (tres.text or "").strip()

    if not text:
        console.print("[red]Transcript is empty — nothing to answer over.[/]")
        raise typer.Exit(2)

    source_label = f"YouTube · {meta.title or video_id}"
    source_id = f"yt:{video_id}"
    content_hash = f"yt:{video_id}:{len(text)}"
    citations = [
        DocCitation(
            uri=f"https://youtu.be/{video_id}",
            label=meta.title or video_id,
            offset_start=0,
            offset_end=len(text),
        )
    ]
    used_question = question if question else await _prompt_question(source_label)
    await cmd_ask_document(
        extracted_text=text,
        citations=citations,
        source_label=source_label,
        source_id=source_id,
        source_kind="youtube",
        content_hash=content_hash,
        question=used_question,
        model=model,
        output=output,
        console_out=console_out,
        no_console=no_console,
        no_save=no_save,
        max_cost=max_cost,
        yes=yes,
        language=language,
        report_language=report_language,
        source_language=source_language,
        no_followup=no_followup,
        semantic=semantic,
        build_index=build_index,
        rerank=rerank,
        limit=limit,
        show_retrieved=show_retrieved,
    )
