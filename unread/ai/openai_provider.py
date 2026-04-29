"""OpenAI-compatible adapters.

Three providers in this file all speak the OpenAI Chat Completions
API and share an `AsyncOpenAI` client (different `base_url` + key).
Splitting them lets each carry its own provider-specific defaults
(model names, base URL) without runtime conditionals.

  - :class:`OpenAIProvider`     — vanilla OpenAI.
  - :class:`OpenRouterProvider` — `https://openrouter.ai/api/v1` proxy
                                  to many models. Key from `settings.openrouter`.
  - :class:`LocalProvider`      — self-hosted server (Ollama / LM Studio /
                                  vLLM). Key from `settings.local` (defaults
                                  to a placeholder; most local servers ignore
                                  the header).
"""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from unread.ai.providers import ChatResult, ProviderUnavailableError
from unread.ai.trust import enforce_base_url_trust
from unread.util.flood import retry_on_429


class _OpenAICompatBase:
    """Shared `AsyncOpenAI` plumbing.

    Subclasses set `name`, supply `_make_client(settings)`, and pin
    their `default_chat_model` / `default_filter_model`. The actual
    HTTP call lives here so per-provider classes stay tiny.
    """

    name: str = "openai-compat"
    default_chat_model: str = ""
    default_filter_model: str = ""

    def __init__(self, settings) -> None:  # type: ignore[no-untyped-def]
        self._settings = settings
        self._client = self._make_client(settings)

    def _make_client(self, settings) -> AsyncOpenAI:  # type: ignore[no-untyped-def]
        raise NotImplementedError

    @retry_on_429()
    async def _completion(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
    ) -> Any:
        # `max_completion_tokens` is the modern name (gpt-5+, reasoning
        # models). Older OpenAI-compat servers still accept it; the few
        # that don't are local-model bridges that vary by version, and
        # the user can fall back by editing the local server's config.
        return await self._client.chat.completions.create(
            model=model,
            messages=messages,
            max_completion_tokens=max_tokens,
            temperature=temperature,
        )

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
    ) -> ChatResult:
        resp = await self._completion(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        choice = resp.choices[0]
        text = choice.message.content or ""
        finish = getattr(choice, "finish_reason", None)
        truncated = finish == "length"
        usage = getattr(resp, "usage", None)
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        cached = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cached = int(getattr(details, "cached_tokens", 0) or 0)
        return ChatResult(
            text=text,
            prompt_tokens=prompt,
            cached_tokens=cached,
            completion_tokens=completion,
            truncated=truncated,
        )


class OpenAIProvider(_OpenAICompatBase):
    name = "openai"
    default_chat_model = "gpt-5.4-mini"
    default_filter_model = "gpt-5.4-nano"

    def _make_client(self, settings) -> AsyncOpenAI:  # type: ignore[no-untyped-def]
        if not settings.openai.api_key:
            raise ProviderUnavailableError(
                "OpenAI provider selected but `openai.api_key` is empty. Run `unread tg init` to add one."
            )
        enforce_base_url_trust("openai", settings)
        kwargs: dict[str, Any] = {
            "api_key": settings.openai.api_key,
            "timeout": settings.openai.request_timeout_sec,
        }
        # `settings.ai.base_url` lets the user point the OpenAI SDK at
        # a private gateway (e.g. internal Azure OpenAI proxy) without
        # switching to the OpenRouter / Local adapters.
        if settings.ai.base_url:
            kwargs["base_url"] = settings.ai.base_url
        return AsyncOpenAI(**kwargs)


class OpenRouterProvider(_OpenAICompatBase):
    name = "openrouter"
    # OpenRouter prefixes models by upstream vendor. These are widely
    # available, cheap defaults; users can override via `ai.chat_model`.
    default_chat_model = "openai/gpt-5.4-mini"
    default_filter_model = "openai/gpt-5.4-nano"

    def _make_client(self, settings) -> AsyncOpenAI:  # type: ignore[no-untyped-def]
        if not settings.openrouter.api_key:
            raise ProviderUnavailableError(
                "OpenRouter provider selected but `openrouter.api_key` is empty. "
                "Run `unread tg init` to add one."
            )
        enforce_base_url_trust("openrouter", settings)
        return AsyncOpenAI(
            api_key=settings.openrouter.api_key,
            base_url=settings.ai.base_url or settings.openrouter.base_url,
            timeout=settings.openai.request_timeout_sec,
        )


class LocalProvider(_OpenAICompatBase):
    name = "local"
    # Most local servers ship Llama 3.1 / Qwen 2.5 by default. The user
    # almost always needs to override; the default is a "model that
    # exists somewhere" placeholder so a CLI smoke run reaches the
    # endpoint instead of erroring at config validation time.
    default_chat_model = "llama3.1"
    default_filter_model = "llama3.1"

    def _make_client(self, settings) -> AsyncOpenAI:  # type: ignore[no-untyped-def]
        return AsyncOpenAI(
            api_key=settings.local.api_key or "local-no-key",
            base_url=settings.ai.base_url or settings.local.base_url,
            timeout=settings.openai.request_timeout_sec,
        )
