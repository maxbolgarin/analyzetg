"""Token counting via tiktoken, with graceful fallback for unknown models
and for environments where tiktoken can't reach its tokenizer blob.

The `tiktoken.encoding_for_model` / `get_encoding` calls download the
encoding files on first use. On a CI runner whose egress to
`openaipublic.blob.core.windows.net` is blocked (some corporate
firewalls + Azure regional outages have hit us in the wild), every
chunker / cost-estimate / dump call would otherwise crash. Fall back
to a character-based heuristic so the application stays functional —
chunk sizes will be approximate, but the user gets a working CLI plus
a one-time warning to install the cache or unblock the URL.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import tiktoken

from unread.util.logging import get_logger

log = get_logger(__name__)

# Average bytes per token for the BPE tokenizers we use. ~4 for English
# / Latin scripts, ~2 for Cyrillic / CJK. Pick the lower bound so the
# fallback OVER-estimates token counts (we'd rather chunk a little
# smaller than risk a 4xx). Used only when tiktoken can't load.
_FALLBACK_CHARS_PER_TOKEN = 3.0
_FALLBACK_WARNED = False


class _CharFallbackEncoding:
    """Stand-in for `tiktoken.Encoding` when the real tokenizer can't load.

    Only `.encode(text) -> list[int]` is needed by `count_tokens` —
    we return a list of the right length, contents irrelevant.
    """

    def encode(self, text: str) -> list[int]:
        return [0] * max(1, int(len(text) / _FALLBACK_CHARS_PER_TOKEN))


def _maybe_warn_fallback(reason: str) -> None:
    global _FALLBACK_WARNED
    if _FALLBACK_WARNED:
        return
    _FALLBACK_WARNED = True
    log.warning(
        "tokens.tiktoken_unavailable",
        reason=reason,
        hint=(
            "Falling back to a char/3 heuristic. Token counts and chunk "
            "sizes will be approximate. To restore exact counting, ensure "
            "egress to openaipublic.blob.core.windows.net is permitted."
        ),
    )


@lru_cache(maxsize=16)
def _encoding_for(model: str) -> Any:
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        # Unknown model id — try the universal fallback encoding.
        try:
            return tiktoken.get_encoding("o200k_base")
        except Exception as e:
            _maybe_warn_fallback(f"get_encoding failed: {type(e).__name__}")
            return _CharFallbackEncoding()
    except Exception as e:
        # tiktoken raises requests.HTTPError (and assorted others) when
        # the blob fetch fails. The exception type isn't part of
        # tiktoken's public API so catch broadly and fall back.
        _maybe_warn_fallback(f"encoding_for_model failed: {type(e).__name__}")
        return _CharFallbackEncoding()


def count_tokens(text: str, model: str = "gpt-5.4") -> int:
    if not text:
        return 0
    try:
        return len(_encoding_for(model).encode(text))
    except Exception as e:
        # Defense-in-depth: if even the fallback path raises (shouldn't
        # happen), return a heuristic instead of bubbling.
        _maybe_warn_fallback(f"encode failed: {type(e).__name__}")
        return max(1, int(len(text) / _FALLBACK_CHARS_PER_TOKEN))


def count_message_tokens(messages: list[dict], model: str = "gpt-5.4") -> int:
    """Rough estimate of total tokens for a list of OpenAI chat messages."""
    total = 0
    for m in messages:
        total += count_tokens(str(m.get("role", "")), model)
        total += count_tokens(str(m.get("content", "")), model)
        total += 4  # per-message overhead
    return total + 2
