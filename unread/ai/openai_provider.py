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


def _is_reasoning_model(model: str) -> bool:
    """True when `model` is an OpenAI reasoning-class model that rejects
    custom `temperature`.

    Looks up :class:`unread.ai.models.ModelInfo.reasoning` first — that's
    the curated source of truth (covers gpt-5.x including mini/nano,
    o-series, and OpenRouter aliases like `openai/gpt-5.4-mini`). When
    the model isn't in the catalog, falls back to a name-shape heuristic:
    `o1`/`o3`/`o4`/`gpt-5` prefixes (matched against the bare suffix so
    `vendor/model` routing still works). The heuristic is permissive —
    accidentally dropping temperature for a non-reasoning model is
    harmless (defaults to 1.0 server-side), while incorrectly
    *forwarding* temperature to a reasoning model 400s the request.
    """
    from unread.ai.models import find_model

    info = find_model(model)
    if info is not None and info.reasoning:
        return True
    name = model.rsplit("/", 1)[-1].lower()
    return name.startswith("o1") or name.startswith("o3") or name.startswith("o4") or name.startswith("gpt-5")


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
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
        }
        # OpenAI's reasoning model family (gpt-5, gpt-5.4, o1, o3, ...)
        # rejects any `temperature` other than the default 1.0 with a
        # 400. Drop the parameter for those models so the wired-in
        # default of 0.2 (config.py) doesn't silently 4xx every chat
        # call when the user picks the catalog default `gpt-5.4-mini`.
        if not _is_reasoning_model(model):
            kwargs["temperature"] = temperature
        return await self._client.chat.completions.create(**kwargs)

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
                "OpenAI provider selected but `openai.api_key` is empty. Run `unread init` to add one."
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
                "Run `unread init` to add one."
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
