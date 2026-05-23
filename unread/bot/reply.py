"""Bot → Telegram reply helpers.

Locates the report that the analyze pipeline just wrote, uploads it as
a TG document, and stamps a one-line caption with token + cost totals
pulled from `usage_log`.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import structlog
from telethon import events

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


async def _upload_with_caption(
    event: events.NewMessage.Event,
    report: Path,
    *,
    started: float,
) -> None:
    """Read the saved report, derive a cost caption, and upload."""
    elapsed = max(0.0, time.time() - started)
    caption = await _build_caption(started, elapsed)
    try:
        await event.client.send_file(
            event.chat_id,
            file=str(report),
            caption=caption,
            reply_to=event.message.id,
            force_document=True,
        )
    except Exception:
        log.exception("bot.upload_failed", report=str(report))
        # Fall back to inline text so the user gets *something*. Cap at
        # 3500 chars to leave headroom under TG's 4096 limit.
        try:
            body = report.read_text(encoding="utf-8")
        except OSError:
            await event.reply("⚠️ Couldn't read the report file.")
            return
        chunk = body[:3500]
        await event.reply(chunk + ("\n…(truncated)" if len(body) > 3500 else ""))


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
