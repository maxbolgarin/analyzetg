"""Dump-to-markdown adapter for local files and stdin.

Mirrors the shape of unread/youtube/dump.py and unread/website/dump.py:
extract text via the existing per-kind extractor (cache-aware, reuses
unread.files.extractors), assemble a metadata header, write a markdown
file under ~/.unread/reports/files/. No LLM call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console

console = Console()


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


def _build_markdown(
    *,
    source_label: str,
    kind: str,
    body: str,
    content_hash: str,
    size_bytes: int,
) -> str:
    header = (
        f"# {source_label}\n\n"
        f"_Kind: {kind} · size: {size_bytes} bytes · sha256: {content_hash[:16]}_\n"
        f"_Extracted: {datetime.now(UTC).isoformat()}_\n\n"
        "---\n\n"
    )
    return header + body.rstrip() + "\n"


async def cmd_dump_file(
    ref: str,
    *,
    output: Path | None = None,
    console_out: bool = False,
    yes: bool = False,
    language: str | None = None,
    content_language: str | None = None,
) -> None:
    """Extract text from a local file (or stdin) and write it as markdown."""
    from unread.cli import _STDIN_REF_SENTINEL
    from unread.core.paths import reports_dir
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
        kind = "stdin"
        size_bytes = len(raw)
        content_hash = _hash_content(text)
        source_label = "<stdin>"
        out_dir = reports_dir() / "files" / "stdin"
        slug = _file_id_for_stdin(raw)[:12]
        default_output = out_dir / f"{slug}-{_stamp()}-dump.md"
    else:
        path = Path(ref).expanduser().resolve()
        if not path.is_file():
            console.print(f"[red]Not a file: {path}[/]")
            raise typer.Exit(2)
        kind = detect_kind(path)
        result = await _extract_for_kind(path, kind)
        text = result.text
        size_bytes = path.stat().st_size
        content_hash = _hash_content(text)
        source_label = path.name
        _ = _file_id_for_path(path)  # touches the cache key for future cache lookups
        out_dir = reports_dir() / "files" / str(kind)
        slug = path.stem.replace(" ", "_")
        default_output = out_dir / f"{slug}-{_stamp()}-dump.md"

    md = _build_markdown(
        source_label=source_label,
        kind=str(kind),
        body=text,
        content_hash=content_hash,
        size_bytes=size_bytes,
    )

    if console_out and output is None:
        console.print(md)
        return

    target = output or default_output
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(md, encoding="utf-8")
    console.print(f"[grey70]Saved to[/] [bold]{target}[/]")
    if console_out:
        console.print(md)
