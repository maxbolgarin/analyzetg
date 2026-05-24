"""Bot → Telegram reply helpers.

Locates the report that the analyze pipeline just wrote, then:

1. Replies with the TL;DR section as a properly-formatted text message
   (renders natively on every Telegram client — phone, desktop, web).
2. Uploads the full report as a PDF (preferred — markdown attachments
   open as raw text on phones) or falls back to the .md file when the
   optional `[bot]` extras aren't installed.
3. Stamps a one-line caption on the document with elapsed time and
   cost totals from `usage_log`.
"""

from __future__ import annotations

import io
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import structlog
from telethon import events
from telethon.tl.types import DocumentAttributeFilename

from unread.bot.extract import extract_tldr
from unread.config import get_settings
from unread.core.paths import reports_dir
from unread.db.repo import open_repo

log = structlog.get_logger(__name__)

# Phase tags that count toward a single analyze-from-the-bot request.
# Mirrors the tags written by `analyzer/openai_client.py` and the
# enrich orchestrators. We don't separate by source kind here — every
# request pays for one map+reduce pass plus its own enrichment fan-out,
# so summing across both gives the user the real-money figure.
_ANALYZE_PHASES: tuple[str, ...] = (
    "analyze_map",
    "analyze_reduce",
    "filter",
    "enrich_voice",
    "enrich_videonote",
    "enrich_video",
    "enrich_image",
    "enrich_doc",
    "enrich_link",
)


async def send_file_report(
    event: events.NewMessage.Event,
    *,
    local_path: Path,
    preset: str,
    started: float,
    kind: str,
) -> None:
    """Upload the most recent local-file report under reports/files/<kind>.

    Picks the newest `*.md` whose mtime is ≥ `started` and (when
    possible) matches the file slug derived from `local_path`. The
    pipeline always writes via `file_report_path`, which uses the same
    slug + preset + timestamp shape — see
    `unread/files/paths.py:file_report_path`.
    """
    target_kind = kind if kind != "text" else "text"
    report = _newest_report_under(
        reports_dir() / "files" / target_kind,
        since=started,
        hint=local_path.stem,
        preset=preset,
    )
    if report is None:
        await event.reply(
            "⚠️ Analysis finished but I couldn't find the saved report. "
            "Check `~/.unread/reports/files/` on the host."
        )
        return
    await _upload_with_caption(event, report, started=started)


async def send_youtube_report(
    event: events.NewMessage.Event,
    *,
    preset: str,
    started: float,
    hint: str,
) -> None:
    """Upload the most recent YouTube report. `hint` is the video slug."""
    report = _newest_report_recursive(
        reports_dir() / "youtube",
        since=started,
        hint=hint,
        preset=preset,
    )
    if report is None:
        await event.reply("⚠️ Analysis finished but I couldn't find the saved report.")
        return
    await _upload_with_caption(event, report, started=started)


async def send_website_report(
    event: events.NewMessage.Event,
    *,
    preset: str,
    started: float,
    hint: str,
) -> None:
    """Upload the most recent website report. `hint` is the page slug."""
    report = _newest_report_recursive(
        reports_dir() / "website",
        since=started,
        hint=hint,
        preset=preset,
    )
    if report is None:
        await event.reply("⚠️ Analysis finished but I couldn't find the saved report.")
        return
    await _upload_with_caption(event, report, started=started)


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


def _newest_report_under(
    directory: Path,
    *,
    since: float,
    hint: str,
    preset: str,
) -> Path | None:
    """Newest `*.md` in `directory` written after `since` (mtime, epoch sec).

    Prefers a name that contains both the hint slug and the preset
    string; falls back to the newest matching file when the hint
    doesn't appear (e.g. truncated slugs).
    """
    if not directory.exists():
        return None
    candidates = [p for p in directory.glob("*.md") if p.stat().st_mtime >= since - 1]
    return _pick_best_match(candidates, hint=hint, preset=preset)


