"""Pre-analysis filters and dedupe (spec §9.3, §9.4)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from unread.models import Message


@dataclass(slots=True)
class FilterOpts:
    min_msg_chars: int = 3
    text_only: bool = False
    include_transcripts: bool = True
    # `--by` substring (case-insensitive) on sender_name. None → no filter.
    # When the user passes a numeric, callers set `sender_id` instead.
    sender_substring: str | None = None
    sender_id: int | None = None


_SERVICE_PREFIXES = ("[service]", "[join]", "[pin]", "[rename]")


def _is_service(m: Message) -> bool:
    # Our normalizer doesn't currently emit Telethon's service marker explicitly;
    # we treat empty/media-only messages with no text/transcript as ignorable upstream.
    # If upstream starts annotating, a leading [service] tag is honored here.
    return bool(m.text) and any(m.text.lower().startswith(p) for p in _SERVICE_PREFIXES)


def effective_text(m: Message, opts: FilterOpts | None = None) -> str:
    """Return the analyzable body: text + enrichments the caller wants.

    With `include_transcripts=True` (default), transcripts, image descriptions,
    extracted document text, and link summaries all count as "text" — the
    whole point of enrichment is that a message with a transcribed voice note
    or a described photo becomes analyzable.
    """
    opts = opts or FilterOpts()
    if not opts.include_transcripts:
        return (m.text or "").strip()
    parts: list[str] = []
    if m.text:
        parts.append(m.text)
    if m.image_description:
        parts.append(m.image_description)
    if m.extracted_text:
        parts.append(m.extracted_text)
    if m.transcript:
        parts.append(m.transcript)
    if m.link_summaries:
        parts.extend(s for _, s in m.link_summaries)
    return "\n".join(p.strip() for p in parts if p).strip()


def filter_messages(msgs: list[Message], opts: FilterOpts) -> list[Message]:
    sender_needle = opts.sender_substring.casefold() if opts.sender_substring else None
    out: list[Message] = []
    for m in msgs:
        if _is_service(m):
            continue
        body = effective_text(m, opts)
        if not body:
            continue
        if len(body) < opts.min_msg_chars:
            continue
        # `text_only` means "only keep messages with native text (drop media-only)".
        # Enrichment doesn't bypass this — if the caller asked for text_only,
        # they explicitly don't want described-photo or transcribed-voice rows.
        if opts.text_only and not m.text:
            continue
        # `--by`: substring on sender_name (case-insensitive) OR exact sender_id.
        # Runs after enrichment so transcribed/described messages are still
        # attributed correctly (transcript inherits the original sender).
        if opts.sender_id is not None and m.sender_id != opts.sender_id:
            continue
        if sender_needle is not None:
            sname = (m.sender_name or "").casefold()
            if sender_needle not in sname:
                continue
        out.append(m)
    return out


# --------------------------------------------------------------------- dedupe

_SPACE_RE = re.compile(r"\s+")


def _normalize_text(text: str) -> str:
    t = text.strip().lower()
    return _SPACE_RE.sub(" ", t)


def dedupe(msgs: list[Message]) -> list[Message]:
    """Collapse repeated messages (forwards, memes). Preserves chronological order.

    First occurrence wins; duplicates bump the first message's `duplicates` counter.
    """
    seen: dict[str, Message] = {}
    order: list[str] = []
    for m in msgs:
        body = (m.text or m.transcript or "").strip()
        if not body:
            continue
        key = hashlib.sha1(_normalize_text(body).encode("utf-8")).hexdigest()
        if key in seen:
            seen[key].duplicates = (seen[key].duplicates or 0) + 1
        else:
            m.duplicates = 0
            seen[key] = m
            order.append(key)
    return [seen[k] for k in order]
