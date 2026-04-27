"""Chunking of messages into model-sized batches (spec §9.6)."""

from __future__ import annotations

from datetime import timedelta

from atg.analyzer.formatter import format_messages
from atg.models import Chunk, Message
from atg.util.logging import get_logger
from atg.util.tokens import count_tokens

log = get_logger(__name__)

# Context window estimates. Real limits can differ per model; we err conservatively.
MODEL_CONTEXT: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 128_000,
    "o3-mini": 128_000,
    "gpt-5": 200_000,
    "gpt-5.4": 1_000_000,
    "gpt-5.4-mini": 400_000,
    "gpt-5.4-nano": 400_000,
}

_UNKNOWN_MODEL_WARNED: set[str] = set()


def model_context_window(model: str) -> int:
    if model in MODEL_CONTEXT:
        return MODEL_CONTEXT[model]
    if model not in _UNKNOWN_MODEL_WARNED:
        _UNKNOWN_MODEL_WARNED.add(model)
        log.warning(
            "chunker.unknown_model",
            model=model,
            fallback=128_000,
            hint="add the model to atg/analyzer/chunker.py::MODEL_CONTEXT",
        )
    return 128_000


def _fmt_line(m: Message) -> str:
    # Re-use the same formatter so chunk boundaries line up exactly with rendered output.
    return format_messages([m]).split("\n")[-1]


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
    if max_chunk_input_tokens is not None and max_chunk_input_tokens > 0:
        # Subtract per-chunk overhead once — the cap should bound the
        # *whole request* (system + user_overhead + body + output reserve),
        # not just the body. Without this, a 35k cap on a request with
        # ~3k of overhead silently lets through ~38k-token requests.
        cap_budget = max_chunk_input_tokens - overhead - output_budget - safety_margin
        budget = min(budget, max(2000, cap_budget))
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
    soft_break = timedelta(minutes=soft_break_minutes)
    min_roll_tokens = max(soft_break_min_tokens, min(budget // 3, 4000))

    for m in msgs:
        line = _fmt_line(m) + "\n"
        t = count_tokens(line, model)
        if prev_date is not None:
            gap = m.date - prev_date
            if gap > soft_break and current.tokens >= min_roll_tokens:
                chunks.append(current)
                current = Chunk()
        if current.tokens + t > budget and current.messages:
            chunks.append(current)
            current = Chunk()
        current.messages.append(m)
        current.tokens += t
        prev_date = m.date

    if current.messages:
        chunks.append(current)
    return chunks
