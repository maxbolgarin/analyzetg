"""Vision-completion adapters — one per provider.

Each adapter accepts an image (raw bytes + mime) and a text prompt
and returns the canonical :class:`unread.ai.providers.ChatResult`
shape so cost accounting and truncation handling work the same as
the text-chat path.

Five providers; three of them (openai / openrouter / local) share the
OpenAI Chat Completions image_url shape via :class:`_OpenAICompatVision`.
Anthropic and Google use their native image content blocks.

Providers without vision support (e.g. the local server's selected
model can't accept images) return an "empty" result — the call site
already treats empty descriptions as a soft skip.
"""

from __future__ import annotations

import asyncio
import base64
import random
from typing import Any, Protocol, runtime_checkable

from unread.ai.providers import (
    ChatResult,
    ProviderSafetyBlockedError,
    ProviderUnavailableError,
)
from unread.util.flood import _user_visible_retry_status, retry_on_429
from unread.util.logging import get_logger

log = get_logger(__name__)


@runtime_checkable
class VisionProvider(Protocol):
    """Vendor-agnostic image-description contract.

    Mirrors :class:`unread.ai.providers.ChatProvider` but accepts an
    image alongside the prompt. Implementations construct their SDK
    client lazily and reuse it across calls; no shared network state.
    """

    name: str

    @property
    def default_vision_model(self) -> str: ...

    async def describe_image(
        self,
        *,
        model: str,
        image_bytes: bytes,
        mime_type: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> ChatResult:
        """One-shot vision call. Returns a populated `ChatResult`.

        `text` is the description; `prompt_tokens` / `completion_tokens`
        / `cached_tokens` carry the usage breakdown for cost accounting;
        `truncated=True` when the upstream cut output for hitting the
        budget.
        """


# ----------------------------- OpenAI-shape -----------------------------------


class _OpenAICompatVision:
    """Shared `AsyncOpenAI` plumbing for vision via Chat Completions.

    OpenAI's `chat.completions` endpoint accepts a list of content
    parts in the user message — text plus `image_url` blocks carrying
    a data URI. This shape is also accepted by OpenRouter (when the
    underlying model supports vision) and by most OpenAI-compatible
    local servers (LM Studio, vLLM with a vision model loaded). The
    only thing that varies is `(api_key, base_url)`, which subclasses
    set via `_make_client`.
    """

    name: str = "openai-compat-vision"
    default_vision_model: str = ""

    def __init__(self, settings) -> None:  # type: ignore[no-untyped-def]
        self._settings = settings
        self._client = self._make_client(settings)

    def _make_client(self, settings):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    @retry_on_429()
    async def _completion(self, **kwargs: Any) -> Any:
        return await self._client.chat.completions.create(**kwargs)

    async def describe_image(
        self,
        *,
        model: str,
        image_bytes: bytes,
        mime_type: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> ChatResult:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_url = f"data:{mime_type};base64,{b64}"
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        # `temperature` is silently dropped for the same reasoning-model
        # family as `_OpenAICompatBase.chat`; reuse the predicate.
        from unread.ai.openai_provider import _is_reasoning_model

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
        }
        if not _is_reasoning_model(model):
            kwargs["temperature"] = temperature
        resp = await self._completion(**kwargs)
        choice = resp.choices[0]
        text = (choice.message.content or "").strip()
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


class OpenAIVisionProvider(_OpenAICompatVision):
    name = "openai"
    default_vision_model = "gpt-4o-mini"

    def _make_client(self, settings):  # type: ignore[no-untyped-def]
        from openai import AsyncOpenAI

        from unread.ai.trust import enforce_base_url_trust

        if not settings.openai.api_key:
            raise ProviderUnavailableError(
                "Vision via OpenAI selected but `openai.api_key` is empty. Run `unread settings` to add one."
            )
        enforce_base_url_trust("openai", settings)
        kwargs: dict[str, Any] = {
            "api_key": settings.openai.api_key,
            "timeout": settings.openai.request_timeout_sec,
        }
        if settings.ai.base_url:
            kwargs["base_url"] = settings.ai.base_url
        return AsyncOpenAI(**kwargs)


class OpenRouterVisionProvider(_OpenAICompatVision):
    name = "openrouter"
    default_vision_model = "openai/gpt-4o-mini"

    def _make_client(self, settings):  # type: ignore[no-untyped-def]
        from openai import AsyncOpenAI

        from unread.ai.openai_provider import OPENROUTER_APP_HEADERS
        from unread.ai.trust import enforce_base_url_trust

        if not settings.openrouter.api_key:
            raise ProviderUnavailableError(
                "Vision via OpenRouter selected but `openrouter.api_key` is empty. "
                "Run `unread settings` to add one."
            )
        enforce_base_url_trust("openrouter", settings)
        return AsyncOpenAI(
            api_key=settings.openrouter.api_key,
            base_url=settings.ai.base_url or settings.openrouter.base_url,
            timeout=settings.openai.request_timeout_sec,
            default_headers=OPENROUTER_APP_HEADERS,
        )


class LocalVisionProvider(_OpenAICompatVision):
    name = "local"
    # No fixed default — the user's local server might serve any
    # vision model. `qwen2-vl` is a popular Ollama-friendly choice;
    # users override via `ai.vision_model`.
    default_vision_model = "qwen2-vl"

    def _make_client(self, settings):  # type: ignore[no-untyped-def]
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=settings.local.api_key or "local-no-key",
            base_url=settings.ai.base_url or settings.local.base_url,
            timeout=settings.openai.request_timeout_sec,
        )


