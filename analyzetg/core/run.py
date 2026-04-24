"""PreparedRun: the handoff object between the shared pipeline prefix
and its consumers (analyze / dump / download-media).

Lifecycle:
    consumer
      ↓
    prepare_chat_run(...)               # shared pipeline
      ↓ returns PreparedRun
    consumer does its specific work
      (run_analysis / _write / save_raw_media)
      ↓
    await prepared.mark_read_fn()       # deferred side-effect
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telethon import TelegramClient

    from analyzetg.config import Settings
    from analyzetg.db.repo import Repo
    from analyzetg.enrich.base import EnrichStats
    from analyzetg.models import Message


@dataclass(slots=True)
class PreparedRun:
    """Everything a consumer needs after the shared pipeline finishes.

    See `docs/superpowers/specs/2026-04-24-unified-chat-run-pipeline-design.md`
    for field-by-field rationale.
    """

    # --- Identity ---
    chat_id: int
    thread_id: int | None  # None = flat-forum OR non-forum
    chat_title: str | None
    thread_title: str | None  # topic title for forum-topic reports
    chat_username: str | None  # for link template
    chat_internal_id: int | None  # for t.me/c/<id>/ link template

    # --- Data ---
    # Already backfilled + filtered + enriched. Consumers consume this
    # directly; do not call repo.iter_messages again.
    messages: list[Message]
    period: tuple[datetime | None, datetime | None]
    topic_titles: dict[int, str] | None  # flat-forum only
    topic_markers: dict[int, int] | None  # flat-forum only
    raw_msg_count: int  # pre-filter count, for report header

    # --- Enrichment outcome ---
    enrich_stats: EnrichStats | None  # None = enrichment not run

    # --- Deferred side-effect ---
    # None = "nothing to mark" (user passed --no-mark-read OR no
    # messages analyzed). Otherwise a no-arg async callable that
    # handles the right mark-read shape (dialog / single-topic / loop
    # over topics for flat-forum). Consumer calls this AFTER its main
    # work succeeds.
    mark_read_fn: Callable[[], Awaitable[int]] | None

    # --- Shared handles ---
    # Pinned to the real types via TYPE_CHECKING imports so consumers
    # get IDE / mypy help; the imports are fenced to typecheck time so
    # PreparedRun stays importable without pulling Telethon into every
    # module that references it.
    client: TelegramClient
    repo: Repo
    settings: Settings
