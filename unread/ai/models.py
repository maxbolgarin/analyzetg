"""Per-provider model catalogue.

Single source of truth for the settings picker, default pricing, and
"is this model supported by this provider" checks. Refreshed against
the provider docs on **2026-05-01**:

  - OpenAI:   platform.openai.com/docs/pricing
  - Anthropic: docs.claude.com/en/docs/about-claude/models
  - Google:   ai.google.dev/pricing

Adding a model here makes it appear in `unread settings` (under the
matching provider) AND seeds a default pricing row for `unread stats`.
The user can still pick a custom model name at the picker — the
registry is a curated list, not a hard allow-list.

`role`:
  - `chat`   — a "smart" model used for the final reduce / answers.
  - `filter` — a cheap-and-fast model for per-chunk map / rerank.
  - `audio`  — Whisper-style transcription (OpenAI-only today).
  - `vision` — image understanding for `--enrich=image` (OpenAI-only).

Cached-input prices reflect the provider's prompt-cache *read* rate
(Anthropic: 0.1× input, OpenAI: 0.1× input, Google: ~0.25× input). They
are estimates — the orchestrator records the actual cached_tokens count
returned by the API, so cost reports stay accurate even if the multiplier
moves.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    label: str
    role: str  # "chat" | "filter" | "audio" | "vision"
    input_price: float = 0.0  # $ / 1M tokens (or $ / minute for audio)
    cached_price: float = 0.0  # $ / 1M tokens
    output_price: float = 0.0  # $ / 1M tokens


# ----------------------- OpenAI (refreshed 2026-05-01) ---------------------

_OPENAI_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo("gpt-5.5", "GPT-5.5 — flagship (1M ctx)", "chat", 5.00, 0.50, 30.00),
    ModelInfo("gpt-5.4", "GPT-5.4 — heavy reasoning", "chat", 2.50, 0.25, 15.00),
    ModelInfo("gpt-5.4-mini", "GPT-5.4 mini — balanced", "chat", 0.75, 0.075, 4.50),
    ModelInfo("gpt-5.4-nano", "GPT-5.4 nano — cheapest", "filter", 0.20, 0.02, 1.25),
    ModelInfo("gpt-4o", "GPT-4o — previous gen", "chat", 2.50, 1.25, 10.00),
    ModelInfo("gpt-4o-mini", "GPT-4o mini — vision default", "vision", 0.15, 0.075, 0.60),
    # Audio. `input_price` carries $/minute; cached/output are unused.
    ModelInfo("gpt-4o-mini-transcribe", "GPT-4o mini transcribe", "audio", 0.003),
    ModelInfo("gpt-4o-transcribe", "GPT-4o transcribe", "audio", 0.006),
    ModelInfo("whisper-1", "Whisper v1", "audio", 0.006),
)


# ----------------------- Anthropic (refreshed 2026-05-01) ------------------

_ANTHROPIC_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo("claude-opus-4-7", "Claude Opus 4.7 — most capable (1M ctx)", "chat", 5.00, 0.50, 25.00),
    ModelInfo("claude-sonnet-4-6", "Claude Sonnet 4.6 — balanced", "chat", 3.00, 0.30, 15.00),
    ModelInfo("claude-haiku-4-5", "Claude Haiku 4.5 — fast & cheap", "filter", 1.00, 0.10, 5.00),
)


# ----------------------- Google (refreshed 2026-05-01) ---------------------
#
# Pricing for prompts ≤200k tokens. Gemini bills a higher tier on
# >200k prompts; we surface the lower tier here since the analyzer
# chunks ahead of any single call ever crossing the threshold.

_GOOGLE_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo("gemini-3.1-pro-preview", "Gemini 3.1 Pro — frontier (preview)", "chat", 2.00, 0.50, 12.00),
    ModelInfo(
        "gemini-3.1-flash-lite-preview", "Gemini 3.1 Flash-Lite (preview)", "filter", 0.25, 0.0625, 1.50
    ),
    ModelInfo("gemini-2.5-pro", "Gemini 2.5 Pro — deep reasoning", "chat", 1.25, 0.31, 10.00),
    ModelInfo("gemini-2.5-flash", "Gemini 2.5 Flash — balanced", "chat", 0.30, 0.075, 2.50),
    ModelInfo("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite — cheapest", "filter", 0.10, 0.025, 0.40),
)


# ----------------------- OpenRouter ---------------------------------------
#
# OpenRouter routes to many backends; we list a curated cross-section of
# the most popular models. Users can always pick "Custom…" to enter any
# other vendor/model alias.

_OPENROUTER_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo("openai/gpt-5.5", "OpenRouter → GPT-5.5", "chat", 5.00, 0.50, 30.00),
    ModelInfo("openai/gpt-5.4-mini", "OpenRouter → GPT-5.4 mini", "chat", 0.75, 0.075, 4.50),
    ModelInfo("openai/gpt-5.4-nano", "OpenRouter → GPT-5.4 nano", "filter", 0.20, 0.02, 1.25),
    ModelInfo("anthropic/claude-opus-4-7", "OpenRouter → Claude Opus 4.7", "chat", 5.00, 0.50, 25.00),
    ModelInfo("anthropic/claude-sonnet-4-6", "OpenRouter → Claude Sonnet 4.6", "chat", 3.00, 0.30, 15.00),
    ModelInfo("anthropic/claude-haiku-4-5", "OpenRouter → Claude Haiku 4.5", "filter", 1.00, 0.10, 5.00),
    ModelInfo("google/gemini-2.5-flash", "OpenRouter → Gemini 2.5 Flash", "chat", 0.30, 0.075, 2.50),
    ModelInfo(
        "google/gemini-2.5-flash-lite", "OpenRouter → Gemini 2.5 Flash-Lite", "filter", 0.10, 0.025, 0.40
    ),
)


# ----------------------- Local --------------------------------------------
#
# Local servers (Ollama / LM Studio / vLLM) have no fixed catalog — the
# user-installed model name is whatever they pulled. Picker shows a
# Custom-only flow for this provider.

_LOCAL_MODELS: tuple[ModelInfo, ...] = ()


_REGISTRY: dict[str, tuple[ModelInfo, ...]] = {
    "openai": _OPENAI_MODELS,
    "anthropic": _ANTHROPIC_MODELS,
    "google": _GOOGLE_MODELS,
    "openrouter": _OPENROUTER_MODELS,
    "local": _LOCAL_MODELS,
}


def models_for_provider(provider: str, *, role: str | None = None) -> list[ModelInfo]:
    """Return the catalog for `provider`, optionally filtered by role.

    Unknown providers return an empty list — the caller falls back to a
    Custom-only picker. Role filtering is order-preserving so the picker
    presents models in the listed-here sequence (flagship → cheap).

    For chat / filter the filter is *advisory*: when `role="chat"` we
    include models tagged `filter` too, because users sometimes want to
    pin a budget model to the chat slot. The reverse isn't true — when
    asking for filter models we hide flagships to keep the picker focused
    on cheap options.
    """
    pool = _REGISTRY.get(provider.strip().lower(), ())
    if role is None:
        return list(pool)
    if role == "chat":
        return [m for m in pool if m.role in {"chat", "filter"}]
    if role == "filter":
        return [m for m in pool if m.role == "filter"]
    return [m for m in pool if m.role == role]


def all_known_models() -> list[ModelInfo]:
    """Flat list of every (provider, model) pair we ship pricing for."""
    seen: dict[str, ModelInfo] = {}
    for pool in _REGISTRY.values():
        for m in pool:
            # Same id can appear under multiple providers (e.g. OpenRouter
            # mirrors). Keep the first occurrence so vanilla OpenAI rows
            # win over `openai/...` aliases when both exist.
            seen.setdefault(m.id, m)
    return list(seen.values())


def find_model(model_id: str) -> ModelInfo | None:
    """Look up a model by id across every provider's catalog."""
    for pool in _REGISTRY.values():
        for m in pool:
            if m.id == model_id:
                return m
    return None


def supported_providers() -> tuple[str, ...]:
    """Provider names with a curated catalog (ordered for UI consistency)."""
    return ("openai", "anthropic", "google", "openrouter", "local")
