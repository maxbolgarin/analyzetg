"""Top-level handler for `unread analyze <website-url>`.

Mirrors the YouTube flow: fetch the page, segment into synthetic
`Message` rows, hand off to the existing analyzer pipeline, write the
report under `reports/website/<domain>/...`. Skips Telegram backfill +
mark_read; `--cite-context` is a no-op (no surrounding-context store);
`--self-check` and `--post-to/--post-saved` are supported.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from unread.analyzer.pipeline import (
    AnalysisOptions,
    estimate_cost,
    run_analysis,
)
from unread.config import get_settings
from unread.db.repo import open_repo
from unread.i18n import t as _t
from unread.i18n import tf as _tf
from unread.models import Message
from unread.util.logging import get_logger
from unread.website.content import (
    WebsiteFetchError,
    WebsitePage,
    fetch_page,
)
from unread.website.metadata import WebsiteMetadata
from unread.website.paths import website_report_path
from unread.website.urls import normalize_url, page_id

console = Console()
log = get_logger(__name__)


# Two-letter ISO 639-1 codes the rest of the system knows how to render
# (presets / i18n / settings). Used as an allowlist when inferring a
# language from any URL component — anything outside this set is dropped
# silently rather than passed downstream where it would hit a missing
# preset directory or i18n entry.
_VALID_INFERRED_LANGS: frozenset[str] = frozenset(
    {
        "en", "ru", "de", "fr", "es", "it", "pt", "nl", "pl", "tr",
        "uk", "be", "kk", "cs", "sk", "ro", "hu", "fi", "sv", "no",
        "da", "el", "bg", "sr", "hr", "sl", "lt", "lv", "et", "ja",
        "ko", "zh", "ar", "he", "fa", "hi", "th", "vi", "id", "ms",
    }
)  # fmt: skip


# ccTLD → ISO 639-1 mapping. Only includes country TLDs whose dominant
# content language is unambiguous in practice. Skipped on purpose:
#   * Multilingual countries: .ca (en/fr), .ch (de/fr/it/rm), .be (nl/fr),
#     .in / .ph (multi), .lu, .pk
#   * ccTLDs commonly resold as generic / vanity domains: .ai .io .me
#     .tv .co .so .to .gg .ly .vc .cc .ws .fm .am .gd .sh
# A wrong inference would actively confuse the LLM, so the rule is:
# only include entries where >90 % of pages on that ccTLD are in the
# mapped language. The allowlist stays in `_VALID_INFERRED_LANGS`.
_CCTLD_TO_LANG: dict[str, str] = {
    # Cyrillic / Slavic
    "ru": "ru", "ua": "uk", "by": "be", "kz": "kk",
    "pl": "pl", "cz": "cs", "sk": "sk", "bg": "bg", "rs": "sr",
    "hr": "hr", "si": "sl",
    # German / Austrian
    "de": "de", "at": "de",
    # Romance
    "fr": "fr",
    "it": "it",
    "es": "es",
    "pt": "pt", "br": "pt",
    # Other European
    "nl": "nl",
    "ro": "ro",
    "hu": "hu",
    "gr": "el",
    "tr": "tr",
    "lt": "lt", "lv": "lv", "ee": "et",
    "fi": "fi",
    "se": "sv",
    "no": "no",
    "dk": "da",
    # Asian
    "jp": "ja",
    "kr": "ko",
    "cn": "zh", "tw": "zh", "hk": "zh",
    "vn": "vi",
    "th": "th",
    # Middle Eastern
    "il": "he",
    "ir": "fa",
    # Latin American Spanish
    "mx": "es", "ar": "es", "cl": "es", "pe": "es", "ve": "es",
    "uy": "es", "py": "es", "ec": "es", "bo": "es", "gt": "es",
    "sv": "es", "do": "es", "ni": "es", "hn": "es", "cr": "es",
}  # fmt: skip


def _source_language_from_url(url: str) -> str | None:
    """Heuristic source-language inference from a URL.

    Three rules tried in order; first match wins. None of them are
    perfect — they're a best-effort hint to seed the LLM's understanding
    of the source content. When no rule matches, the analyzer's
    system prompt still tells the LLM to detect the source language
    itself; this URL pass is a free signal layered on top.

    1. **Leading subdomain is a 2-letter ISO code**:
       `ru.wikipedia.org`, `de.example.com` → `ru`, `de`. The most
       reliable signal: this convention is widespread (Wikipedia, news
       sites, e-commerce). False-positive risk is low because random
       subdomains are rarely 2-char ISO codes.
    2. **ccTLD with an unambiguous dominant language**:
       `alex.ru`, `site.de`, `news.fr` → `ru`, `de`, `fr`. See
       :data:`_CCTLD_TO_LANG` for the curated mapping; multilingual
       countries and vanity ccTLDs (.ai, .io, .me, …) are intentionally
       absent.
    3. **2-letter language segment anywhere in the path**:
       `example.com/ru/page`, `max.com/asfa/ru`, `docs.com/de/intro`,
       `app.com/en-US/help` (the `en-US` form is split on `-`). Lower
       confidence — paths can contain 2-char strings for unrelated
       reasons — so we only match exact ISO codes from the allowlist.

    Returns the ISO 639-1 code on first match, or None when nothing fits.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    parts = host.split(".")

    # 1. Leading subdomain — only fires on `<lang>.example.tld` shape
    # (need at least 3 labels). `ru.alex.com` matches; `alex.com` doesn't.
    if len(parts) >= 3 and parts[0] in _VALID_INFERRED_LANGS:
        return parts[0]

    # 2. ccTLD — last label, unambiguous mapping only.
    tld = parts[-1] if parts else ""
    if tld in _CCTLD_TO_LANG:
        return _CCTLD_TO_LANG[tld]

    # 3. Path segment — exact 2-letter match anywhere in the path.
    # Region-tagged forms (`en-US`, `pt-BR`, `zh-Hans`) are split and
    # the language prefix is checked.
    for raw_seg in (parsed.path or "").lower().strip("/").split("/"):
        seg = raw_seg.split("-", 1)[0] if "-" in raw_seg else raw_seg
        if len(seg) == 2 and seg in _VALID_INFERRED_LANGS:
            return seg

    return None


