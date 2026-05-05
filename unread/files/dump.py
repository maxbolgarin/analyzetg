"""Dump adapter for local files and stdin — preserves the original bytes.

Unlike `unread/youtube/dump.py` and `unread/website/dump.py` which extract
remote content into markdown, the local-file dump is just a save-a-copy:
the original bytes go to `~/.unread/reports/files/<kind>/<original-name>-<stamp>.<ext>`
unchanged. The user already has the file in its native format on disk;
re-extracting it would be lossy (PDF→text drops layout, code→markdown
drops structure) and pointless.

The markdown-with-metadata-header shape lives in the LLM-bound paths
(`unread <file>` analyze, `unread ask <file>`) where the extracted text
is what the model consumes. `dump` is the no-LLM verb — it just saves.

For stdin, there's no original file to copy: bytes are written to
`<stamp>.txt`, decoded as UTF-8 with replacement (assumes text input;
piping a binary stream to `unread dump` is unusual).
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console

console = Console()


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%SZ")


async def cmd_dump_file(
    ref: str,
    *,
    output: Path | None = None,
    console_out: bool = False,
    yes: bool = False,
    language: str | None = None,
    report_language: str | None = None,
    source_language: str | None = None,
) -> None:
    """Save a local file (or stdin bytes) to ~/.unread/reports/files/.

    Files are copied byte-for-byte with their original extension preserved.
    Stdin lands as `<stamp>.txt`.
    """
    from unread.cli import _STDIN_REF_SENTINEL
    from unread.core.paths import reports_dir
    from unread.files.commands import _file_id_for_stdin, _read_stdin_bytes
    from unread.files.extractors import detect_kind

    if ref == _STDIN_REF_SENTINEL:
        raw, _was_truncated = _read_stdin_bytes()
        if not raw.strip():
            console.print("[red]No data on stdin.[/]")
            raise typer.Exit(2)
        out_dir = reports_dir() / "files" / "stdin"
        slug = _file_id_for_stdin(raw)[:12]
        default_output = out_dir / f"{slug}-{_stamp()}.txt"
        target = output or default_output
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        console.print(f"[grey70]Saved to[/] [bold]{target}[/]")
        if console_out:
            console.print(raw.decode("utf-8", errors="replace"))
        return

    path = Path(ref).expanduser().resolve()
    if not path.is_file():
        console.print(f"[red]Not a file: {path}[/]")
        raise typer.Exit(2)
    kind = detect_kind(path)
    out_dir = reports_dir() / "files" / str(kind)
    # Preserve the original extension. The stem gets a stamp suffix so
    # repeat-dumps of the same file don't overwrite each other.
    suffix = "".join(path.suffixes)  # handles `.tar.gz`-style multi-suffixes
    stem = path.name.removesuffix(suffix).replace(" ", "_") or path.stem
    default_output = out_dir / f"{stem}-{_stamp()}{suffix}"
    target = output or default_output
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    console.print(f"[grey70]Saved to[/] [bold]{target}[/]")
    if console_out and kind == "text":
        # Only echo text files to the terminal; binary `console_out` is
        # noise (and may corrupt the user's terminal state).
        console.print(target.read_text(encoding="utf-8", errors="replace"))
