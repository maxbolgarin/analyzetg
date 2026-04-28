"""Chat-completion orchestration: provider dispatch + retries + logging.

The actual API calls live in `unread.ai.<provider>_provider`. This
module owns the policy that's identical across providers:

  - Single-call → log usage / cost / context fields.
  - On `truncated=True` (output cut at `max_tokens`), retry once with
    a doubled budget, capped at `_MAX_RETRY_TOKENS`.
  - Re-export :class:`ChatResult` from the canonical `unread.ai`
    module so existing callers that destructure it keep working.

`make_client()` returns the active provider (an alias preserved for
back-compat — call sites pass it to `chat_complete` exactly as they
did when it was an `AsyncOpenAI` instance).
"""

from __future__ import annotations

from typing import Any

from unread.ai import ChatProvider, ChatResult, make_chat_provider
from unread.config import get_settings
from unread.db.repo import Repo
from unread.util.logging import get_logger
from unread.util.pricing import chat_cost

log = get_logger(__name__)


# Re-export so legacy `from unread.analyzer.openai_client import ChatResult`
# imports keep working without churn.
__all__ = ["ChatResult", "build_messages", "chat_complete", "make_client"]


def make_client() -> ChatProvider:
    """Construct the active chat provider for the current settings."""
    return make_chat_provider(get_settings())


def build_messages(system: str, static_context: str, dynamic: str) -> list[dict[str, str]]:
    """Prompt caching hygiene: system → static → dynamic, strictly in that order."""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": (static_context + "\n\n" + dynamic).strip()},
    ]


# Absolute ceiling for the retry-on-truncation budget. Most current models
# cap a single completion at ~16k tokens; raise carefully if you move to
# a reasoning model with higher caps.
_MAX_RETRY_TOKENS = 16_000


async def _one_call(
    provider: ChatProvider,
    *,
    repo: Repo,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float,
    context: dict[str, Any] | None,
) -> ChatResult:
    """Single chat call with usage logging. Does NOT retry on truncation.

    The adapter returns a populated :class:`ChatResult` minus `cost_usd`;
    we compute cost from the per-model pricing table and tag the usage
    log with the provider name so multi-provider installs can attribute
    spend correctly.
    """
    raw = await provider.chat(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    cost = chat_cost(model, raw.prompt_tokens, raw.cached_tokens, raw.completion_tokens)
    finish = "length" if raw.truncated else None
    log_context: dict[str, Any] = {**(context or {}), "provider": provider.name}
    if finish:
        log_context["finish_reason"] = finish
    await repo.log_usage(
        kind="chat",
        model=model,
        prompt_tokens=raw.prompt_tokens,
        cached_tokens=raw.cached_tokens,
        completion_tokens=raw.completion_tokens,
        cost_usd=cost,
        context=log_context,
    )
    # Surface a few identifying keys from `context` so the log tells you
    # *what* each call was for (e.g. phase=enrich_link with the URL itself,
    # phase=map with batch_hash). Without this, 53 link summaries and 3
    # analysis chunks all look identical in the log stream.
    ctx_fields = {
        k: v
        for k, v in (context or {}).items()
        if k
        in {
            "phase",
            "url",
            "url_host",
            "batch_hash",
            "doc_id",
            "chat_id",
            "msg_id",
            "msg_date",
            "retry_of_truncated",
        }
        and v is not None
    }
    log.info(
        "ai.chat",
        provider=provider.name,
        model=model,
        prompt=raw.prompt_tokens,
        cached=raw.cached_tokens,
        completion=raw.completion_tokens,
        cost=cost,
        finish=finish,
        **ctx_fields,
    )
    return ChatResult(
        text=raw.text,
        prompt_tokens=raw.prompt_tokens,
        cached_tokens=raw.cached_tokens,
        completion_tokens=raw.completion_tokens,
        cost_usd=cost,
        truncated=raw.truncated,
    )


async def chat_complete(
    provider: ChatProvider,
    *,
    repo: Repo,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    context: dict[str, Any] | None = None,
) -> ChatResult:
    """Chat completion with automatic retry when the response is truncated.

    If the provider reports `truncated=True` on the first call, retry
    once with `max_tokens` doubled (capped at `_MAX_RETRY_TOKENS`). The
    retry replaces the result — you don't get both. Cost is logged for
    both calls. Provider-agnostic: works the same for OpenAI, OpenRouter,
    Anthropic, Google, and Local since each adapter's `truncated` flag
    is normalized to the same semantics.
    """
    settings = get_settings()
    result = await _one_call(
        provider,
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
            "ai.chat.truncated_retry",
            provider=provider.name,
            model=model,
            old_max=max_tokens,
            new_max=bumped,
            completion=result.completion_tokens,
        )
        result = await _one_call(
            provider,
            repo=repo,
            model=model,
            messages=messages,
            max_tokens=bumped,
            temperature=settings.openai.temperature,
            context={**(context or {}), "retry_of_truncated": True},
        )
        if result.truncated:
            log.warning(
                "ai.chat.truncated_after_retry",
                provider=provider.name,
                model=model,
                max_tokens=bumped,
                completion=result.completion_tokens,
                hint=(
                    "bump output_budget_tokens in the preset file "
                    f"(current budget hit max retry cap {_MAX_RETRY_TOKENS})"
                ),
            )
    return result