def _meta_header(meta: WebsiteMetadata, *, paragraphs_count: int) -> str:
    """Compact metadata block prepended as the first synthetic message."""
    bits: list[str] = [f"Website: {meta.title or meta.url}"]
    if meta.site_name and meta.site_name != meta.title:
        bits.append(f"Site: {meta.site_name}")
    if meta.author:
        bits.append(f"Author: {meta.author}")
    if meta.published:
        bits.append(f"Published: {meta.published}")
    if meta.language:
        bits.append(f"Language: {meta.language}")
    if meta.word_count:
        bits.append(f"Word count: {meta.word_count:,}")
    bits.append(f"Paragraphs: {paragraphs_count}")
    bits.append(f"URL: {meta.url}")
    return "\n".join(bits)


def _build_synthetic_messages(meta: WebsiteMetadata, paragraphs: list[str]) -> list[Message]:
    """Header + per-paragraph `Message` list keyed off `chat_id=0`.

    msg_id strategy: header is `#0`; paragraphs are `#1..#N` so a
    citation `[#7]` resolves to "the 7th paragraph" — the link template
    is just the page URL (no fragment). The article author / publisher
    name is reused as `sender_name` for every row so the formatter has
    something coherent to print, even though websites have no real
    "speaker" concept.
    """
    fetched = datetime.now(UTC)
    sender = meta.site_name or meta.author or meta.domain or "website"
    msgs: list[Message] = [
        Message(
            chat_id=0,
            msg_id=0,
            date=fetched,
            sender_name=sender,
            text=_meta_header(meta, paragraphs_count=len(paragraphs)),
        )
    ]
    for i, body in enumerate(paragraphs, start=1):
        msgs.append(
            Message(
                chat_id=0,
                msg_id=i,
                date=fetched,
                sender_name=sender,
                text=body,
            )
        )
    return msgs