# ----------------------------- Anthropic --------------------------------------


class AnthropicVisionProvider:
    """Anthropic vision via `messages.create` with image blocks.

    Anthropic accepts images as `{"type": "image", "source": {...}}`
    blocks inside a user message's content list. The `source` is
    base64 (or URL); we always send base64 since the chat-cache /
    enrichment cache already holds raw bytes.

    Mirrors the chat adapter's retry logic (own backoff loop with
    user-visible status, SDK retries disabled).
    """

    name = "anthropic"
    default_vision_model = "claude-haiku-4-5"

    def __init__(self, settings) -> None:  # type: ignore[no-untyped-def]
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:  # pragma: no cover
            raise ProviderUnavailableError(
                "Vision via Anthropic selected but the `anthropic` package isn't installed. "
                "Run `uv sync --extra dev`."
            ) from e
        if not settings.anthropic.api_key:
            raise ProviderUnavailableError(
                "Vision via Anthropic selected but `anthropic.api_key` is empty. "
                "Run `unread settings` to add one."
            )
        self._client = AsyncAnthropic(
            api_key=settings.anthropic.api_key,
            timeout=settings.openai.request_timeout_sec,
            max_retries=0,
        )
        self._settings = settings

    async def describe_image(
        self,
        *,
        model: str,
        image_bytes: bytes,
        mime_type: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> ChatResult:
        b64 = base64.b64encode(image_bytes).decode("ascii")
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": user_prompt},
                    ],
                }
            ],
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        from anthropic import (  # type: ignore[import-not-found]
            APIConnectionError,
            APIStatusError,
            RateLimitError,
        )

        max_retries = max(1, self._settings.openai.max_retries)
        resp = None
        for attempt in range(max_retries):
            try:
                resp = await self._client.messages.create(**kwargs)
                break
            except (RateLimitError, APIConnectionError) as e:
                if attempt == max_retries - 1:
                    raise
                delay = min(1.5**attempt, 30.0) + random.uniform(0, 1)
                log.warning(
                    "anthropic.vision_retry",
                    attempt=attempt + 1,
                    delay=round(delay, 2),
                    err=type(e).__name__,
                )
                _user_visible_retry_status(
                    f"Anthropic {type(e).__name__} — retrying in {delay:.0f}s "
                    f"(attempt {attempt + 1}/{max_retries})…"
                )
                await asyncio.sleep(delay)
            except APIStatusError as e:
                if 500 <= int(getattr(e, "status_code", 0) or 0) < 600 and attempt < max_retries - 1:
                    delay = min(1.5**attempt, 30.0) + random.uniform(0, 1)
                    await asyncio.sleep(delay)
                else:
                    raise
        if resp is None:
            raise RuntimeError("Anthropic vision call exhausted retries without a response")

        text_parts: list[str] = []
        for block in getattr(resp, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", "") or "")
        text = "".join(text_parts).strip()

        usage = getattr(resp, "usage", None)
        prompt_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cached_tokens = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        truncated = getattr(resp, "stop_reason", None) == "max_tokens"

        return ChatResult(
            text=text,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            truncated=truncated,
        )


# ----------------------------- Google -----------------------------------------


class GoogleVisionProvider:
    """Gemini vision via `generate_content` with image parts.

    Gemini's `Part.from_bytes(data=..., mime_type=...)` is the
    documented way to pass images alongside a text part. The single
    user message carries both. Safety-block handling matches the
    chat adapter (typed exception so the orchestrator doesn't retry).
    """

    name = "google"
    default_vision_model = "gemini-2.5-flash"

    def __init__(self, settings) -> None:  # type: ignore[no-untyped-def]
        try:
            from google import genai
        except ImportError as e:  # pragma: no cover
            raise ProviderUnavailableError(
                "Vision via Google selected but the `google-genai` package isn't installed. "
                "Run `uv sync --extra dev`."
            ) from e
        if not settings.google.api_key:
            raise ProviderUnavailableError(
                "Vision via Google selected but `google.api_key` is empty. Run `unread settings` to add one."
            )
        self._client = genai.Client(api_key=settings.google.api_key)
        self._settings = settings

    async def describe_image(
        self,
        *,
        model: str,
        image_bytes: bytes,
        mime_type: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> ChatResult:
        from google.genai import errors as genai_errors
        from google.genai import types

        contents = [
            types.Content(
                role="user",
                parts=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    types.Part(text=user_prompt),
                ],
            )
        ]
        config_kwargs: dict[str, object] = {
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_prompt:
            config_kwargs["system_instruction"] = system_prompt

        max_retries = max(1, self._settings.openai.max_retries)
        resp = None
        for attempt in range(max_retries):
            try:
                resp = await self._client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(**config_kwargs),
                )
                break
            except genai_errors.APIError as e:
                code = int(getattr(e, "code", 0) or 0)
                retriable = code == 429 or 500 <= code < 600
                if not retriable or attempt == max_retries - 1:
                    raise
                delay = min(1.5**attempt, 30.0) + random.uniform(0, 1)
                _user_visible_retry_status(
                    f"Gemini {code} — retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})…"
                )
                await asyncio.sleep(delay)
        if resp is None:
            raise RuntimeError("Gemini vision call exhausted retries without a response")

        finish_reason = ""
        candidates = getattr(resp, "candidates", None) or []
        if candidates:
            finish_reason = str(getattr(candidates[0], "finish_reason", "") or "")

        try:
            text = (resp.text or "").strip()
        except ValueError as e:
            raise ProviderSafetyBlockedError(
                f"Gemini refused to describe the image (finish_reason={finish_reason or 'unknown'}).",
                reason=finish_reason or "unknown",
                provider=self.name,
            ) from e
        except AttributeError:
            text = ""

        usage = getattr(resp, "usage_metadata", None)
        prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        completion_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        cached_tokens = int(getattr(usage, "cached_content_token_count", 0) or 0)
        truncated = finish_reason.upper().endswith("MAX_TOKENS")

        return ChatResult(
            text=text,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            truncated=truncated,
        )


# ----------------------------- Factory ----------------------------------------


def make_vision_provider(provider: str, settings) -> VisionProvider:  # type: ignore[no-untyped-def]
    """Construct the vision adapter for `provider`.

    Raises :class:`ProviderUnavailableError` when the provider name is
    unknown or its credentials/SDK are missing — caller can surface the
    error in `enrich/image.py` and skip image enrichment cleanly.
    """
    name = (provider or "").strip().lower()
    if name == "openai":
        return OpenAIVisionProvider(settings)
    if name == "openrouter":
        return OpenRouterVisionProvider(settings)
    if name == "local":
        return LocalVisionProvider(settings)
    if name == "anthropic":
        return AnthropicVisionProvider(settings)
    if name == "google":
        return GoogleVisionProvider(settings)
    raise ProviderUnavailableError(
        f"Unknown vision provider {name!r}. Set `ai.vision_provider` to one of: "
        "openai, openrouter, anthropic, google, local."
    )
