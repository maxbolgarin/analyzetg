"""Token counting via tiktoken, with graceful fallback for unknown models."""

from __future__ import annotations

from functools import lru_cache

import tiktoken


@lru_cache(maxsize=16)
def _encoding_for(model: str):
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        return tiktoken.get_encoding("o200k_base")


def count_tokens(text: str, model: str = "gpt-5.4") -> int:
    if not text:
        return 0
    return len(_encoding_for(model).encode(text))


def count_message_tokens(messages: list[dict], model: str = "gpt-5.4") -> int:
    """Rough estimate of total tokens for a list of OpenAI chat messages."""
    total = 0
    for m in messages:
        total += count_tokens(str(m.get("role", "")), model)
        total += count_tokens(str(m.get("content", "")), model)
        total += 4  # per-message overhead
    return total + 2
