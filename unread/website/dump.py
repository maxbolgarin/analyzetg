"""`unread dump <website-url>` — extract article + optionally inlined images.

Mirrors :mod:`unread.website.commands` shape (fetch + cache + write to
disk under ``reports/website/...``) but skips the LLM call and emits
markdown. Two modes:

- ``text``: article only — saved as ``article.md``, with ``# Title``
  and ``## Section`` headings preserved via trafilatura's markdown
  output.
- ``full``: article plus every ``<img>`` inlined into the page,
  downloaded into ``_files/`` and referenced from a ``## Images``
  section appended to ``article.md``.

Why this re-fetches even on a cache hit: the analyze cache stores
flat-text paragraphs (the LLM doesn't need heading marks), so dump
needs the raw HTML in hand to run a second-pass markdown extraction.
A cache row is always written so a follow-on ``unread <url>``
(analyze) is free.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

import typer
from rich.console import Console

from unread.config import get_settings
from unread.db.repo import open_repo
from unread.i18n import t as _t
from unread.i18n import tf as _tf
from unread.util.logging import get_logger
from unread.website.commands import _meta_header
from unread.website.content import (
    WebsiteFetchError,
    WebsitePage,
    extract_markdown_body,
    fetch_page_with_html,
)
from unread.website.images import (
    download_inlined_images,
    extract_inlined_images,
    render_image_section,
)
from unread.website.paths import website_dump_dir
from unread.website.urls import normalize_url, page_id

console = Console()
log = get_logger(__name__)

WebsiteDumpMode = Literal["text", "full"]

_IMAGES_SUBDIR = "_files"


def _build_markdown(page: WebsitePage, body: str, image_block: str = "") -> str:
    """Compose the saved markdown: meta header + extracted body + optional images."""
    header = _meta_header(page.metadata, paragraphs_count=len(page.paragraphs))
    parts = [header, "", body.rstrip()]
    if image_block:
        parts.extend(["", image_block])
    return "\n".join(parts).rstrip() + "\n"


def _resolve_output_dir(
    output: Path | None,
    page: WebsitePage,
    mode: WebsiteDumpMode,
) -> Path:
    if output:
        return output
    return website_dump_dir(
        page_id=page.metadata.page_id,
        title=page.metadata.title,
        domain=page.metadata.domain,
        mode=mode,
        stamp=datetime.now(),
    )


async def cmd_dump_website(
    *,
    url: str,
    mode: WebsiteDumpMode,
    max_images: int,
    output: Path | None,
    console_out: bool,
    language: str,
    content_language: str,
    yes: bool,
) -> None:
    """Dump a single web page to disk. Markdown body + optional inlined images."""
    settings = get_settings()
    normalized = normalize_url(url)
    pid = page_id(normalized)
    del normalized, pid  # currently unused — kept for symmetry with the analyze path

    console.print(f"[grey70]{_tf('website_fetching', url=url)}[/]")
    try:
        page, html = await fetch_page_with_html(url, settings=settings)
    except WebsiteFetchError as e:
        raise typer.BadParameter(str(e)) from e

    # Always refresh the analyze cache so a follow-on `unread <url>` is free.
    async with open_repo(settings.storage.data_path) as repo:
        await repo.put_website_page(
            page_id=page.metadata.page_id,
            url=page.metadata.url,
            normalized_url=page.metadata.normalized_url,
            domain=page.metadata.domain,
            title=page.metadata.title,
            site_name=page.metadata.site_name,
            author=page.metadata.author,
            published=page.metadata.published,
            language=page.metadata.language,
            word_count=page.metadata.word_count,
            paragraphs=page.paragraphs,
            content_hash=page.content_hash,
            extractor=page.extractor,
            raw_html_size=page.raw_html_size,
        )

    if not page.paragraphs:
        console.print(f"[red]{_t('cli_error_prefix')}[/] {_t('err_files_empty_page')}")
        raise typer.Exit(2)

    # Markdown body preserves headings + paragraphs. Fall back to the
    # txt paragraphs if trafilatura's markdown writer comes back empty
    # (older versions, exotic pages) — never produce an empty article.
    body = extract_markdown_body(html, url=page.metadata.url) or "\n\n".join(page.paragraphs)

    out_dir = _resolve_output_dir(output, page, mode)
    out_dir.mkdir(parents=True, exist_ok=True)

    image_block = ""
    saved_count = 0
    if mode == "full":
        images = await extract_inlined_images(html, page.metadata.url, max_images=max_images)
        saved = await download_inlined_images(
            images,
            out_dir / _IMAGES_SUBDIR,
            settings=settings,
        )
        saved_count = len(saved)
        image_block = render_image_section(saved, dir_name=_IMAGES_SUBDIR)

    article_path = out_dir / "article.md"
    article_path.write_text(_build_markdown(page, body, image_block), encoding="utf-8")

    if mode == "full":
        console.print(f"[green]{_tf('dump_website_full_done', path=article_path, images=saved_count)}[/]")
    else:
        console.print(f"[green]{_tf('dump_website_text_done', path=article_path)}[/]")
    if console_out:
        console.print(article_path.read_text(encoding="utf-8"))
