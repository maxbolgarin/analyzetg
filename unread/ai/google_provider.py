"""Google Gemini adapter (Developer API, via `google-genai`).

Wraps `google.genai.Client.aio.models.generate_content` to the
canonical :class:`unread.ai.providers.ChatResult` shape. Translations:

  - **System message**: Gemini takes a `system_instruction` on the
    `GenerateContentConfig`, NOT inside `contents`. We split the
    OpenAI-shaped messages, route system entries to `system_instruction`,
    and convert the rest into a list of `types.Content` parts (Gemini
    uses `role="user"` and `role="model"` instead of `assistant`).
  - **Truncation signal**: Gemini's `candidates[0].finish_reason` is
    `"MAX_TOKENS"` when output is cut. Mapped to `truncated=True`.
  - **Cached tokens**: `usage_metadata.cached_content_token_count` —
    surfaced as `cached_tokens` so prompt-cache accounting matches.
  - **Retries**: the `google-genai` client doesn't expose `max_retries`
    on its constructor, so we wrap the chat call in a small inline
    backoff loop that catches the SDK's typed `APIError` for 429 / 5xx
    and surfaces a one-line retry status to the user.

Vertex AI mode is intentionally out of scope for v1 — it would require
project / location / ADC plumbing beyond the API-key flow most users
expect.
"""

from __future__ import annotations

import asyncio
import random

from unread.ai.providers import ChatResult, ProviderUnavailableError
from unread.util.flood import _user_visible_retry_status
from unread.util.logging import get_logger

log = get_logger(__name__)


def _convert_messages(
    messages: list[dict[str, str]],
) -> tuple[str, list]:
    """Split OpenAI-shaped messages into (system_instruction, contents).

    Multiple system entries are concatenated. `assistant` role is
    renamed to `model` (Gemini's vocabulary).
    """
    from google.genai import types

    system_chunks: list[str] = []
    contents: list = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            if content:
                system_chunks.append(content)
            continue
        gemini_role = "model" if role == "assistant" else "user"
        contents.append(types.Content(role=gemini_role, parts=[types.Part(text=content)]))
    system_prompt = "\n\n".join(system_chunks)
    return system_prompt, contents


class GoogleProvider:
    name = "google"
    default_chat_model = "gemini-2.5-flash"
    default_filter_model = "gemini-2.5-flash-lite"

    def __init__(self, settings) -> None:  # type: ignore[no-untyped-def]
        try:
            from google import genai
        except ImportError as e:  # pragma: no cover — pulled in via pyproject
            raise ProviderUnavailableError(
                "Google provider selected but the `google-genai` package isn't installed. "
                "Run `uv sync --extra dev` (or pip install google-genai)."
            ) from e
        if not settings.google.api_key:
            raise ProviderUnavailableError(
                "Google provider selected but `google.api_key` is empty. Run `unread tg init` to add one."
            )
        self._client = genai.Client(api_key=settings.google.api_key)
        self._settings = settings

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
    ) -> ChatResult:
        from google.genai import errors as genai_errors
        from google.genai import types

        system_prompt, contents = _convert_messages(messages)
        config_kwargs: dict[str, object] = {
            "max_output_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_prompt:
            config_kwargs["system_instruction"] = system_prompt

        max_retries = self._settings.openai.max_retries
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
                # google-genai's APIError carries an integer `code`
                # (HTTP-style). 429 = rate limit; 5xx = transient server
                # error. Anything else (4xx) is a programmer / config
                # bug — re-raise so the user sees it instead of waiting.
                code = int(getattr(e, "code", 0) or 0)
                retriable = code == 429 or 500 <= code < 600
                if not retriable or attempt == max_retries - 1:
                    raise
                delay = min(1.5**attempt, 30.0) + random.uniform(0, 1)
                log.warning(
                    "google.retry",
                    attempt=attempt + 1,
                    delay=round(delay, 2),
                    code=code,
                )
                _user_visible_retry_status(
                    f"Gemini {code} — retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})…"
                )
                await asyncio.sleep(delay)
        assert resp is not None  # loop either returns or raises

        # `resp.text` is the convenience accessor; falls back to
        # walking candidates[0].content.parts when None.
        text = resp.text or ""

        usage = getattr(resp, "usage_metadata", None)
        prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
        completion_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
        cached_tokens = int(getattr(usage, "cached_content_token_count", 0) or 0)

        finish_reason = ""
        candidates = getattr(resp, "candidates", None) or []
        if candidates:
            finish_reason = str(getattr(candidates[0], "finish_reason", "") or "")
        truncated = finish_reason.upper().endswith("MAX_TOKENS")

        return ChatResult(
            text=text,
            prompt_tokens=prompt_tokens,
            cached_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            truncated=truncated,
        )
