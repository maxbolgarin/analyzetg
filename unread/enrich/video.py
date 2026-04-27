"""Video enricher: transcribe the audio track of a video message.

v1 intentionally covers audio-only; adding one keyframe-per-N-seconds for a
visual summary is planned but out of scope here. The existing
`enrich.audio.enrich_audio` already handles the audio transcode + transcription
for `media_type in {"voice", "videonote", "video"}`, so this is a thin
pass-through that keeps `video` as its own toggle in `EnrichOpts` (so a user
can enable voice transcription but skip videos, or vice-versa).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from unread.db.repo import Repo
from unread.enrich.audio import enrich_audio
from unread.enrich.base import EnrichResult
from unread.models import Message

if TYPE_CHECKING:
    from telethon import TelegramClient


async def enrich_video(
    msg: Message,
    *,
    client: TelegramClient,
    repo: Repo,
    model: str | None = None,
    language: str | None = None,
) -> EnrichResult | None:
    """Transcribe the audio track of a video message.

    Guard: only fires for `media_type == "video"` so the audio enricher's
    voice/videonote paths aren't double-processed. Returns the `transcript`
    EnrichResult from the audio enricher unchanged — the formatter already
    knows how to render transcripts inline.
    """
    if msg.media_type != "video":
        return None
    return await enrich_audio(msg, client=client, repo=repo, model=model, language=language)
