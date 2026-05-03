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
    max_cost: float | None = None,
    yes: bool = False,
    language: str | None = None,
    content_language: str | None = None,
    no_followup: bool = False,
    semantic: bool = False,
    build_index: bool = False,
    rerank: bool | None = None,
    limit: int = 200,
    show_retrieved: bool = False,
) -> None:
    """Fetch a YouTube transcript and ask a question over it."""
    from unread.db.repo import open_repo
    from unread.youtube.commands import _restore_metadata_from_row
    from unread.youtube.metadata import fetch_metadata
    from unread.youtube.transcript import get_transcript
    from unread.youtube.urls import extract_video_id

    settings = get_settings()
    video_id = extract_video_id(ref)
    if not video_id:
        console.print(f"[red]Could not extract a YouTube video id from: {ref}[/]")
        raise typer.Exit(2)

    async with open_repo(settings.storage.data_path) as repo:
        cached = await repo.get_youtube_video(video_id)
        if cached:
            meta = _restore_metadata_from_row(cached)
        else:
            meta = await fetch_metadata(video_id)
        tres = await get_transcript(meta, settings=settings, repo=repo)

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
    used_question = question or _prompt_question(source_label)
    await cmd_ask_document(
        extracted_text=text,
        citations=citations,
        source_label=source_label,
        source_id=source_id,
        content_hash=content_hash,
        question=used_question,
        model=model,
        output=output,
        console_out=console_out,
        max_cost=max_cost,
        yes=yes,
        language=language,
        content_language=content_language,
        no_followup=no_followup,
        semantic=semantic,
        build_index=build_index,
        rerank=rerank,
        limit=limit,
        show_retrieved=show_retrieved,
    )