def _restore_page_from_row(row: dict) -> WebsitePage:
    """Rebuild a `WebsitePage` from a cached `website_pages` row."""
    import json

    paragraphs = list(json.loads(row["paragraphs_json"])) if row.get("paragraphs_json") else []
    metadata = WebsiteMetadata(
        url=row["url"],
        normalized_url=row["normalized_url"],
        page_id=row["page_id"],
        domain=row.get("domain") or "",
        title=row.get("title"),
        site_name=row.get("site_name"),
        author=row.get("author"),
        published=row.get("published"),
        language=row.get("language"),
        word_count=int(row.get("word_count") or 0),
    )
    import contextlib

    fetched_raw = row.get("fetched_at")
    fetched_at = datetime.now(UTC)
    if isinstance(fetched_raw, datetime):
        fetched_at = fetched_raw
    elif isinstance(fetched_raw, str):
        with contextlib.suppress(ValueError):
            fetched_at = datetime.fromisoformat(fetched_raw.replace("Z", "+00:00"))
    return WebsitePage(
        metadata=metadata,
        paragraphs=paragraphs,
        raw_html_size=int(row.get("raw_html_size") or 0),
        fetched_at=fetched_at,
        content_hash=row["content_hash"],
        extractor=row.get("extractor") or "",
    )


def _render_panel(page: WebsitePage) -> Panel:
    """Pretty-print the fetched page summary."""
    rows: list[str] = []
    if page.metadata.site_name:
        rows.append(f"[bold]Site[/]    {page.metadata.site_name}")
    rows.append(f"[bold]Title[/]   {page.metadata.title or page.metadata.url}")
    if page.metadata.author:
        rows.append(f"[bold]Author[/]  {page.metadata.author}")
    if page.metadata.published:
        rows.append(f"[bold]Date[/]    {page.metadata.published}")
    if page.metadata.language:
        rows.append(f"[bold]Lang[/]    {page.metadata.language}")
    rows.append(
        f"[bold]Body[/]    {page.metadata.word_count:,} words "
        f"/ {len(page.paragraphs)} paragraphs (extractor: {page.extractor})"
    )
    rows.append(f"[bold]URL[/]     {page.metadata.url}")
    return Panel("\n".join(rows), title="Website page", border_style="cyan")


