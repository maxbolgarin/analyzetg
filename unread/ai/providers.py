"""ChatProvider abstraction + factory.

One ChatProvider per active install (selected by `settings.ai.provider`).
Each adapter:
  - Accepts OpenAI-shaped messages: `[{"role": "system"|"user"|"assistant", "content": str}, ...]`.
  - Returns the canonical :class:`ChatResult` regardless of the underlying SDK.
  - Owns provider-specific defaults (`default_chat_model`, `default_filter_model`).

The truncation-retry / usage-logging / cost-accounting orchestration
stays in `unread.analyzer.openai_client.chat_complete` — it calls the
adapter's `.chat(...)` once, inspects the result's `truncated` flag,
and decides whether to retry. This way every provider gets the same
retry semantics for free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class ChatResult:
    """Canonical chat-completion response.

    Mirrors the legacy `analyzer.openai_client.ChatResult` so call
    sites that destructure the dataclass continue to work unchanged.
    `cost_usd` is populated by the orchestrator using the per-model
    pricing table — adapters return `None` for it and let the layer
    above figure cost out from token counts.

    `truncated` is True when the underlying provider stopped because
    it hit the output budget. The orchestrator retries on this with
    a doubled `max_tokens`.
    """

    text: str
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    cost_usd: float | None = None
    truncated: bool = False


class ProviderUnavailableError(RuntimeError):
    """Raised when an adapter can't be constructed (missing key/SDK)."""


@runtime_checkable
class ChatProvider(Protocol):
    """Vendor-agnostic chat completion contract.

    Implementations must be safe to construct repeatedly (no shared
    network state across instances) and async-callable from any task.
    """

    name: str

    @property
    def default_chat_model(self) -> str:
        """The model used when `settings.ai.chat_model` is empty."""

    @property
    def default_filter_model(self) -> str:
        """The model used when `settings.ai.filter_model` is empty."""

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
    ) -> ChatResult:
        """One-shot chat call. No retries — the orchestrator handles those."""


def make_chat_provider(settings) -> ChatProvider:  # type: ignore[no-untyped-def]
    """Construct the adapter for `settings.ai.provider`.

    Raises :class:`ProviderUnavailableError` when the picked provider
    is missing its credentials or its SDK can't be imported. The
    error message names the missing key/dependency so the caller can
    surface a useful banner instead of a stack trace.
    """
    name = (settings.ai.provider or "openai").strip().lower()
    if name == "openai":
        from unread.ai.openai_provider import OpenAIProvider

        return OpenAIProvider(settings)
    if name == "openrouter":
        from unread.ai.openai_provider import OpenRouterProvider

        return OpenRouterProvider(settings)
    if name == "local":
        from unread.ai.openai_provider import LocalProvider

        return LocalProvider(settings)
    if name == "anthropic":
        from unread.ai.anthropic_provider import AnthropicProvider

        return AnthropicProvider(settings)
    if name == "google":
        from unread.ai.google_provider import GoogleProvider

        return GoogleProvider(settings)
    raise ProviderUnavailableError(
        f"Unknown AI provider {name!r}. Set `ai.provider` to one of: "
        "openai, openrouter, anthropic, google, local."
    )


def resolve_chat_model(settings) -> str:  # type: ignore[no-untyped-def]
    """Pick the effective chat model for the active provider.

    Resolution order (high → low):
      1. `settings.ai.chat_model` — explicit per-install override.
      2. `settings.openai.chat_model_default` if provider == "openai"
         (back-compat with the existing config knob).
      3. The adapter's hard-coded `default_chat_model`.
    """
    if settings.ai.chat_model:
        return settings.ai.chat_model
    provider_name = (settings.ai.provider or "openai").strip().lower()
    if provider_name == "openai" and settings.openai.chat_model_default:
        return settings.openai.chat_model_default
    return make_chat_provider(settings).default_chat_model


def resolve_filter_model(settings) -> str:  # type: ignore[no-untyped-def]
    """Pick the effective cheap-pass model for the active provider."""
    if settings.ai.filter_model:
        return settings.ai.filter_model
    provider_name = (settings.ai.provider or "openai").strip().lower()
    if provider_name == "openai" and settings.openai.filter_model_default:
        return settings.openai.filter_model_default
    return make_chat_provider(settings).default_filter_model
