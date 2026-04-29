"""Retry helpers for Telegram FloodWaitError and OpenAI 429s."""

from __future__ import annotations

import asyncio
import functools
import random
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from unread.util.logging import get_logger

log = get_logger(__name__)
T = TypeVar("T")


def _user_visible_retry_status(message: str) -> None:
    """Surface a one-line retry status to the terminal, when interactive.

    A long run that hits a 429 / FloodWait used to look frozen — the
    log line went to disk but never reached stdout. This emits a single
    yellow line via Rich when stderr is a TTY; in a non-interactive run
    (CI, scripted) we stay silent and rely on the structured log.
    """
    try:
        import sys as _sys

        from rich.console import Console as _Console

        if not _sys.stderr.isatty():
            return
        _Console(stderr=True).print(f"[yellow]{message}[/]")
    except Exception:
        # Display is best-effort; never let a UI hiccup change retry semantics.
        pass


def retry_on_flood(
    max_retries: int = 10,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator that catches Telethon FloodWaitError and sleeps the requested time + 1s.

    Other exceptions propagate immediately. Users see a one-line
    "FloodWait — sleeping {n}s" status on each retry so a 30-second
    pause doesn't look like a frozen process.
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
                    _user_visible_retry_status(
                        f"Telegram FloodWait — sleeping {delay}s (attempt {attempt + 1}/{max_retries})…"
                    )
                    await asyncio.sleep(delay)
            # Final attempt, no catch
            return await fn(*args, **kwargs)

        return inner

    return wrap


def retry_on_429(
    max_retries: int = 5, base: float = 1.5, cap: float = 30.0
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Exponential-backoff decorator for OpenAI rate limit / transient 5xx.

    On a retry-eligible failure, sleep with jitter and emit a one-line
    "Rate limited — retrying in Ns" status to stderr (TTY only) so the
    user knows the CLI is alive during long sleeps.
    """

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
                    label = "Rate limited" if is_rate_limit else type(e).__name__
                    _user_visible_retry_status(
                        f"{label} — retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})…"
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
