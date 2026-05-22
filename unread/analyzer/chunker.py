"""Chunking of messages into model-sized batches (spec §9.6)."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import timedelta

from unread.ai.models import find_model
from unread.analyzer.formatter import format_messages
from unread.models import Chunk, Message
from unread.util.logging import get_logger
from unread.util.tokens import count_tokens

log = get_logger(__name__)

# Sentence-boundary splitter for the oversized-message path. Matches a
# whitespace gap that follows a sentence terminator (`.`, `!`, `?`) and
# precedes the start of the next sentence — uppercase Latin / Cyrillic
# or an opening quotation mark. Deliberately loose: better to over-split
# (more chunks) than under-split (a slice that still busts the budget).
_SENTENCE_SPLIT = re.compile(r'(?<=[.!?])\s+(?=[A-ZА-Я"«])')

# Suffix appended when a single sentence exceeds the budget alone and
# we have to mid-sentence truncate. A literal char marker — kept short
# so it doesn't itself eat much of the budget.
_TRUNC_MARKER = "…[truncated]"

# Legacy fallback table for OpenAI ids predating the per-provider catalog
# in unread/ai/models.py. New entries should land in `ai/models.py` as a
# `ModelInfo.context_window=` instead, so the chunker, settings picker,
# and pricing table all read from the same source.
MODEL_CONTEXT: dict[str, int] = {
    "gpt-4.1": 128_000,
    "o3-mini": 128_000,
    "gpt-5": 200_000,
}

_UNKNOWN_MODEL_WARNED: set[str] = set()


def model_context_window(model: str) -> int:
    """Return the input-context window for `model`, defaulting to 128k.

    Lookup order:
      1. `ai.models.find_model()` — covers OpenAI / Anthropic / Google
         / OpenRouter ids registered in the per-provider catalog.
      2. Legacy `MODEL_CONTEXT` table for any older alias.
      3. 128k fallback with a one-time warning.

    Without (1), every Claude / Gemini id silently fell to 128k — the
    chunker over-chunked Opus 4.7 (1M ctx) by ~8x, multiplying both
    spend and wall time. The warning fires once per unknown model id
    per process so log volume stays sane.
    """
    info = find_model(model)
    if info is not None and info.context_window > 0:
        return info.context_window
    if model in MODEL_CONTEXT:
        return MODEL_CONTEXT[model]
    if model not in _UNKNOWN_MODEL_WARNED:
        _UNKNOWN_MODEL_WARNED.add(model)
        log.warning(
            "chunker.unknown_model",
            model=model,
            fallback=128_000,
            hint="register the model in unread/ai/models.py with a context_window",
        )
    return 128_000


def _fmt_line(m: Message) -> str:
    """Render exactly the per-message section the formatter would emit.

    `format_messages([m])` prepends a small `Messages: N` preamble + a
    blank line; everything from the first message header onward is the
    per-message rendering (now multi-line because of the untrusted-
    content sentinels). Strip the preamble and return the rest so the
    chunker's token math matches what eventually ships in the prompt.
    """
    rendered = format_messages([m])
    if not rendered:
        return ""
    lines = rendered.split("\n")
    # Find the first line that starts a message header — `[<ts> #<id>]` —
    # everything from there to the end is the message rendering.
    for i, line in enumerate(lines):
        if line.startswith("[") and "#" in line:
            return "\n".join(lines[i:])
    # Defensive: no header pattern matched (msg had no body and was
    # dropped). Return the whole rendering so token math is conservative.
    return rendered


def _stripped_clone(m: Message, body: str, *, header_suffix: str = "") -> Message:
    """Clone `m` keeping all metadata, replacing the body with `body`.

    The composed body in `formatter._body()` glues `text` ++ image
    description ++ extracted doc text ++ transcript. To put a custom
    body string in front of the formatter we have to set `text` to that
    body AND clear every other body source so they don't bleed back in.
    """
    return replace(
        m,
        text=body,
        image_description=None,
        extracted_text=None,
        transcript=None,
        link_summaries=None,  # link summaries piggy-back only on the FIRST part
        header_suffix=header_suffix,
    )


def _split_sentences(body: str) -> list[str]:
    """Split a body into sentence-ish fragments at `[.!?]\\s+[A-ZА-Я"«]`.

    Returns at least one element even when the body has no detectable
    boundaries (a single long blob). Whitespace at fragment boundaries
    is normalized — leading/trailing space stripped — so re-joining
    with `" "` reconstructs a near-byte-equivalent body.
    """
    parts = _SENTENCE_SPLIT.split(body)
    return [p.strip() for p in parts if p.strip()]


def _truncate_body_to_budget(
    sentence: str,
    template: Message,
    budget: int,
    model: str,
) -> str:
    """Hard-truncate a body that's still oversized after sentence split.

    Binary-search the largest character prefix of `sentence` such that
    the rendered line (header + sentinels + prefix + truncation marker)
    still fits in `budget`. The marker `…[truncated]` is appended so the
    model can tell content was cut. Always returns a non-empty body —
    if even one character + marker would overflow, we surface the
    pathological condition by returning just the marker (provider will
    likely 4xx, but the chunker has done its best).
    """
    lo, hi = 0, len(sentence)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = sentence[:mid].rstrip() + _TRUNC_MARKER
        clone = _stripped_clone(template, candidate)
        if count_tokens(_fmt_line(clone), model) <= budget:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best == 0:
        return _TRUNC_MARKER
    return sentence[:best].rstrip() + _TRUNC_MARKER


def _split_oversize(m: Message, budget: int, model: str) -> list[Message]:
    """Split an oversize message into sub-messages that each fit in budget.

    Strategy: split body at sentence boundaries, greedily pack
    sentences into the largest fragments that fit, mid-sentence-
    truncate any single sentence that still overflows. Each returned
    clone preserves the original `msg_id`, sender, timestamp, and tags
    — only the body content varies — so citations issued by the model
    against any sub-chunk resolve back to the same original message.

    Sub-messages 2..N carry a `(continued K/N)` `header_suffix` so the
    rendered header line tells the model "this is a slice of the
    same #msg_id you saw before". The first sub-message keeps the
    natural header (no prefix) and is the only one that carries the
    original `link_summaries` block — splitting them across continuations
    would duplicate the (often large) summary on every part.

    Implementation note: the greedy pack uses *cumulative per-sentence
    token sums* + a fixed per-message rendering overhead to decide
    where to flush, instead of re-rendering the growing candidate body
    every iteration. With N sentences that quadratic re-render cost
    was the difference between the function returning instantly on a
    moderate body vs. taking minutes on a multi-megabyte one.
    """
    body = format_messages([m])  # render through the full formatter to
    # extract the composed body inside the sentinel block. We can't just
    # call `_body(m)` because it isn't re-exported and the rendered line
    # is already the source of truth for token math.
    # Pull the body out from between the first sentinel pair.
    open_marker = f"<<<UNTRUSTED_CONTENT id={m.msg_id}>>>"
    close_marker = "<<<END_UNTRUSTED>>>"
    o = body.find(open_marker)
    c = body.find(close_marker, o + len(open_marker)) if o >= 0 else -1
    if o < 0 or c < 0:
        # Defensive: no sentinel pair found — message had no body.
        # Hand back the original so the caller's "raise / log" branch
        # still surfaces the issue rather than silently dropping it.
        return [m]
    raw_body = body[o + len(open_marker) + 1 : c].rstrip("\n")

    sentences = _split_sentences(raw_body) or [raw_body]

    # Per-message rendering overhead: count tokens of the rendering
    # with a 1-char body, subtract that 1 token, and use the remainder
    # as the fixed cost added to any body slice. This way we only
    # re-render once instead of once per sentence.
    probe = _stripped_clone(m, "x")
    overhead = max(0, count_tokens(_fmt_line(probe), model) - 1)
    body_budget = max(1, budget - overhead)

    # Per-sentence token counts. Tokenizing 16k+ short strings one at a
    # time is the hot path on multi-megabyte bodies; estimate via byte
    # ratio against a single one-shot count of the joined corpus.
    # Cheap, deterministic, and good enough for the bucket boundary.
    # Slight over-estimate (90% of measured chars/token) so a
    # mis-estimate biases towards smaller-than-budget chunks rather
    # than the budget-busting direction.
    joined = " ".join(sentences)
    total_body_tokens = count_tokens(joined, model)
    total_chars = sum(len(s) for s in sentences) + max(0, len(sentences) - 1)
    if total_chars <= 0 or total_body_tokens <= 0:
        chars_per_token = 4.0
    else:
        chars_per_token = max(1.0, (total_chars / total_body_tokens) * 0.9)
    sentence_tokens = [max(1, int(len(s) / chars_per_token) + 1) for s in sentences]
    # +1 token approximates the joining whitespace between sentences.
    join_cost = 1

    parts: list[str] = []
    current_sents: list[str] = []
    current_tokens = 0
    for sent, tok in zip(sentences, sentence_tokens, strict=True):
        added = tok + (join_cost if current_sents else 0)
        if current_tokens + added <= body_budget:
            current_sents.append(sent)
            current_tokens += added
            continue
        # `sent` would overflow the current part. Flush what we have.
        if current_sents:
            parts.append(" ".join(current_sents))
            current_sents = []
            current_tokens = 0
        # Try the sentence on its own.
        if tok <= body_budget:
            current_sents = [sent]
            current_tokens = tok
        else:
            # Single sentence overflows — mid-sentence truncate. Falls
            # back to a real-render binary search (rare path).
            parts.append(_truncate_body_to_budget(sent, m, budget, model))
    if current_sents:
        parts.append(" ".join(current_sents))

    if not parts:
        # Should be unreachable (sentences was non-empty), but be safe.
        return [m]

    total = len(parts)
    clones: list[Message] = []
    for i, part_body in enumerate(parts, 1):
        suffix = f"(continued {i}/{total})" if i > 1 else ""
        clone = _stripped_clone(m, part_body, header_suffix=suffix)
        # Only the first sub-message carries the original link summaries;
        # they're independent of the body slice and we don't want
        # duplication on every part.
        if i == 1:
            clone.link_summaries = m.link_summaries
        clones.append(clone)
    return clones


def build_chunks(
    msgs: list[Message],
    *,
    model: str,
    system_prompt: str,
    user_overhead: str,
    output_budget: int,
    safety_margin: int = 2000,
    soft_break_minutes: int = 30,
    soft_break_min_tokens: int = 500,
    max_chunk_input_tokens: int | None = None,
) -> list[Chunk]:
    """Greedily pack messages into chunks under the model's token budget.

    Soft-break: when the pause between consecutive messages exceeds
    `soft_break_minutes` AND the current chunk has at least
    `soft_break_min_tokens` worth of content, roll over into a new chunk
    even if the hard budget isn't full yet.

    `max_chunk_input_tokens` (optional) is an extra cap *below* the model's
    full context window. Use it to force map-reduce for huge inputs so a
    single request doesn't blow per-minute TPM ceilings or wash out the
    LLM's focus. None = use the full effective budget.
    """
    if not msgs:
        return []
    context = model_context_window(model)
    overhead = count_tokens(system_prompt, model) + count_tokens(user_overhead, model)
    budget = context - overhead - output_budget - safety_margin
    capped_by_preset = False
    if max_chunk_input_tokens is not None and max_chunk_input_tokens > 0:
        # Subtract per-chunk overhead once — the cap should bound the
        # *whole request* (system + user_overhead + body + output reserve),
        # not just the body. Without this, a 35k cap on a request with
        # ~3k of overhead silently lets through ~38k-token requests.
        cap_budget = max_chunk_input_tokens - overhead - output_budget - safety_margin
        if cap_budget < budget:
            capped_by_preset = True
        budget = min(budget, max(2000, cap_budget))
    log.debug(
        "chunker.budget",
        model=model,
        context=context,
        overhead=overhead,
        output_budget=output_budget,
        safety_margin=safety_margin,
        max_chunk_input_tokens=max_chunk_input_tokens,
        capped_by_preset=capped_by_preset,
        budget=budget,
        msgs=len(msgs),
    )
    if budget < 2000:
        # Silently clamping to 500 here used to produce 200+ pathological
        # tiny chunks and a runaway bill. Surface the misconfiguration so
        # the user can either lower output_budget_tokens or switch to a
        # bigger-context model rather than discover it from the invoice.
        raise ValueError(
            f"Chunk token budget too small ({budget}) for model {model!r} "
            f"(context={context}, overhead={overhead}, output_budget={output_budget}, "
            f"safety_margin={safety_margin}). "
            "Reduce preset.output_budget_tokens or pick a larger-context model."
        )

    chunks: list[Chunk] = []
    current = Chunk()
    prev_date = None
    soft_breaks = 0
    hard_breaks = 0
    soft_break = timedelta(minutes=soft_break_minutes)
    min_roll_tokens = max(soft_break_min_tokens, min(budget // 3, 4000))

    # Expand oversize messages into a sequence of in-budget sub-messages
    # before the packing loop runs. The expansion is per-message so a
    # mix of normal and oversize inputs only pays the rendering cost
    # once for each oversize entry. Normal messages pass through
    # unchanged. The expanded list still respects the original
    # chronological order so soft-break logic stays valid.
    expanded: list[Message] = []
    for m in msgs:
        line = _fmt_line(m)
        if not line:
            continue
        if count_tokens(line + "\n", model) <= budget:
            expanded.append(m)
            continue
        # Oversize — split (or truncate the worst offenders).
        sub_msgs = _split_oversize(m, budget, model)
        if len(sub_msgs) == 1 and sub_msgs[0] is m:
            # Defensive: split returned the original (no body to slice).
            # Surface the issue and keep the original in the chunk so
            # the model still sees something even if the call 4xxes.
            log.warning(
                "chunker.message_exceeds_budget",
                msg_id=m.msg_id,
                chat_id=m.chat_id,
                tokens=count_tokens(line + "\n", model),
                budget=budget,
                hint="Could not split — message had no extractable body.",
            )
            expanded.append(m)
            continue
        truncated_parts = sum(1 for s in sub_msgs if s.text and s.text.endswith(_TRUNC_MARKER))
        if truncated_parts:
            log.info(
                "chunker.message_truncated",
                msg_id=m.msg_id,
                chat_id=m.chat_id,
                budget=budget,
                truncated_parts=truncated_parts,
                total_parts=len(sub_msgs),
                hint=(
                    "A sentence inside this message was longer than the "
                    "per-chunk budget on its own and got mid-sentence "
                    "truncated. Likely a long URL, base64 blob, or "
                    "unbroken text wall."
                ),
            )
        else:
            log.info(
                "chunker.message_split",
                msg_id=m.msg_id,
                chat_id=m.chat_id,
                budget=budget,
                parts=len(sub_msgs),
                hint=(
                    "Single message body exceeded the per-chunk budget; "
                    "split into N sentence-aligned sub-chunks. Citations "
                    "still resolve to the original msg_id."
                ),
            )
        expanded.extend(sub_msgs)

    for m in expanded:
        line = _fmt_line(m) + "\n"
        t = count_tokens(line, model)
        if prev_date is not None:
            gap = m.date - prev_date
            if gap > soft_break and current.tokens >= min_roll_tokens:
                chunks.append(current)
                current = Chunk()
                soft_breaks += 1
        if current.tokens + t > budget and current.messages:
            chunks.append(current)
            current = Chunk()
            hard_breaks += 1
        current.messages.append(m)
        current.tokens += t
        prev_date = m.date

    if current.messages:
        chunks.append(current)
    if chunks:
        log.debug(
            "chunker.packed",
            chunks=len(chunks),
            soft_breaks=soft_breaks,
            hard_breaks=hard_breaks,
            tokens_min=min(c.tokens for c in chunks),
            tokens_max=max(c.tokens for c in chunks),
            tokens_total=sum(c.tokens for c in chunks),
            msgs_min=min(len(c.messages) for c in chunks),
            msgs_max=max(len(c.messages) for c in chunks),
        )
    return chunks
