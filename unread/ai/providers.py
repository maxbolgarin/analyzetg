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


class ProviderSafetyBlockedError(RuntimeError):
    """Raised when a provider refused to emit content for safety reasons.

    Surfaced today by :class:`unread.ai.google_provider.GoogleProvider`
    when Gemini sets ``finish_reason`` to ``SAFETY`` / ``RECITATION`` /
    ``OTHER`` (the SDK *raises* ``ValueError`` on ``resp.text`` in those
    cases). Carries the structured reason + safety_ratings so the
    orchestrator can render a useful status instead of a generic crash,
    and a clean message for surfacing to the user. Safety blocks aren't
    transient — the orchestrator should NOT retry on this.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str = "",
        ratings: tuple = (),
        provider: str = "",
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.ratings = ratings
        self.provider = provider


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


def _resolve_provider_name(settings, slot: str) -> str:  # type: ignore[no-untyped-def]
    """Return the effective provider name for a slot (`chat`/`filter`/`audio`/`vision`).

    Reads the per-slot key (`ai.<slot>_provider`) first, then falls
    back to the deprecated umbrella `ai.provider` (transitional;
    `_migrate_legacy_ai_provider_sync` rewrites it on first read), then
    to `"openai"`. The audio slot snaps back to `openai` if the
    resolved provider has no Whisper-shape API.
    """
    direct = (getattr(settings.ai, f"{slot}_provider", "") or "").strip().lower()
    legacy = (getattr(settings.ai, "provider", "") or "").strip().lower()
    name = direct or legacy or "openai"
    if slot == "audio" and name not in {"openai", "openrouter", "local"}:
        name = "openai"
    return name


def make_chat_provider(settings) -> ChatProvider:  # type: ignore[no-untyped-def]
    """Construct the adapter for the chat slot.

    Reads `settings.ai.chat_provider` (the new per-slot key); falls
    back to the deprecated `ai.provider` for one cycle. Raises
    :class:`ProviderUnavailableError` when the picked provider is
    missing its credentials or its SDK can't be imported. The error
    message names the missing key/dependency so the caller can
    surface a useful banner instead of a stack trace.
    """
    name = _resolve_provider_name(settings, "chat")
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


def _provider_class_defaults(name: str) -> tuple[str, str]:
    """Return ``(chat, filter)`` defaults from the adapter's class attrs.

    Reading the class attribute avoids constructing the SDK client just
    to peek at a hard-coded default — relevant for ``unread --help``,
    ``unread settings``, and any callsite that resolves the model name
    without intent to dispatch a request.
    """
    if name == "openai":
        from unread.ai.openai_provider import OpenAIProvider

        return OpenAIProvider.default_chat_model, OpenAIProvider.default_filter_model
    if name == "openrouter":
        from unread.ai.openai_provider import OpenRouterProvider

        return OpenRouterProvider.default_chat_model, OpenRouterProvider.default_filter_model
    if name == "local":
        from unread.ai.openai_provider import LocalProvider

        return LocalProvider.default_chat_model, LocalProvider.default_filter_model
    if name == "anthropic":
        from unread.ai.anthropic_provider import AnthropicProvider

        return AnthropicProvider.default_chat_model, AnthropicProvider.default_filter_model
    if name == "google":
        from unread.ai.google_provider import GoogleProvider

        return GoogleProvider.default_chat_model, GoogleProvider.default_filter_model
    from unread.ai.openai_provider import OpenAIProvider

    return OpenAIProvider.default_chat_model, OpenAIProvider.default_filter_model


def resolve_chat(settings) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    """Resolve `(provider, model)` for the analyze / ask flagship slot.

    Provider source order: `ai.chat_provider` → legacy `ai.provider` →
    "openai". Model source order: `ai.chat_model` → legacy
    `openai.chat_model_default` (only when provider == openai) →
    provider class default.
    """
    provider = _resolve_provider_name(settings, "chat")
    model = settings.ai.chat_model or ""
    if not model and provider == "openai" and settings.openai.chat_model_default:
        model = settings.openai.chat_model_default
    if not model:
        model = _provider_class_defaults(provider)[0]
    return provider, model


def resolve_filter(settings) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    """Resolve `(provider, model)` for the map / rerank cheap-pass slot."""
    provider = _resolve_provider_name(settings, "filter")
    model = settings.ai.filter_model or ""
    if not model and provider == "openai" and settings.openai.filter_model_default:
        model = settings.openai.filter_model_default
    if not model:
        model = _provider_class_defaults(provider)[1]
    return provider, model


# Per-provider default audio model. Only providers that speak the
# OpenAI-shape `audio.transcriptions` API are listed; resolve_audio()
# enforces this set as a capability filter.
_DEFAULT_AUDIO_MODEL: dict[str, str] = {
    "openai": "gpt-4o-mini-transcribe",
    "openrouter": "openai/whisper-1",
    "local": "whisper",
}


# Per-provider default vision model. These are the model the slot
# resolver returns when `ai.vision_model` is empty. Picked to match
# each adapter's cheapest vision-capable offering circa 2026-05.
_DEFAULT_VISION_MODEL: dict[str, str] = {
    "openai": "gpt-4o-mini",
    "openrouter": "openai/gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
    "google": "gemini-2.5-flash",
    "local": "qwen2-vl",
}


def resolve_audio(settings) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    """Resolve `(provider, model)` for the audio transcription slot.

    Capability snap: anthropic / google have no Whisper-shape API,
    so `audio_provider` set to either silently snaps back to openai
    here. The settings UI prevents the bad pick at write time; this
    is defense-in-depth for hand-edited configs.

    Model source order: `ai.audio_model` → legacy
    `openai.audio_model_default` (when provider == openai) →
    provider's entry in `_DEFAULT_AUDIO_MODEL`.
    """
    provider = _resolve_provider_name(settings, "audio")
    model = settings.ai.audio_model or ""
    if not model and provider == "openai" and settings.openai.audio_model_default:
        model = settings.openai.audio_model_default
    if not model:
        model = _DEFAULT_AUDIO_MODEL.get(provider, "gpt-4o-mini-transcribe")
    return provider, model


def resolve_vision(settings) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    """Resolve `(provider, model)` for the image enrichment slot.

    Model source order: `ai.vision_model` → legacy `enrich.vision_model`
    (when provider == openai) → provider's entry in
    `_DEFAULT_VISION_MODEL`.
    """
    provider = _resolve_provider_name(settings, "vision")
    model = settings.ai.vision_model or ""
    if not model and provider == "openai" and settings.enrich.vision_model:
        model = settings.enrich.vision_model
    if not model:
        model = _DEFAULT_VISION_MODEL.get(provider, "gpt-4o-mini")
    return provider, model


# Back-compat shims — call sites in `unread.analyzer.openai_client` and
# `unread.ask.sources.core` still import these by name. Returning the
# model half of the tuple keeps the existing semantics (model only).
def resolve_chat_model(settings) -> str:  # type: ignore[no-untyped-def]
    return resolve_chat(settings)[1]


def resolve_filter_model(settings) -> str:  # type: ignore[no-untyped-def]
    return resolve_filter(settings)[1]


def provider_default_model(provider: str, role: str) -> str:
    """Hardcoded default model for `(provider, role)`.

    Used by the settings picker to surface "the model that would be
    used right now" in the "(use provider's default)" row, so users
    can see which name the slot resolves to instead of an opaque
    placeholder. Returns the empty string for (provider, role) pairs
    with no canonical default — the caller should fall back to a
    generic label in that case.
    """
    name = (provider or "").strip().lower()
    if role == "audio":
        return _DEFAULT_AUDIO_MODEL.get(name, "")
    if role == "vision":
        return _DEFAULT_VISION_MODEL.get(name, "")
    if role == "chat":
        return _provider_class_defaults(name)[0] if name else ""
    if role == "filter":
        return _provider_class_defaults(name)[1] if name else ""
    return ""


def make_audio_client(provider: str, settings):  # type: ignore[no-untyped-def]
    """Return an :class:`openai.AsyncOpenAI` configured for `provider`.

    All three audio-capable providers (openai / openrouter / local)
    speak the same `audio.transcriptions.create` API — the only
    difference is `base_url` + `api_key`. Returns the constructed
    client; the caller dispatches the actual transcription call.

    Raises :class:`ProviderUnavailableError` when the provider isn't
    a recognised audio provider or when its key/URL is missing.
    """
    from openai import AsyncOpenAI

    name = (provider or "").strip().lower()
    timeout = settings.openai.request_timeout_sec
    if name == "openai":
        if not settings.openai.api_key:
            raise ProviderUnavailableError(
                "audio provider 'openai' has no API key — set OPENAI_API_KEY or run `unread settings`."
            )
        return AsyncOpenAI(api_key=settings.openai.api_key, timeout=timeout)
    if name == "openrouter":
        if not settings.openrouter.api_key:
            raise ProviderUnavailableError(
                "audio provider 'openrouter' has no API key — set OPENROUTER_API_KEY or "
                "run `unread settings`."
            )
        from unread.ai.openai_provider import OPENROUTER_APP_HEADERS

        return AsyncOpenAI(
            api_key=settings.openrouter.api_key,
            base_url=settings.openrouter.base_url,
            timeout=timeout,
            default_headers=OPENROUTER_APP_HEADERS,
        )
    if name == "local":
        return AsyncOpenAI(
            api_key=settings.local.api_key or "local-no-key",
            base_url=settings.local.base_url,
            timeout=timeout,
        )
    raise ProviderUnavailableError(
        f"audio provider {name!r} is not Whisper-compatible. Pick openai / openrouter / local."
    )
