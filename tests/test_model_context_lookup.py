"""Tests for non-OpenAI model context-window lookup.

Pre-prod regression: `MODEL_CONTEXT` only knew OpenAI ids, so every
Claude / Gemini / OpenRouter model fell to the 128k fallback. Opus 4.7
(1M ctx) was treated as 128k → ~8x more chunks than needed → ~8x the
spend and wall time. The fix wires `model_context_window` through
`ai.models.find_model()` so the per-provider catalog is the single
source of truth.
"""

from __future__ import annotations

from unread.analyzer.chunker import model_context_window


def test_claude_opus_returns_1m_context():
    """Claude Opus 4.7 advertises 1M ctx; without find_model() it fell
    to the 128k fallback."""
    assert model_context_window("claude-opus-4-7") == 1_000_000


def test_claude_sonnet_returns_200k_context():
    assert model_context_window("claude-sonnet-4-6") == 200_000


def test_gemini_pro_returns_1m_context():
    assert model_context_window("gemini-2.5-pro") == 1_000_000


def test_gemini_flash_lite_returns_1m_context():
    assert model_context_window("gemini-2.5-flash-lite") == 1_000_000


def test_openrouter_anthropic_alias_returns_correct_context():
    """OpenRouter `anthropic/claude-opus-4-7` should match the upstream
    catalog row's context window — not silently fall to 128k."""
    assert model_context_window("anthropic/claude-opus-4-7") == 1_000_000


def test_openai_gpt_5_4_mini_returns_400k_context():
    assert model_context_window("gpt-5.4-mini") == 400_000


def test_unknown_model_falls_back_to_128k():
    # Unknown model still falls back so the chunker doesn't blow up on
    # custom local-model names.
    assert model_context_window("some-experimental-model") == 128_000


def test_legacy_alias_still_resolves():
    # `gpt-4.1` is in the legacy MODEL_CONTEXT table but not in the
    # per-provider catalog. The lookup chain must consult the legacy
    # table after find_model() so older configs keep working.
    assert model_context_window("gpt-4.1") == 128_000
