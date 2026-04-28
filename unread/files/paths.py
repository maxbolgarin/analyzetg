"""Report path helper for local-file analyses.

Mirrors `unread/website/paths.py` and `unread/youtube/paths.py` so a
user scanning `~/.unread/reports/` sees one folder per source kind:

  - `~/.unread/reports/files/<kind>/<file-slug>-<preset>-<stamp>.md`

Stdin runs land at `~/.unread/reports/files/stdin/<preset>-<stamp>.md`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from unread.core.paths import reports_dir, slugify


def _file_slug(name: str, file_id: str) -> str:
    """Slug + last-6-of-id suffix to disambiguate same-name files."""
    base = slugify(Path(name).stem) if name else ""
    suffix = file_id[-6:].lower() if file_id else ""
    if base and suffix:
        return f"{base[:34]}-{suffix}"
    if base:
        return base[:40]
    if suffix:
        return f"file-{suffix}"
    return "file"


def file_report_path(
    *,
    file_id: str,
    name: str,
    kind: str,
    preset: str,
    stamp: datetime | None = None,
) -> Path:
    """Default disk path for a local-file analysis report."""
    when = stamp or datetime.now()
    ts = when.strftime("%Y-%m-%d_%H%M%S")
    if kind == "stdin":
        return reports_dir() / "files" / "stdin" / f"{preset}-{ts}.md"
    return reports_dir() / "files" / kind / f"{_file_slug(name, file_id)}-{preset}-{ts}.md"
