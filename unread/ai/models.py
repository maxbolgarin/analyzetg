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
    # Effective input-context window in tokens. 0 means "unknown — use the
    # 128k fallback". Wired through `model_context_window()` so the
    # chunker sizes prompts correctly for Claude / Gemini, not just
    # OpenAI. Refreshed against vendor docs 2026-05-01.
    context_window: int = 0
    # Hard cap on a single completion's `max_tokens` (output tokens).
    # 0 means "unknown — use the orchestrator's 16k fallback". Used by
    # `analyzer.openai_client.chat_complete` to bound the truncation
    # retry: bumping above the per-model cap just guarantees a 4xx after
    # the user already paid for the prompt (e.g. Gemini Flash caps at
    # 8192 — doubling 4000→8000 is fine, doubling 5000→10000 is not).
    # Audio / vision-only entries leave this at 0; they're never used
    # for chat completions.
    max_output_tokens: int = 0
    # OpenAI reasoning-class models (o-series, gpt-5 family, including
    # the mini / nano variants) reject any `temperature` other than the
    # default `1.0` with a 400. The OpenAI adapter drops `temperature`
    # from the request when this flag is True. Anthropic / Google models
    # leave this False; their reasoning toggles are different shapes
    # (extended thinking, etc.) and don't constrain `temperature`.
    reasoning: bool = False


# ----------------------- OpenAI (refreshed 2026-05-01) ---------------------

_OPENAI_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo(
        "gpt-5.5",
        "GPT-5.5 — flagship (1M ctx)",
        "chat",
        5.00,
        0.50,
        30.00,
        context_window=1_000_000,
        max_output_tokens=16_384,
        reasoning=True,
    ),
    ModelInfo(
        "gpt-5.4",
        "GPT-5.4 — heavy reasoning",
        "chat",
        2.50,
        0.25,
        15.00,
        context_window=1_000_000,
        max_output_tokens=16_384,
        reasoning=True,
    ),
    ModelInfo(
        "gpt-5.4-mini",
        "GPT-5.4 mini — balanced",
        "chat",
        0.75,
        0.075,
        4.50,
        context_window=400_000,
        max_output_tokens=16_384,
        reasoning=True,
    ),
    ModelInfo(
        "gpt-5.4-nano",
        "GPT-5.4 nano — cheapest",
        "filter",
        0.20,
        0.02,
        1.25,
        context_window=400_000,
        max_output_tokens=16_384,
        reasoning=True,
    ),
    ModelInfo(
        "gpt-4o",
        "GPT-4o — previous gen",
        "chat",
        2.50,
        1.25,
        10.00,
        context_window=128_000,
        max_output_tokens=16_384,
    ),
    ModelInfo(
        "gpt-4o-mini",
        "GPT-4o mini — vision default",
        "vision",
        0.15,
        0.075,
        0.60,
        context_window=128_000,
        max_output_tokens=16_384,
    ),
    # Audio. `input_price` carries $/minute; cached/output are unused;
    # context window is irrelevant (file-based input). `max_output_tokens`
    # left at 0 since the chat orchestrator never picks an audio model.
    ModelInfo("gpt-4o-mini-transcribe", "GPT-4o mini transcribe", "audio", 0.003),
    ModelInfo("gpt-4o-transcribe", "GPT-4o transcribe", "audio", 0.006),
    ModelInfo("whisper-1", "Whisper v1", "audio", 0.006),
)


# ----------------------- Anthropic (refreshed 2026-05-01) ------------------

_ANTHROPIC_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo(
        "claude-opus-4-7",
        "Claude Opus 4.7 — most capable (1M ctx)",
        "chat",
        5.00,
        0.50,
        25.00,
        context_window=1_000_000,
        max_output_tokens=16_384,
    ),
    ModelInfo(
        "claude-sonnet-4-6",
        "Claude Sonnet 4.6 — balanced",
        "chat",
        3.00,
        0.30,
        15.00,
        context_window=200_000,
        max_output_tokens=16_384,
    ),
    ModelInfo(
        "claude-haiku-4-5",
        "Claude Haiku 4.5 — fast & cheap",
        "filter",
        1.00,
        0.10,
        5.00,
        context_window=200_000,
        max_output_tokens=8_192,
    ),
)


# ----------------------- Google (refreshed 2026-05-01) ---------------------
#
# Pricing for prompts ≤200k tokens. Gemini bills a higher tier on
# >200k prompts; we surface the lower tier here since the analyzer
# chunks ahead of any single call ever crossing the threshold.

