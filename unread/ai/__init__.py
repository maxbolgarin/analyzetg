"""Vendor-agnostic AI layer.

`unread` routes each capability slot independently — analyze (chat),
filter (cheap-pass), audio (transcription), vision (image
understanding) — through a per-slot `(provider, model)` pair. The
active adapter for each slot is picked by `settings.ai.<slot>_provider`
(values: openai | openrouter | anthropic | google | local).

  - `openai`     — OpenAI Chat / Whisper / vision via `AsyncOpenAI`.
  - `openrouter` — same SDK shape, pointed at OpenRouter's endpoint.
  - `anthropic`  — `anthropic.AsyncAnthropic` (`messages.create`),
                   chat + vision (image blocks).
  - `google`     — `google.genai.Client` (Gemini), chat + vision.
  - `local`      — OpenAI-compatible server (Ollama / LM Studio /
                   vLLM); chat + Whisper-shape audio + vision when the
                   chosen model supports it.

Embeddings (used by `unread ask` semantic search) are still
OpenAI-only — when the OpenAI key is missing, the ask pipeline falls
back to keyword retrieval and prints a one-line warning.
"""

from __future__ import annotations

from unread.ai.models import (
    ModelInfo,
    all_known_models,
    find_model,
    models_for_provider,
    supported_providers,
)
from unread.ai.providers import (
    ChatProvider,
    ChatResult,
    ProviderSafetyBlockedError,
    ProviderUnavailableError,
    make_audio_client,
    make_chat_provider,
    resolve_audio,
    resolve_chat,
    resolve_chat_model,
    resolve_filter,
    resolve_filter_model,
    resolve_vision,
)

__all__ = [
    "ChatProvider",
    "ChatResult",
    "ModelInfo",
    "ProviderSafetyBlockedError",
    "ProviderUnavailableError",
    "all_known_models",
    "find_model",
    "make_audio_client",
    "make_chat_provider",
    "models_for_provider",
    "resolve_audio",
    "resolve_chat",
    "resolve_chat_model",
    "resolve_filter",
    "resolve_filter_model",
    "resolve_vision",
    "supported_providers",
]
