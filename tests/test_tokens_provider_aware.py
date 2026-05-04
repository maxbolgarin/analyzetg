"""Provider-aware token counting (pre-prod blocker #8).

tiktoken under-counts Claude / Gemini prompts by 10-25%. We multiply
the raw count by a per-provider safety margin so the chunker stays
on the safe side of context limits without paying for a network
round-trip in the hot loop.
"""

from __future__ import annotations

import math

import pytest

from unread.ai.models import (
    PROVIDER_TOKEN_SAFETY_MARGIN,
    provider_for_model,
)
from unread.util.tokens import count_tokens


@pytest.mark.parametrize(
    ("model_id", "expected_provider"),
    [
        ("gpt-5.4", "openai"),
        ("gpt-5.4-mini", "openai"),
        ("gpt-4o-mini", "openai"),
        ("o3-mini", "openai"),
        ("claude-opus-4-7", "anthropic"),
        ("claude-sonnet-4-6", "anthropic"),
        ("claude-haiku-4-5", "anthropic"),
        ("gemini-2.5-pro", "google"),
        ("gemini-2.5-flash", "google"),
        # OpenRouter aliases peel down to the inner vendor.
        ("openai/gpt-5.4-mini", "openai"),
        ("anthropic/claude-opus-4-7", "anthropic"),
        ("google/gemini-2.5-flash", "google"),
        # Heuristic fallbacks for ids not in the catalog.
        ("claude-future-model", "anthropic"),
        ("gemini-future-model", "google"),
        ("gpt-future-model", "openai"),
        # Truly unknown — no provider.
        ("acme-llm-9000", None),
    ],
)
def test_provider_for_model_resolves_correctly(model_id: str, expected_provider: str | None):
    assert provider_for_model(model_id) == expected_provider


def test_count_tokens_openai_no_margin():
    """OpenAI models use tiktoken directly — no safety multiplier."""
    text = "The quick brown fox jumps over the lazy dog."
    n = count_tokens(text, model="gpt-5.4")
    # Sanity: we know this sentence is ~10 tokens via tiktoken's o200k.
    assert 5 < n < 20


def test_count_tokens_anthropic_applies_margin():
    """Claude models get a 1.25× safety margin so the chunker is conservative."""
    text = "The quick brown fox jumps over the lazy dog."
    openai_n = count_tokens(text, model="gpt-5.4")
    anthropic_n = count_tokens(text, model="claude-opus-4-7")
    # Margin is exactly the registry value, rounded up.
    expected = math.ceil(openai_n * PROVIDER_TOKEN_SAFETY_MARGIN["anthropic"])
    assert anthropic_n == expected
    assert anthropic_n > openai_n


def test_count_tokens_google_applies_margin():
    text = "The quick brown fox jumps over the lazy dog."
    openai_n = count_tokens(text, model="gpt-5.4")
    google_n = count_tokens(text, model="gemini-2.5-pro")
    expected = math.ceil(openai_n * PROVIDER_TOKEN_SAFETY_MARGIN["google"])
    assert google_n == expected
    assert google_n > openai_n


def test_count_tokens_empty_string_zero_margin_unaffected():
    """Empty text short-circuits — never multiply zero by anything."""
    assert count_tokens("", model="claude-opus-4-7") == 0
    assert count_tokens("", model="gpt-5.4") == 0


def test_count_tokens_unknown_model_no_margin():
    """A custom local model id with no provider hint stays at 1.0×."""
    text = "hello world"
    base = count_tokens(text, model="gpt-5.4")
    custom = count_tokens(text, model="acme-llm-9000")
    # tiktoken falls back to o200k_base for unknown ids, same encoding
    # we use for gpt-5.4 → byte-for-byte identical token count.
    assert custom == base


def test_count_tokens_safety_margin_rounds_up():
    """A 4-token tiktoken count × 1.25 = 5.0 — round-up matters at the boundary."""
    # Pick a one-character text that tiktoken counts as 1 token; 1 *
    # 1.25 = 1.25 → ceil = 2.
    n = count_tokens("a", model="claude-opus-4-7")
    assert n == 2
