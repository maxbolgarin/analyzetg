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
) -> None:
    """Fetch a webpage's text and ask a question over it."""
    from unread.website.commands import _render_panel
    from unread.website.content import fetch_page

    settings = get_settings()
    page = await fetch_page(ref, settings=settings)
    # Print the page-metadata panel BEFORE any LLM-call activity, mirroring
    # `cmd_analyze_website` (`unread/website/commands.py:342`). Skip it in
    # --no-console mode since the saved file doesn't include it anyway.
    if not no_console:
        console.print(_render_panel(page))

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
    used_question = question if question else await _prompt_question(source_label)
    await cmd_ask_document(
        extracted_text=text,
        citations=citations,
        source_label=source_label,
        source_id=source_id,
        source_kind="website",
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
