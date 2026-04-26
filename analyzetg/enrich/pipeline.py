"""Enrichment orchestrator: dispatch per-kind enrichers over a message list.

Called from `analyzer.pipeline.run_analysis` between filter/dedupe and
chunking. Mutates messages in place (attaching transcripts, image
descriptions, extracted text, link summaries) so downstream formatting
and hashing see the enriched body.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext as _null_ctx
from typing import TYPE_CHECKING

from analyzetg.config import get_settings
from analyzetg.db.repo import Repo
from analyzetg.enrich.audio import enrich_audio
from analyzetg.enrich.base import EnrichOpts, EnrichStats
from analyzetg.enrich.document import enrich_document
from analyzetg.enrich.image import enrich_image
from analyzetg.enrich.link import enrich_message_links
from analyzetg.enrich.video import enrich_video
from analyzetg.models import Message
from analyzetg.util.logging import get_logger

if TYPE_CHECKING:
    from telethon import TelegramClient

log = get_logger(__name__)


def _caps(opts: EnrichOpts) -> dict[str, int]:
    return {
        "image": opts.max_images_per_run,
        "link": opts.max_link_fetches_per_run,
    }


async def enrich_messages(
    msgs: list[Message],
    *,
    client: TelegramClient | None,
    repo: Repo,
    opts: EnrichOpts,
    language: str | None = None,
    content_language: str | None = None,
) -> EnrichStats:
    """Run per-kind enrichers across `msgs`, respecting opts + caps.

    Mutates each `Message` in place (sets .transcript, .image_description,
    .extracted_text, .link_summaries). Returns aggregated stats for logging
    and UI.

    `client` may be None only when no Telegram-media enrichment is requested
    (e.g. --enrich=link). The orchestrator raises cleanly if a kind needing
    the client is enabled without one.
    """
    stats = EnrichStats()
    if not msgs or not opts.any_enabled():
        return stats

    needs_client = any((opts.voice, opts.videonote, opts.video, opts.image, opts.doc))
    if needs_client and client is None:
        raise RuntimeError(
            "Enrichment requires a TelegramClient for media downloads. "
            "Pass client=... into run_analysis or disable media-based enrichers."
        )

    settings = get_settings()
    # Image / link enricher prompts go to the LLM → use content_language
    # (the chat content language) so descriptions come back in that
    # language. Mirrors `pipeline._resolve_content_lang`: explicit param
    # → settings.locale.content_language → settings.locale.language → "en".
    # The `language` param is accepted for symmetry with the calling
    # signature but intentionally NOT in the fallback — the caller is
    # responsible for picking the right content_language; falling back
    # to the UI language here would mix layers.
    _ = language  # kept on the signature for caller symmetry
    enrich_language = (
        content_language or settings.locale.content_language or settings.locale.language or "en"
    ).lower()
    sem = asyncio.Semaphore(max(1, opts.concurrency))
    caps = _caps(opts)
    counted: dict[str, int] = {"image": 0, "link": 0}

    # Per-doc_id locks prevent two concurrent handlers from independently
    # downloading/transcribing the same media when the same doc_id appears
    # in multiple messages (common when a voice note is forwarded across
    # several chats in a single batch). Without this, both handlers miss
    # the cache on first look, both call the Whisper API, and
    # `put_media_enrichment` lets the second write overwrite the first —
    # wasting one API call.
    doc_locks: dict[int, asyncio.Lock] = {}

    def _lock_for(doc_id: int) -> asyncio.Lock:
        lock = doc_locks.get(doc_id)
        if lock is None:
            lock = asyncio.Lock()
            doc_locks[doc_id] = lock
        return lock

    async def handle(msg: Message) -> None:
        # Voice / videonote — via audio enricher.
        mt = msg.media_type
        # Serialize per-doc_id to prevent duplicate downloads/API calls
        # when the same media appears under multiple msg_ids in a batch.
        lock = _lock_for(int(msg.media_doc_id)) if msg.media_doc_id else None
        try:
            if mt == "voice" and opts.voice:
                async with sem, lock or _null_ctx():
                    res = await enrich_audio(msg, client=client, repo=repo, model=opts.audio_model)
                if res:
                    stats.record("voice", res)
            elif mt == "videonote" and opts.videonote:
                async with sem, lock or _null_ctx():
                    res = await enrich_audio(msg, client=client, repo=repo, model=opts.audio_model)
                if res:
                    stats.record("videonote", res)
            elif mt == "video" and opts.video:
                async with sem, lock or _null_ctx():
                    res = await enrich_video(msg, client=client, repo=repo, model=opts.audio_model)
                if res:
                    stats.record("video", res)
            elif mt == "photo" and opts.image:
                if counted["image"] >= caps["image"]:
                    stats.record_skip("image")
                else:
                    counted["image"] += 1
                    async with sem, lock or _null_ctx():
                        res = await enrich_image(
                            msg,
                            client=client,
                            repo=repo,
                            model=opts.vision_model,
                            language=enrich_language,
                        )
                    if res:
                        stats.record("image", res)
            elif mt == "doc" and opts.doc:
                async with sem, lock or _null_ctx():
                    res = await enrich_document(msg, client=client, repo=repo)
                if res:
                    stats.record("doc", res)
        except Exception as e:  # Per-message errors must not abort the run.
            log.error(
                "enrich.error",
                kind=mt,
                chat_id=msg.chat_id,
                msg_id=msg.msg_id,
                err=str(e)[:500],
            )
            stats.record_error(mt or "unknown")

        # Link enrichment is orthogonal — any message with text may have URLs.
        if opts.link and msg.text:
            try:
                if counted["link"] >= caps["link"]:
                    # Cap on *fetches* per run, not per message — but we
                    # approximate by capping at message granularity to keep
                    # this simple. Users hitting the cap should raise it.
                    pass
                else:
                    async with sem:
                        pairs = await enrich_message_links(
                            msg,
                            repo=repo,
                            model=opts.link_model,
                            timeout_sec=opts.link_fetch_timeout_sec,
                            skip_domains=opts.skip_link_domains or settings.enrich.skip_link_domains,
                            language=enrich_language,
                        )
                    # Count one per unique URL fetched (cache hits excluded
                    # doesn't matter here — we bound network calls, not
                    # DB lookups).
                    for _ in pairs:
                        counted["link"] += 1
                        if counted["link"] > caps["link"]:
                            break
                    if pairs:
                        stats.counts["link"] = stats.counts.get("link", 0) + len(pairs)
            except Exception as e:
                log.error(
                    "enrich.link.error",
                    chat_id=msg.chat_id,
                    msg_id=msg.msg_id,
                    err=str(e)[:500],
                )
                stats.record_error("link")

    # Wrap the gather in a Rich Progress bar so a 50-image enrich pass
    # shows obvious advancement instead of dead silence for ~30 seconds.
    # Each handler advances the bar in its own try/finally below; we
    # gather once with the progress active.
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[dim]{task.description}[/]"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        transient=False,
        console=Console(),
    ) as progress:
        task_id = progress.add_task("Enriching media", total=len(msgs))

        async def handle_with_progress(m: Message) -> None:
            try:
                await handle(m)
            finally:
                progress.advance(task_id)

        await asyncio.gather(*(handle_with_progress(m) for m in msgs))
    return stats
