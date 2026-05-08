"""Shared rendering shell for `unread <ref>` (analyze) and `unread ask <ref>`.

Both flows produce the same on-screen shape:

    [bold cyan]Run[/] <one-line summary>
    ──── <title> ────
    <bold-cyan label>: <value>
    <bold-cyan label>: <value>
    ...

    <Markdown body of the LLM answer>
    ──────────────────
    [green]Also saved: <path>[/]      (or "Written: <path>" when no_console)

…and the same saved file shape:

    ---
    **Label:** value
    **Label:** value
    ...
    ---

    <body>

The data feeding into it differs (analyze knows about chunks / cache /
period; ask knows about Source / Question / Mode), but the rendering
itself doesn't. This module is the single source of truth — both
`unread/analyzer/commands.py:_print_and_write` and the ask paths in
`unread/ask/` build their own row list and call `print_report_shell`.

Lazy imports inside the body keep the module-level surface small so
importing it doesn't drag in `analyzer/commands.py`.
"""

from __future__ import annotations

import os
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.rule import Rule
from rich.table import Table

from unread.core.paths import unique_path
from unread.i18n import tf as _tf
from unread.util.fsmode import tighten

console = Console()

# Terminals where OSC 8 hyperlinks reliably fire Cmd/Ctrl+click. Anything
# outside this set falls back to plaintext URL rendering in the console
# so the link is at least clickable via the terminal's built-in URL
# detector. VS Code / Cursor / most Linux terminals advertise OSC 8
# support but in practice line wrapping inside Rich's Markdown renderer
# breaks the sequence often enough that clicks land on inert styled
# text.
_OSC8_FRIENDLY_TERMINALS = frozenset(
    {
        "iTerm.app",
        "WezTerm",
        "kitty",
        "ghostty",
        "Tabby",
        "Hyper",
    }
)


def _should_use_plain_citations(*, force_plain: bool) -> bool:
    """Return True iff the console renderer should flatten `[#N](URL)`.

    `force_plain=True` (user setting / `--plain-citations` flag) always
    wins. Otherwise we auto-detect: only well-known OSC 8-friendly
    terminal emulators keep the styled clickable form; everywhere else
    we drop to `#N (URL)` so the URL is visible and the terminal's
    plaintext URL detector can make it clickable.
    """
    if force_plain:
        return True
    return os.environ.get("TERM_PROGRAM", "") not in _OSC8_FRIENDLY_TERMINALS


def _strip_md_bold(label: str) -> str:
    """Strip `**…**` from i18n labels for Rich rendering.

    i18n stores labels like `**Source:**` so the saved markdown header
    renders bold. The Rich grid styles them via markup instead, so the
    wrapper has to come off before the row is added.
    """
    if label.startswith("**") and label.endswith("**"):
        return label[2:-2]
    return label


def render_meta_grid(rows: list[tuple[str, str]]) -> Table:
    """Build a Rich `Table.grid` for the report header.

    Bold-cyan label column on the left, fold-overflow value column on
    the right. Caller passes already-i18nized labels (e.g. `**Source:**`,
    `**Chat:**`); the bold-markdown wrapper is stripped here.
    """
    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="right", style="bold cyan", no_wrap=True)
    grid.add_column(overflow="fold")
    for label, value in rows:
        grid.add_row(_strip_md_bold(label), value)
    return grid


def render_md_header(rows: list[tuple[str, str]]) -> str:
    """Build the `--- … ---` markdown header prepended to saved reports.

    Labels arrive already wrapped in `**…**` so the saved file renders
    bold. Trailing blank line separates the header from the answer body.
    """
    lines: list[str] = ["---"]
    for label, value in rows:
        lines.append(f"{label} {value}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def print_report_shell(
    *,
    summary_line: str,
    title: str | None,
    meta_rows: list[tuple[str, str]],
    body_md: str,
    output: Path | None,
    default_path: Path,
    no_console: bool = False,
    no_save: bool = False,
    plain_citations: bool = False,
    saved_label_key: str = "also_saved",
) -> Path | None:
    """Render the report shell + (optionally) save to disk.

    Both analyze and ask call this with their own row data. The shell
    handles printing the summary line, the Rule + grid + Markdown body
    + closing Rule, and the markdown-headered save file.

    `summary_line` is printed verbatim (already styled); typical shape:
    `f"[bold cyan]{_t('report_summary_run')}[/] preset=… cost=…"`.

    `body_md` is the raw answer markdown — no `Rule`, no `# question`
    wrapper, no inline `_Source: …_` blurb. The header table carries
    all the metadata.

    `plain_citations=True` flattens markdown links to plain URLs in the
    console render only (saved file keeps the links). Mirrors analyze's
    `settings.analyze.plain_citations` behavior.

    `no_console=True && no_save=True` is rejected — that combo would
    suppress every form of output, leaving an LLM-billed run with
    nothing to show for the spend.

    Returns the saved path (or None when `no_save=True`).
    """
    if no_console and no_save:
        raise ValueError("no_console=True and no_save=True would suppress all output")

    saved_path: Path | None = None

    if not no_console:
        console.print(summary_line)
        console.print(Rule(title or "result", style="cyan"))
        console.print(render_meta_grid(meta_rows))
        console.print()  # blank line between header grid and body
        rendered = body_md
        if _should_use_plain_citations(force_plain=plain_citations):
            from unread.analyzer.commands import _flatten_citations

            rendered = _flatten_citations(rendered)
        console.print(Markdown(rendered))
        console.print(Rule(style="cyan"))
    else:
        # Even in --no-console mode, print the one-line summary so the
        # user gets the cost / scope at a glance. Mirrors analyze's
        # behavior where the "Run …" line fires unconditionally.
        console.print(summary_line)

    if not no_save:
        target = output or default_path
        target.parent.mkdir(parents=True, exist_ok=True)
        # Even with seconds-precision stamps, two parallel invocations
        # can still land in the same second — `unique_path` appends
        # -2/-3 so we never silently overwrite a previous report.
        target = unique_path(target)
        target.write_text(
            render_md_header(meta_rows) + body_md,
            encoding="utf-8",
        )
        # Reports often contain private content. Tighten to owner-only
        # so other local users on a shared box can't read them.
        tighten(target)
        # `also_saved` when both terminal AND file exist (default);
        # `written_to` when only the file exists (--no-console).
        label_key = saved_label_key if not no_console else "written_to"
        console.print(f"[green]{_tf(label_key, path=target)}[/]")
        saved_path = target

    return saved_path