def _newest_report_recursive(
    directory: Path,
    *,
    since: float,
    hint: str,
    preset: str,
) -> Path | None:
    """Recursive variant — youtube/website reports are nested one level deeper."""
    if not directory.exists():
        return None
    candidates = [p for p in directory.rglob("*.md") if p.stat().st_mtime >= since - 1]
    return _pick_best_match(candidates, hint=hint, preset=preset)


def _pick_best_match(candidates: list[Path], *, hint: str, preset: str) -> Path | None:
    if not candidates:
        return None
    hint_norm = hint.lower()
    preset_norm = (preset or "").lower()

    def score(p: Path) -> tuple[int, float]:
        name = p.name.lower()
        s = 0
        if preset_norm and preset_norm in name:
            s += 2
        if hint_norm and hint_norm[:20] in name:
            s += 1
        return (s, p.stat().st_mtime)

    candidates.sort(key=score, reverse=True)
    return candidates[0]


# Reports under this many chars don't get a PDF attachment — they're
# small enough that the TL;DR (or the whole body when there's no
# TL;DR) is the report. A typical short-report shape is `## TL;DR`
# + `## Sources` + `## Verification` boilerplate, which fits well
# inside this budget; longer analyses with real section content stay
# above it.
_SMALL_REPORT_THRESHOLD_CHARS = 1500


def _is_small_report(md_text: str) -> bool:
    """True iff the report is tiny enough that a PDF attachment is overkill."""
    return len(md_text) < _SMALL_REPORT_THRESHOLD_CHARS


async def _upload_with_caption(
    event: events.NewMessage.Event,
    report: Path,
    *,
    started: float,
) -> None:
    """Send the TL;DR inline + the full report as PDF (or .md fallback).

    Tiny reports (under `_SMALL_REPORT_THRESHOLD_CHARS` of markdown)
    skip the PDF entirely — there's nothing in the body the TL;DR
    didn't already cover. The cost / elapsed caption rides along with
    the inline message instead of the PDF caption.
    """
    elapsed = max(0.0, time.time() - started)
    caption = await _build_caption(started, elapsed)
    try:
        md_text = report.read_text(encoding="utf-8")
    except OSError:
        log.exception("bot.report_read_failed", report=str(report))
        await event.reply(f"⚠️ Analysis done but report unreadable. {caption}")
        return

    if _is_small_report(md_text):
        await _send_inline_only(event, md_text, caption)
        return

    # Step 1: TL;DR inline. Phone clients render Telegram MarkdownV1
    # natively — `**bold**`, `[text](url)` citations, etc. — so the
    # user sees the summary without downloading anything.
    await _send_tldr(event, md_text)

    # Step 2: full report as a document. PDF when the optional
    # `[bot]` extras are present; raw `.md` otherwise so the operator
    # still gets something even on a minimal install.
    await _send_full_report(event, report=report, md_text=md_text, caption=caption)


async def _send_inline_only(
    event: events.NewMessage.Event,
    md_text: str,
    caption: str,
) -> None:
    """One text reply with TL;DR (or whole body) + cost caption appended.

    Used for tiny reports where a PDF attachment would carry no extra
    information. Falls back to plain text if markdown rendering
    chokes on stray characters in the LLM output.
    """
    tldr = extract_tldr(md_text)
    body = tldr if tldr else md_text.strip()
    # Telegram caps text messages at 4096 chars. The threshold gating
    # this path keeps us well under that, but cap defensively in case
    # a future preset emits an oversized TL;DR.
    if len(body) > 3500:
        body = body[:3500].rsplit(" ", 1)[0] + " …"
    text = f"**TL;DR**\n\n{body}\n\n_{caption}_" if tldr else f"{body}\n\n_{caption}_"
    try:
        await event.reply(text, parse_mode="md")
    except Exception:
        log.warning("bot.small_report.md_failed", exc_info=True)
        try:
            await event.reply(f"{body}\n\n{caption}")
        except Exception:
            log.exception("bot.small_report.send_failed")


