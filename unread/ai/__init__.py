"""Vendor-agnostic chat-completion layer.

`unread` chat-completion call sites speak to a single `ChatProvider`
interface. The active adapter is picked by `settings.ai.provider`:

  - `openai`     — OpenAI Chat Completions via `AsyncOpenAI`.
  - `openrouter` — same SDK, pointed at OpenRouter's compatible endpoint.
  - `anthropic`  — `anthropic.AsyncAnthropic` (`messages.create`).
  - `google`     — `google.genai.Client` (Gemini, Developer API).
  - `local`      — `AsyncOpenAI` against a self-hosted OpenAI-compatible
                   server (Ollama / LM Studio / vLLM).

Capabilities that the alternative providers don't support natively
(Whisper transcription, embeddings, vision) keep using the OpenAI SDK
directly via `settings.openai.api_key`. Those call sites gate on the
key being present and surface a friendly "needs OpenAI" message when
it's empty — see `unread/cli.py:_exit_missing_openai_credentials`.
"""

from __future__ import annotations

from unread.ai.providers import (
    ChatProvider,
    ChatResult,
    ProviderUnavailableError,
    make_chat_provider,
    resolve_chat_model,
    resolve_filter_model,
)

__all__ = [
    "ChatProvider",
    "ChatResult",
    "ProviderUnavailableError",
    "make_chat_provider",
    "resolve_chat_model",
    "resolve_filter_model",
]