_GOOGLE_MODELS: tuple[ModelInfo, ...] = (
    ModelInfo(
        "gemini-3.1-pro-preview",
        "Gemini 3.1 Pro — frontier (preview)",
        "chat",
        2.00,
        0.50,
        12.00,
        context_window=1_000_000,
        max_output_tokens=32_768,
    ),
    ModelInfo(
        "gemini-3.1-flash-lite-preview",
        "Gemini 3.1 Flash-Lite (preview)",
        "filter",
        0.25,
        0.0625,
        1.50,
        context_window=1_000_000,
        max_output_tokens=8_192,
    ),
    ModelInfo(
        "gemini-2.5-pro",
        "Gemini 2.5 Pro — deep reasoning",
        "chat",
        1.25,
        0.31,
        10.00,
        context_window=1_000_000,
        max_output_tokens=32_768,
    ),
    ModelInfo(
        "gemini-2.5-flash",
        "Gemini 2.5 Flash — balanced",
        "chat",
        0.30,
        0.075,
        2.50,
        context_window=1_000_000,
        max_output_tokens=8_192,
    ),
    ModelInfo(
        "gemini-2.5-flash-lite",
        "Gemini 2.5 Flash-Lite — cheapest",
        "filter",
        0.10,
        0.025,
        0.40,
        context_window=1_000_000,
        max_output_tokens=8_192,
    ),
)


# ----------------------- OpenRouter ---------------------------------------
#
# OpenRouter routes to many backends; we list a curated cross-section of
# the most popular models. Users can always pick "Custom…" to enter any
# other vendor/model alias.

