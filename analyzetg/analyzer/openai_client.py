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
    return await oai.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
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
    settings = get_settings()
    resp = await _completion(oai, model, messages, max_tokens, settings.openai.temperature)
    choice = resp.choices[0]
    text = choice.message.content or ""
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
        context=context or {},
    )
    log.info(
        "openai.chat",
        model=model,
        prompt=prompt,
        cached=cached,
        completion=completion,
        cost=cost,
    )
    return ChatResult(
        text=text,
        prompt_tokens=prompt,
        cached_tokens=cached,
        completion_tokens=completion,
        cost_usd=cost,
    )
