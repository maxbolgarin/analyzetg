"""Thin wrapper around AsyncOpenAI with retries and usage logging."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from analyzetg.config import get_settings
from analyzetg.db.repo import Repo
from analyzetg.util.flood import retry_on_429
from analyzetg.util.logging import get_logger
from analyzetg.util.pricing import chat_cost

log = get_logger(__name__)


@dataclass(slots=True)
class ChatResult:
    text: str
    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int
    cost_usd: float | None
    truncated: bool = False  # True iff finish_reason == "length"


def make_client() -> AsyncOpenAI:
    s = get_settings()
    return AsyncOpenAI(api_key=s.openai.api_key, timeout=s.openai.request_timeout_sec)


def build_messages(system: str, static_context: str, dynamic: str) -> list[dict[str, str]]:
    """Prompt caching hygiene: system → static → dynamic, strictly in that order."""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": (static_context + "\n\n" + dynamic).strip()},
    ]


@retry_on_429()
async def _completion(
    oai: AsyncOpenAI, model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float
) -> Any:
    # `max_completion_tokens` replaces the deprecated `max_tokens` on gpt-5+
    # and reasoning models; older models (gpt-4o, etc.) accept it too.
    return await oai.chat.completions.create(
        model=model,
        messages=messages,
        max_completion_tokens=max_tokens,
        temperature=temperature,
    )


# Absolute ceiling for the retry-on-truncation budget. Most current models
# cap a single completion at ~16k tokens; raise carefully if you move to
# a reasoning model with higher caps.
_MAX_RETRY_TOKENS = 16_000


async def _one_call(
    oai: AsyncOpenAI,
    *,
    repo: Repo,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    context: dict[str, Any] | None,
) -> ChatResult:
    """Single OpenAI call with usage logging. Does NOT retry on truncation."""
    resp = await _completion(oai, model, messages, max_tokens, temperature)
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
    cost = chat_cost(model, prompt, cached, completion)
    await repo.log_usage(
        kind="chat",
        model=model,
        prompt_tokens=prompt,
        cached_tokens=cached,
        completion_tokens=completion,
        cost_usd=cost,
        context={**(context or {}), "finish_reason": finish} if finish else (context or {}),
    )
    log.info(
        "openai.chat",
        model=model,
        prompt=prompt,
        cached=cached,
        completion=completion,
        cost=cost,
        finish=finish,
    )
    return ChatResult(
        text=text,
        prompt_tokens=prompt,
        cached_tokens=cached,
        completion_tokens=completion,
        cost_usd=cost,
        truncated=truncated,
    )


async def chat_complete(
    oai: AsyncOpenAI,
    *,
    repo: Repo,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    context: dict[str, Any] | None = None,
) -> ChatResult:
    """Chat completion with automatic retry when the response is truncated.

    If `finish_reason == "length"` on the first call, retry once with
    `max_tokens` doubled (capped at `_MAX_RETRY_TOKENS`). The retry replaces
    the result — you don't get both. Cost is logged for both calls.
    """
    settings = get_settings()
    result = await _one_call(
        oai,
        repo=repo,
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=settings.openai.temperature,
        context=context,
    )
    if result.truncated and max_tokens < _MAX_RETRY_TOKENS:
        bumped = min(max_tokens * 2, _MAX_RETRY_TOKENS)
        log.warning(
            "openai.chat.truncated_retry",
            model=model,
            old_max=max_tokens,
            new_max=bumped,
            completion=result.completion_tokens,
        )
        result = await _one_call(
            oai,
            repo=repo,
            model=model,
            messages=messages,
            max_tokens=bumped,
            temperature=settings.openai.temperature,
            context={**(context or {}), "retry_of_truncated": True},
        )
        if result.truncated:
            log.warning(
                "openai.chat.truncated_after_retry",
                model=model,
                max_tokens=bumped,
                completion=result.completion_tokens,
                hint=(
                    "bump output_budget_tokens in the preset file "
                    f"(current budget hit max retry cap {_MAX_RETRY_TOKENS})"
                ),
            )
    return result
