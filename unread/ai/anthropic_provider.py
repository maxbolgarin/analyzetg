"""Anthropic Claude adapter.

Wraps `anthropic.AsyncAnthropic.messages.create` to the canonical
:class:`unread.ai.providers.ChatResult` shape. Two notable
translations relative to the OpenAI shape:

  - **System message**: Anthropic doesn't accept `{"role": "system"}`
    inside `messages`. We pull every system-role message out of the
    list and concatenate them into the top-level `system=` parameter.
  - **Truncation signal**: Anthropic uses `stop_reason == "max_tokens"`
    where OpenAI uses `finish_reason == "length"`. We map that to
    `ChatResult.truncated=True` so the orchestrator's truncation-retry
    fires identically.
  - **Cached tokens**: Anthropic exposes `cache_read_input_tokens` on
    the usage object. We surface it as `cached_tokens` for parity with
    OpenAI's prompt-cache accounting.
"""

from __future__ import annotations

from typing import Any

from unread.ai.providers import ChatResult, ProviderUnavailableError


def _split_system_and_messages(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    """Pull system-role entries into a single string; return everything else.

    `unread`'s formatter always emits a single system message followed
    by a single user message (see `analyzer.openai_client.build_messages`),
    but be defensive about multiple / interleaved system entries — some
    callers (e.g. the rerank prompt) don't go through `build_messages`.
    """
    system_chunks: list[str] = []
    rest: list[dict[str, str]] = []
    for m in messages:
        if m.get("role") == "system":
            system_chunks.append(m.get("content", ""))
        else:
            rest.append(m)
    system_prompt = "\n\n".join(c for c in system_chunks if c)
    return system_prompt, rest


class AnthropicProvider:
    name = "anthropic"
    # Sensible defaults as of the SDK / model lineup at the time of
    # writing; the user can switch via `ai.chat_model` / `ai.filter_model`.
    default_chat_model = "claude-sonnet-4-5"
    default_filter_model = "claude-haiku-4-5"

    def __init__(self, settings) -> None:  # type: ignore[no-untyped-def]
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:  # pragma: no cover — pulled in via pyproject
            raise ProviderUnavailableError(
                "Anthropic provider selected but the `anthropic` package isn't installed. "
                "Run `uv sync --extra dev` (or pip install anthropic)."
            ) from e
        if not settings.anthropic.api_key:
            raise ProviderUnavailableError(
                "Anthropic provider selected but `anthropic.api_key` is empty. "
                "Run `unread tg init` to add one."
            )
        self._client = AsyncAnthropic(
            api_key=settings.anthropic.api_key,
            timeout=settings.openai.request_timeout_sec,
        )
        self._settings = settings

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
    ) -> ChatResult:
        system_prompt, rest = _split_system_and_messages(messages)
        # Anthropic requires `messages` non-empty. If the formatter
        # only produced a system entry (rare; defensive only), inject
        # a placeholder user turn so the call doesn't fail validation.
        if not rest:
            rest = [{"role": "user", "content": ""}]
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": rest,
            "temperature": temperature,
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        resp = await self._client.messages.create(**kwargs)

        # Concatenate any text blocks (Anthropic returns a list of
        # content blocks; tool-use / thinking blocks are absent here
        # since we don't request them).
        text_parts: list[str] = []
        for block in getattr(resp, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text_parts.append(getattr(block, "text", "") or "")
        text = "".join(text_parts)

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