async def cmd_analyze_website(
    *,
    url: str,
    preset: str | None,
    prompt_file: Path | None,
    model: str | None,
    filter_model: str | None,
    output: Path | None,
    console_out: bool,
    no_console: bool = False,
    no_cache: bool = False,
    max_cost: float | None = None,
    dry_run: bool = False,
    self_check: bool = False,
    post_to: str | None = None,
    post_saved: bool = False,
    language: str = "en",
    report_language: str = "en",
    source_language: str = "",
    yes: bool = False,
) -> None:
    """Analyze a single web page. Fetches once, caches by content hash."""
    from unread.analyzer.commands import (
        _load_preset_for_commands,
        _post_to_chat,
        _print_and_write,
        _self_check,
    )

    settings = get_settings()
    normalized = normalize_url(url)
    pid = page_id(normalized)

    effective_preset = preset or "website"

    async with open_repo(settings.storage.data_path) as repo:
        cached = None if no_cache else await repo.get_website_page(pid)
        if cached and cached.get("paragraphs_json"):
            console.print(f"[grey70]{_tf('website_using_cached', url=url)}[/]")
            page = _restore_page_from_row(cached)
        else:
            console.print(f"[grey70]{_tf('website_fetching', url=url)}[/]")
            try:
                page = await fetch_page(url, settings=settings)
            except WebsiteFetchError as e:
                raise typer.BadParameter(str(e)) from e

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

        # Render the metadata panel regardless of cache hit — the only
        # observable difference between cached and freshly-fetched runs
        # should be the leading "fetching..." vs "using cached..." status
        # line, not whether the panel appears.
        console.print(_render_panel(page))

        if not page.paragraphs:
            console.print(f"[red]{_t('cli_error_prefix')}[/] {_t('err_files_empty_page')}")
            raise typer.Exit(2)

        messages = _build_synthetic_messages(page.metadata, page.paragraphs)
        loaded_preset = _load_preset_for_commands(effective_preset, prompt_file, language=report_language)

        # Auto-derive source-language hint when the user didn't pass an
        # explicit `--content-language`. Two layers of detection:
        #   1. `page.metadata.language` — trafilatura's detection during
        #      extraction. Reliable when the page sets a `<html lang="">`
        #      or trafilatura can detect from content.
        #   2. URL-host language subdomain — zh.wikipedia.org, ru.lenta.ru,
        #      etc. The robust fallback when trafilatura returned nothing.
        # Without this auto-derivation, a bare `unread <chinese-wiki-url>`
        # leaves the LLM with no source-language hint and it tends to
        # mirror the source language for the analysis body even when the
        # report-language preset says otherwise. Explicit user value
        # always wins.
        if not source_language:
            if page.metadata.language:
                source_language = page.metadata.language.strip().lower().split("-", 1)[0]
                log.info("website.source_language.auto", lang=source_language, src="page.metadata")
            else:
                derived = _source_language_from_url(page.metadata.url)
                if derived:
                    source_language = derived
                    log.info("website.source_language.auto", lang=source_language, src="url.host")

        if dry_run:
            n = len(messages)
            if loaded_preset is None:
                console.print(f"[bold]Dry run: {n} synthetic msgs / preset={effective_preset}[/]")
                return
            lo, hi = estimate_cost(
                n_messages=n,
                preset=loaded_preset,
                settings=settings,
            )
            console.print(
                f"[bold]Dry run: page={page.metadata.page_id} "
                f"paragraphs={len(page.paragraphs)} preset={effective_preset} "
                f"final={loaded_preset.final_model} filter={loaded_preset.filter_model}[/]"
            )
            if hi is not None:
                console.print(f"  Estimated cost: ${lo or 0.0:.4f} – ${hi:.4f}")
            else:
                console.print("  [yellow]Cost estimate unavailable (missing pricing entry)[/]")
            return

        if max_cost is not None and loaded_preset is not None:
            lo, hi = estimate_cost(
                n_messages=len(messages),
                preset=loaded_preset,
                settings=settings,
            )
            if hi is not None and hi > max_cost:
                console.print(
                    f"[bold yellow]Estimated upper-bound cost ${hi:.4f} exceeds --max-cost ${max_cost:.4f}[/]"
                )
                if yes:
                    console.print(f"[red]{_t('aborting_yes_set')}[/]")
                    raise typer.Exit(2)
                from unread.util.prompt import confirm as _confirm

                if not _confirm("Run anyway?", default=False):
                    console.print("[yellow]Aborted.[/]")
                    raise typer.Exit(0)

        opts = AnalysisOptions(
            preset=effective_preset,
            prompt_file=prompt_file,
            model_override=model,
            filter_model_override=filter_model,
            use_cache=not no_cache,
            include_transcripts=True,
            min_msg_chars=0,  # synthetic header may be short; never drop it
            website_page_id=page.metadata.page_id,
            website_content_hash=page.content_hash,
            source_kind="website",
        )

        # Citations like `[#7](URL)` jump straight back to the page (no
        # fragment — paragraph indices have no native HTML anchor).
        link_template = page.metadata.url

        console.print(f"[grey70]{_t('running_analysis')}[/]")
        result = await run_analysis(
            repo=repo,
            chat_id=0,
            thread_id=None,
            title=page.metadata.title or page.metadata.url,
            opts=opts,
            messages=messages,
            language=language,
            report_language=report_language,
            source_language=source_language,
            link_template_override=link_template,
        )

        if self_check and result.final_result and messages:
            verification, verification_err = await _self_check(
                result=result,
                messages=messages,
                repo=repo,
                report_language=report_language,
            )
            heading = _t("verification_heading", language)
            if verification:
                result.final_result = result.final_result.rstrip() + f"\n\n## {heading}\n\n" + verification
            elif verification_err:
                failure_line = _t("verification_failed", language).format(err=verification_err)
                result.final_result = result.final_result.rstrip() + f"\n\n## {heading}\n\n" + failure_line

        if output is None and not console_out:
            output_path: Path | None = website_report_path(
                page_id=page.metadata.page_id,
                title=page.metadata.title,
                domain=page.metadata.domain,
                preset=effective_preset,
            )
        else:
            output_path = output

        _print_and_write(
            result,
            output=output_path,
            title=page.metadata.title or page.metadata.url,
            console_out=not no_console,
            no_save=console_out,
        )

        post_target = post_to if post_to else ("me" if post_saved else None)
        if post_target and result.msg_count > 0:
            from unread.tg.client import tg_client

            try:
                async with tg_client(settings) as client:
                    await _post_to_chat(
                        client,
                        repo,
                        result,
                        title=page.metadata.title or page.metadata.url,
                        target=post_target,
                    )
            except Exception as e:
                log.warning("website.post_failed", target=post_target, err=str(e)[:200])
                console.print(f"[yellow]{_tf('couldnt_post_to', target=post_target, err=e)}[/]")