_OPENROUTER_MODELS: tuple[ModelInfo, ...] = (
    # OpenRouter aliases mirror the underlying model's max_output_tokens
    # cap (claude-opus → 16384, gemini-flash → 8192, etc.) — the router
    # forwards the request to the upstream vendor whose limits are what
    # actually matter. `reasoning=True` for the gpt-5 aliases for the
    # same reason: OpenRouter routes them to OpenAI's reasoning endpoint.
    ModelInfo(
        "openai/gpt-5.5",
        "OpenRouter → GPT-5.5",
        "chat",
        5.00,
        0.50,
        30.00,
        context_window=1_000_000,
        max_output_tokens=16_384,
        reasoning=True,
    ),
    ModelInfo(
        "openai/gpt-5.4-mini",
        "OpenRouter → GPT-5.4 mini",
        "chat",
        0.75,
        0.075,
        4.50,
        context_window=400_000,
        max_output_tokens=16_384,
        reasoning=True,
    ),
    ModelInfo(
        "openai/gpt-5.4-nano",
        "OpenRouter → GPT-5.4 nano",
        "filter",
        0.20,
        0.02,
        1.25,
        context_window=400_000,
        max_output_tokens=16_384,
        reasoning=True,
    ),
    ModelInfo(
        "anthropic/claude-opus-4-7",
        "OpenRouter → Claude Opus 4.7",
        "chat",
        5.00,
        0.50,
        25.00,
        context_window=1_000_000,
        max_output_tokens=16_384,
    ),
    ModelInfo(
        "anthropic/claude-sonnet-4-6",
        "OpenRouter → Claude Sonnet 4.6",
        "chat",
        3.00,
        0.30,
        15.00,
        context_window=200_000,
        max_output_tokens=16_384,
    ),
    ModelInfo(
        "anthropic/claude-haiku-4-5",
        "OpenRouter → Claude Haiku 4.5",
        "filter",
        1.00,
        0.10,
        5.00,
        context_window=200_000,
        max_output_tokens=8_192,
    ),
    ModelInfo(
        "google/gemini-2.5-flash",
        "OpenRouter → Gemini 2.5 Flash",
        "chat",
        0.30,
        0.075,
        2.50,
        context_window=1_000_000,
        max_output_tokens=8_192,
    ),
    ModelInfo(
        "google/gemini-2.5-flash-lite",
        "OpenRouter → Gemini 2.5 Flash-Lite",
        "filter",
        0.10,
        0.025,
        0.40,
        context_window=1_000_000,
        max_output_tokens=8_192,
    ),
    # Audio. OpenRouter exposes Whisper-class endpoints under the
    # `openai/...` namespace; pricing mirrors the upstream provider.
    # Useful for users running Anthropic / Google as their chat slot
    # who still want OpenAI-quality transcription via OpenRouter's key.
    ModelInfo("openai/whisper-1", "OpenRouter → Whisper v1", "audio", 0.006),
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


# IDs of chat-class models that also accept image input. The vision
# picker folds these in alongside any `role="vision"` entries so users
# of Anthropic / Google / OpenRouter can pick "claude-sonnet-4-6 for
# image" without needing a parallel vision-only catalog entry.
_VISION_CAPABLE_IDS: frozenset[str] = frozenset(
    {
        # OpenAI flagships (vision via chat completions image_url).
        "gpt-5.5",
        "gpt-5.4-mini",
        "gpt-5.4-nano",
        "gpt-4o",
        # Anthropic — every modern Claude accepts image blocks.
        "claude-opus-4-7",
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        # Google — all Gemini 2.5 / 3.1 entries accept image parts.
        "gemini-3.1-pro-preview",
        "gemini-3.1-flash-lite-preview",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        # OpenRouter mirrors — vendor-prefixed.
        "openai/gpt-5.5",
        "openai/gpt-5.4-mini",
        "openai/gpt-5.4-nano",
        "openai/gpt-4o-mini",
        "anthropic/claude-opus-4-7",
        "anthropic/claude-sonnet-4-6",
        "anthropic/claude-haiku-4-5",
        "google/gemini-2.5-flash",
        "google/gemini-2.5-flash-lite",
    }
)


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

    For `role="vision"` we include the explicit `role="vision"` entry
    (e.g. OpenAI's gpt-4o-mini) plus every chat-class model that's known
    to accept image input via :data:`_VISION_CAPABLE_IDS`. This avoids
    duplicating Anthropic / Google catalog entries just to expose them
    under the vision picker.
    """
    pool = _REGISTRY.get(provider.strip().lower(), ())
    if role is None:
        return list(pool)
    if role == "chat":
        return [m for m in pool if m.role in {"chat", "filter"}]
    if role == "filter":
        return [m for m in pool if m.role == "filter"]
    if role == "vision":
        return [m for m in pool if m.role == "vision" or m.id in _VISION_CAPABLE_IDS]
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


def provider_for_model(model_id: str) -> str | None:
    """Return the canonical provider name for ``model_id`` (or None).

    Resolution order (vendor prefix wins over catalog hit so OpenRouter
    aliases like ``anthropic/claude-opus-4-7`` route to the underlying
    vendor's tokenizer / safety margin, not to OpenRouter's bucket):

      1. ``vendor/...`` prefix (OpenRouter convention) — peel off the vendor.
      2. Exact catalog hit — return the provider whose pool contains it.
      3. Heuristic on the bare id — ``claude*``/``anthropic*`` → anthropic,
         ``gemini*``/``google*`` → google, ``gpt*``/``o1*``/``o3*``/``o4*``
         → openai, otherwise None.

    Used by token counting to apply a per-provider safety margin without
    requiring the user to maintain a registry entry for every Claude /
    Gemini variant they might pass on the CLI.
    """
    raw = (model_id or "").strip()
    if not raw:
        return None
    lower = raw.lower()
    # 1. vendor/...  prefix — OpenRouter style. The semantic provider
    # is the vendor (the model behind the OpenRouter facade), not the
    # router itself, because token counting wants the underlying
    # tokenizer's safety margin.
    if "/" in raw:
        vendor = raw.split("/", 1)[0].lower()
        if vendor in _REGISTRY or vendor in {"anthropic", "google", "openai"}:
            return vendor
    # 2. Exact catalog match
    for provider, pool in _REGISTRY.items():
        if any(m.id.lower() == lower for m in pool):
            return provider
    # 3. Heuristics
    if lower.startswith(("claude", "anthropic")):
        return "anthropic"
    if lower.startswith(("gemini", "google")):
        return "google"
    if lower.startswith(("gpt", "o1", "o3", "o4", "chatgpt")):
        return "openai"
    return None


def supported_providers() -> tuple[str, ...]:
    """Provider names with a curated catalog (ordered for UI consistency)."""
    return ("openai", "anthropic", "google", "openrouter", "local")


# Per-provider safety multiplier applied to tiktoken counts. tiktoken
# uses OpenAI's BPE encodings; Claude and Gemini tokenize the same
# text into ~10-25% more tokens (different vocab, different merges).
# Bumping the count keeps the chunker on the safe side of provider
# context limits without resorting to network calls in the hot loop.
# OpenAI / OpenRouter / local: 1.0 (tiktoken is exact for OpenAI; for
# OpenRouter we trust the underlying-model heuristic, which already
# routes claude/gemini ids to the anthropic/google bucket).
PROVIDER_TOKEN_SAFETY_MARGIN: dict[str, float] = {
    "openai": 1.0,
    "openrouter": 1.0,
    "local": 1.0,
    "anthropic": 1.25,
    "google": 1.25,
}
