"""Retry helpers for Telegram FloodWaitError and OpenAI 429s."""

from __future__ import annotations

import asyncio
import functools
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from atg.util.logging import get_logger

log = get_logger(__name__)
T = TypeVar("T")


def retry_on_flood(
    max_retries: int = 10,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator that catches Telethon FloodWaitError and sleeps the requested time + 1s.

    Other exceptions propagate immediately.
    """

    def wrap(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def inner(*args: Any, **kwargs: Any) -> T:
            from telethon.errors.rpcerrorlist import FloodWaitError  # type: ignore[attr-defined]

            for attempt in range(max_retries):
                try:
                    return await fn(*args, **kwargs)
                except FloodWaitError as e:
                    delay = int(getattr(e, "seconds", 1)) + 1
                    log.warning("tg.flood_wait", delay=delay, attempt=attempt + 1)
                    await asyncio.sleep(delay)
            # Final attempt, no catch
            return await fn(*args, **kwargs)

        return inner

    return wrap


def retry_on_429(
    max_retries: int = 5, base: float = 1.5, cap: float = 30.0
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Exponential-backoff decorator for OpenAI rate limit / transient 5xx."""

    def wrap(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def inner(*args: Any, **kwargs: Any) -> T:
            from openai import (  # type: ignore[import-not-found]
                APIStatusError,
                APITimeoutError,
                RateLimitError,
            )

            retriable = (RateLimitError, APITimeoutError, APIStatusError)
            for attempt in range(max_retries):
                try:
                    return await fn(*args, **kwargs)
                except retriable as e:
                    # Retry 429 (rate limit) and 5xx; re-raise other 4xx.
                    is_rate_limit = isinstance(e, RateLimitError)
                    is_4xx_other = isinstance(e, APIStatusError) and not is_rate_limit and e.status_code < 500
                    if is_4xx_other:
                        raise
                    delay = min(base**attempt, cap) + random.uniform(0, 1)
                    log.warning(
                        "openai.retry",
                        attempt=attempt + 1,
                        delay=round(delay, 2),
                        err=type(e).__name__,
                    )
                    await asyncio.sleep(delay)
            return await fn(*args, **kwargs)

        return inner

    return wrap


class RateLimiter:
    """Simple rolling-minute token bucket for Telegram read throttle."""

    def __init__(self, max_per_minute: int) -> None:
        self._max = max(1, int(max_per_minute))
        self._hits: list[float] = []

    async def acquire(self) -> None:
        loop = asyncio.get_event_loop()
        now = loop.time()
        self._hits = [t for t in self._hits if now - t < 60]
        if len(self._hits) >= self._max:
            sleep = 60 - (now - self._hits[0]) + 0.05
            if sleep > 0:
                await asyncio.sleep(sleep)
        self._hits.append(loop.time())