async def _send_tldr(event: events.NewMessage.Event, md_text: str) -> None:
    """Reply with the TL;DR body. No-op when the report has no TL;DR."""
    tldr = extract_tldr(md_text)
    if not tldr:
        return
    # Telegram caps text messages at 4096 chars. TL;DRs are usually a
    # paragraph or two (≤1000 chars), but cap defensively so a runaway
    # LLM output never trips the API.
    if len(tldr) > 3800:
        tldr = tldr[:3800].rsplit(" ", 1)[0] + " …"
    try:
        await event.reply(f"**TL;DR**\n\n{tldr}", parse_mode="md")
    except Exception:
        # Markdown can choke on stray characters in the TL;DR. Retry
        # as plain text rather than dropping the message entirely.
        log.warning("bot.tldr_md_failed", exc_info=True)
        try:
            await event.reply(f"TL;DR\n\n{tldr}")
        except Exception:
            log.exception("bot.tldr_send_failed")


async def _send_full_report(
    event: events.NewMessage.Event,
    *,
    report: Path,
    md_text: str,
    caption: str,
) -> None:
    """Send the full report as PDF (preferred) or .md (fallback).

    `settings.bot.report_format` chooses the preferred format:
        - `"pdf"` (default): render via weasyprint, fall back to .md
          when libpango isn't installed at runtime.
        - `"md"`: skip the PDF render entirely; just upload the .md.
    """
    from unread.bot import pdf as pdf_helper

    pdf_bytes: bytes | None = None
    if get_settings().bot.report_format == "pdf" and pdf_helper.is_available():
        try:
            pdf_bytes = pdf_helper.markdown_to_pdf_bytes(md_text, title=report.stem)
        except Exception:
            # PDF generation can fail on weird CSS or a Pango edge
            # case — log and fall through to the .md upload so the
            # user still gets the report.
            log.exception("bot.pdf_render_failed", report=str(report))
            pdf_bytes = None

    try:
        if pdf_bytes is not None:
            buf = io.BytesIO(pdf_bytes)
            # Telethon picks the upload filename from the BytesIO
            # object only when we attach a DocumentAttributeFilename.
            pdf_name = report.stem + ".pdf"
            await event.client.send_file(
                event.chat_id,
                file=buf,
                attributes=[DocumentAttributeFilename(file_name=pdf_name)],
                caption=caption,
                reply_to=event.message.id,
                force_document=True,
            )
        else:
            await event.client.send_file(
                event.chat_id,
                file=str(report),
                caption=caption,
                reply_to=event.message.id,
                force_document=True,
            )
    except Exception:
        log.exception("bot.upload_failed", report=str(report))
        # Last-resort: inline text (capped under the 4096 limit).
        chunk = md_text[:3500]
        await event.reply(chunk + ("\n…(truncated)" if len(md_text) > 3500 else ""))


async def _build_caption(started: float, elapsed: float) -> str:
    """Compose the `✓ Xs | tokens | $cost` one-liner."""
    s = get_settings()
    since = datetime.fromtimestamp(started)
    try:
        async with open_repo(s.storage.data_path) as repo:
            usage = await repo.sum_usage_since(since, phases=_ANALYZE_PHASES)
    except Exception:
        # DB hiccup shouldn't lose the report — render a fallback caption.
        log.exception("bot.caption_usage_lookup_failed")
        return f"✓ Done in {elapsed:.1f}s"
    prompt = int(usage["prompt_tokens"])
    cached = int(usage["cached_tokens"])
    completion = int(usage["completion_tokens"])
    cost = float(usage["cost_usd"])
    parts = [f"✓ {elapsed:.1f}s"]
    if prompt or completion:
        parts.append(f"{prompt}↓ + {completion}↑ tok")
        if cached:
            parts.append(f"({cached} cached)")
    if cost:
        parts.append(f"${cost:.4f}")
    return " | ".join(parts)


# Convenience for handlers that want the same phase tuple. Importable
# from `unread.bot.reply`.
def analyze_phases() -> Iterable[str]:
    return _ANALYZE_PHASES
