"""Ask-over-website adapter. Wraps unread.website.content."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from unread.ask.sources.core import DocCitation, cmd_ask_document
from unread.ask.sources.file import _prompt_question
from unread.config import get_settings

console = Console()


async def cmd_ask_website(
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
    """Fetch a webpage's text and ask a question over it."""
    from unread.website.content import fetch_page

    settings = get_settings()
    page = await fetch_page(ref, settings=settings)

    text = "\n\n".join(page.paragraphs).strip()
    if not text:
        console.print(f"[red]Could not extract readable text from: {ref}[/]")
        raise typer.Exit(2)

    source_label = page.metadata.title or ref
    source_id = f"web:{page.metadata.normalized_url}"
    content_hash = page.content_hash
    citations = [
        DocCitation(
            uri=ref,
            label=source_label,
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
