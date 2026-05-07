"""Ask-over-local-file adapter. Wraps unread.files extractors."""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console

from unread.ask.sources.core import DocCitation, cmd_ask_document

console = Console()


def _is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


async def _prompt_question(source_label: str) -> str:
    """Inline single-line question prompt with prompt_toolkit line editing.

    Uses `prompt_toolkit.PromptSession` (same pattern as the follow-up
    loop in `unread/ask/commands.py`) so Esc / arrow keys / Backspace
    are handled instead of leaking literal `^[^[^[` into the buffer
    like raw `input()` does. Esc / Ctrl-C / Ctrl-D abort the prompt
    with exit code 130. Empty submission also aborts — the doc-mode
    ask paths require a real question to send to the LLM.

    The styled prefix ("Question for <source>:") is fed straight into
    `prompt_async` instead of being printed via Rich beforehand.
    prompt_toolkit owns the line and redraws on every keypress, so a
    pre-printed Rich prefix would get overlapped by the user's input
    on the next redraw.
    """
    if not _is_tty():
        console.print("[red]ask requires a question for non-Telegram refs.[/]")
        raise typer.Exit(2)

    from html import escape as _html_escape

    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    @kb.add("c-d")
    def _abort(event):
        event.app.exit(exception=KeyboardInterrupt())

    safe_label = _html_escape(source_label)
    prefix = HTML(f"<b>Question for</b> <ansicyan>{safe_label}</ansicyan><b>:</b> ")
    session: PromptSession = PromptSession()
    try:
        text = await session.prompt_async(prefix, key_bindings=kb)
    except (EOFError, KeyboardInterrupt):
        console.print()
        raise typer.Exit(130) from None
    text = (text or "").strip()
    if not text:
        console.print("[red]No question provided.[/]")
        raise typer.Exit(2)
    return text


async def cmd_ask_file(
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
    """Extract text from a local file (or stdin) and ask a question over it."""
    from unread.cli import _STDIN_REF_SENTINEL
    from unread.files.commands import (
        _extract_for_kind,
        _file_id_for_path,
        _file_id_for_stdin,
        _hash_content,
        _read_stdin_bytes,
    )
    from unread.files.extractors import detect_kind

    if ref == _STDIN_REF_SENTINEL:
        raw, _was_truncated = _read_stdin_bytes()
        if not raw.strip():
            console.print("[red]No data on stdin.[/]")
            raise typer.Exit(2)
        text = raw.decode("utf-8", errors="replace")
        source_label = "<stdin>"
        source_id = _file_id_for_stdin(raw)
        content_hash = _hash_content(text)
        citations = [DocCitation(uri="stdin://", label="stdin", offset_start=0, offset_end=len(text))]
        source_kind = "stdin"
    else:
        path = Path(ref).expanduser().resolve()
        if not path.is_file():
            console.print(f"[red]Not a file: {path}[/]")
            raise typer.Exit(2)
        kind = detect_kind(path)
        result = await _extract_for_kind(path, kind)
        text = result.text
        source_label = path.name
        source_id = _file_id_for_path(path)
        content_hash = _hash_content(text)
        citations = [DocCitation(uri=f"file://{path}", label=path.name, offset_start=0, offset_end=len(text))]
        source_kind = "file"

    used_question = question if question else await _prompt_question(source_label)
    await cmd_ask_document(
        extracted_text=text,
        citations=citations,
        source_label=source_label,
        source_id=source_id,
        source_kind=source_kind,
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
