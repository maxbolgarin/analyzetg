"""Helpers shared by the doc-mode and Telegram-archive ask paths.

The actual rendering of "Run …" summary line + Rule + meta grid +
Markdown body + saved file lives in `unread/util/report_render.py` so
analyze and ask produce identical output. This module owns just the
ask-specific bits: the default save path layout and the question-row
truncation helper.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from unread.core.paths import reports_dir, slugify


def truncate_value(text: str, limit: int = 120) -> str:
    """Squash a multi-line value to a single line with an ellipsis when long.

    Used for the `Question:` row — long questions wrap awkwardly inside
    the metadata grid and bury the rest of the header below the fold.
    """
    one_line = " ".join((text or "").split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1].rstrip() + "…"


def default_ask_path(source_kind: str, source_label: str) -> Path:
    """Default save location for an ask report.

    Layout: `~/.unread/reports/ask/<source_kind>/<slug>-<stamp>.md`

    `source_kind` is one of `"website"`, `"youtube"`, `"file"`,
    `"stdin"`, `"tg"`. `source_label` is slugified; empty / all-
    punctuation labels fall back to the source kind so we never write
    a bare `-<stamp>.md` file.
    """
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    slug = slugify(source_label) or source_kind
    return reports_dir() / "ask" / source_kind / f"{slug}-{stamp}.md"
