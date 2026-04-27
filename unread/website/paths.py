"""Report path helpers for website analyses.

Layout: `reports/website/<domain-slug>/<page-slug>-<preset>-<stamp>.md`.
Mirrors `reports/youtube/<channel>/<video>-<preset>-<stamp>.md` so a
user scanning `reports/` sees one folder per source. Slug rules come
from `core.paths.slugify` — single source of truth (invariant #8).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from unread.core.paths import slugify


def _domain_slug(domain: str | None) -> str:
    if domain and (s := slugify(domain.replace(".", "-"))):
        return s
    return "unknown-domain"


def _page_slug(title: str | None, page_id: str) -> str:
    """Slug + last-6-of-page_id suffix to disambiguate collisions.

    Example: "Why I Like Lisp" + page_id 1f3a... → `why-i-like-lisp-3a4b5c`.
    """
    base = slugify(title) if title else ""
    suffix = page_id[-6:].lower()
    if base:
        return f"{base[:34]}-{suffix}"
    return f"page-{suffix}"


def website_report_path(
    *,
    page_id: str,
    title: str | None,
    domain: str | None,
    preset: str,
    stamp: datetime | None = None,
) -> Path:
    """Default disk path for a website analysis report."""
    when = stamp or datetime.now()
    ts = when.strftime("%Y-%m-%d_%H%M%S")
    return Path(
        "reports",
        "website",
        _domain_slug(domain),
        f"{_page_slug(title, page_id)}-{preset}-{ts}.md",
    )
